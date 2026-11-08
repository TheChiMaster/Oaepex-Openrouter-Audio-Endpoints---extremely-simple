"""FastAPI backend for Oaepex - OpenRouter Audio Endpoints.

Serves the front end with:
  - GET  /api/models   : all available speech models + their endpoint parameters
  - POST /api/speech   : text-to-speech generation, returns raw audio bytes

Nothing is ever written to disc here; audio is streamed straight back to the
client. The API key is only ever held in memory for the duration of a request.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

load_dotenv()

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
ENV_API_KEY = os.getenv("openrouter_api_key", "")

app = FastAPI(title="Oaepex Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Fallback voice identifiers per provider family, used only when a model
# does not report its own `supported_voices` via the Models API. Providers
# not listed fall back to free text entry.
KNOWN_VOICES: dict[str, list[str]] = {
    "openai": [
        "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx",
        "sage", "shimmer", "verse",
    ],
    "google": [
        "Kore", "Puck", "Charon", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr",
    ],
    "mistralai": ["default"],
    "microsoft": [
        "en-US-Harper:MAI-Voice-2.1", "en-US-Emilia:MAI-Voice-2.1",
        "en-US-Andrew:MAI-Voice-2.1", "en-US-Brian:MAI-Voice-2.1",
    ],
}


def get_api_key(request: Request) -> str:
    """API key from the request header, falling back to the .env secret."""
    key = request.headers.get("x-openrouter-key", "").strip()
    return key or ENV_API_KEY


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def wrap_pcm_as_wav(
    pcm: bytes,
    sample_rate: int = 24000,
    channels: int = 1,
    bits_per_sample: int = 16,
) -> bytes:
    """Prefix raw PCM bytes with a RIFF/WAVE header so browsers can play them.

    OpenRouter's `pcm` response format is a raw byte stream with no container,
    which `<audio>` elements cannot play. Providers document it as 16-bit
    little-endian mono PCM at 24 kHz (Azure explicitly; OpenAI and Gemini use
    the same layout), so wrapping it in a minimal WAV header makes it playable
    everywhere without re-encoding.
    """
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    header = b"RIFF" + (36 + len(pcm)).to_bytes(4, "little") + b"WAVE"
    header += b"fmt " + (16).to_bytes(4, "little")
    header += (1).to_bytes(2, "little")  # 1 = uncompressed PCM
    header += channels.to_bytes(2, "little")
    header += sample_rate.to_bytes(4, "little")
    header += byte_rate.to_bytes(4, "little")
    header += block_align.to_bytes(2, "little")
    header += bits_per_sample.to_bytes(2, "little")
    header += b"data" + len(pcm).to_bytes(4, "little")
    return header + pcm


def describe_error(status: int, payload: Any) -> str:
    """Turn an OpenRouter error payload into a human readable message."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message", "")
            code = err.get("code", status)
            return f"OpenRouter error {code}: {msg}"
        if isinstance(err, str):
            return f"OpenRouter error {status}: {err}"
        msg = payload.get("message")
        if msg:
            return f"OpenRouter error {status}: {msg}"
    return f"OpenRouter returned HTTP {status}"


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/models")
def list_models(request: Request) -> JSONResponse:
    """Return every model that can output speech, plus per-model parameters.

    Capability flags (voice cloning, multiple references, image reference)
    come from the per-model endpoints API, fetched concurrently.
    """
    api_key = get_api_key(request)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="No API key supplied. Enter your OpenRouter key in the app "
            "or set 'openrouter_api_key' in the backend .env file.",
        )

    headers = auth_headers(api_key)

    with httpx.Client(timeout=30) as client:
        try:
            resp = client.get(
                f"{OPENROUTER_BASE}/models",
                params={"output_modalities": "speech"},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Could not reach OpenRouter: {exc}"
            ) from exc

        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="Invalid OpenRouter API key.")
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=describe_error(resp.status_code, _safe_json(resp)),
            )

        models: list[dict[str, Any]] = resp.json().get("data", [])
        if not models:
            raise HTTPException(
                status_code=404,
                detail="No speech models are available to your account. "
                "This can happen when your account enforces data controls "
                "(Zero Data Retention) that exclude every TTS provider, or "
                "when your region/privacy settings filter all providers out. "
                "Review https://openrouter.ai/settings/privacy and try again.",
            )

        # Fetch endpoint details for capability flags (one request per model).
        def fetch_endpoint(model_id: str) -> dict[str, Any]:
            try:
                r = client.get(
                    f"{OPENROUTER_BASE}/models/{model_id}/endpoints",
                    headers=headers,
                )
                if r.status_code != 200:
                    return {}
                data = r.json().get("data", {})
                endpoints = data.get("endpoints", [])
                caps: dict[str, Any] = {}
                for ep in endpoints:
                    caps.setdefault(
                        "supports_voice_cloning",
                        ep.get("supports_voice_cloning", False),
                    )
                    caps.setdefault(
                        "supports_multiple_audio_references",
                        ep.get("supports_multiple_audio_references", False),
                    )
                    caps.setdefault(
                        "supports_image_reference",
                        ep.get("supports_image_reference", False),
                    )
                    if "pricing" in ep and "pricing" not in caps:
                        caps["pricing"] = ep["pricing"]
                return caps
            except httpx.HTTPError:
                return {}

        results = [fetch_endpoint(m["id"]) for m in models]

        enriched: list[dict[str, Any]] = []

        for model, caps in zip(models, results):
            provider_slug = model["id"].split("/")[0]
            # Prefer the live voice list from the Models API; only fall back
            # to the static table when the model reports none (e.g. models
            # that rely on voice cloning / reference audio instead of IDs).
            voices = model.get("supported_voices") or KNOWN_VOICES.get(
                provider_slug, []
            )
            enriched.append(
                {
                    "id": model["id"],
                    "name": model.get("name", model["id"]),
                    "context_length": model.get("context_length"),
                    "pricing": model.get("pricing", {}),
                    "capabilities": caps,
                    "known_voices": voices,
                    "provider_slug": provider_slug,
                }
            )

    return JSONResponse({"models": enriched})


@app.post("/api/speech")
async def speech(request: Request) -> Response:
    """Generate speech audio and stream the raw bytes back. Never touches disc."""
    api_key = get_api_key(request)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="No API key supplied. Enter your OpenRouter key in the app "
            "or set 'openrouter_api_key' in the backend .env file.",
        )

    body = await request.json()

    payload: dict[str, Any] = {
        "model": body.get("model"),
        "input": body.get("input"),
    }
    for field in ("voice", "instructions", "response_format", "speed"):
        if body.get(field) not in (None, ""):
            payload[field] = body[field]
    if body.get("input_references"):
        payload["input_references"] = body["input_references"]
    if isinstance(body.get("provider"), dict):
        payload["provider"] = body["provider"]

    if not payload["model"] or not payload["input"]:
        raise HTTPException(status_code=422, detail="Both 'model' and 'input' are required.")

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/audio/speech",
                headers={**auth_headers(api_key), "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach OpenRouter: {exc}") from exc

    if resp.status_code != 200:
        # Non-200 responses are JSON error bodies, not audio.
        try:
            detail = describe_error(resp.status_code, resp.json())
        except Exception:
            detail = f"OpenRouter returned HTTP {resp.status_code}"
        raise HTTPException(status_code=resp.status_code, detail=detail)

    content_type = resp.headers.get("content-type", "audio/mpeg")
    content = resp.content
    media_type = content_type
    # Raw PCM cannot be played by browser <audio> elements; wrap it in a
    # minimal WAV container (24 kHz, 16-bit, mono) and serve it as audio/wav.
    if content_type.split(";")[0].strip().lower() == "audio/pcm" or (
        body.get("response_format") == "pcm"
    ):
        content = wrap_pcm_as_wav(content)
        media_type = "audio/wav"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "X-Generation-Id": resp.headers.get("X-Generation-Id", ""),
            "Content-Disposition": 'inline; filename="generated-audio"',
        },
    )


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"message": resp.text[:500]}
