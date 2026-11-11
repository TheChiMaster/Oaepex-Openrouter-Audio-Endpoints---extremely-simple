"""FastAPI backend for Oaepex - OpenRouter Audio Endpoints.

Serves the front end with:
  - GET    /api/models      : all available speech models + their endpoint parameters
  - POST   /api/speech      : text-to-speech generation, returns raw audio bytes
  - GET    /api/key/status  : whether an OpenRouter key is configured (never the key itself)
  - POST   /api/key         : store the pasted key in the local .env file
  - DELETE /api/key         : remove the stored key

Audio is streamed straight back to the client and never written to disc. The
only file the backend ever writes is the local `.env`, which holds the API
key the user pasted in the browser. That file lives on the user's own machine
and is excluded from version control via .gitignore.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv, set_key, unset_key
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

# The .env file lives next to this module (the backend directory), so the
# key is stored on the user's own machine regardless of where uvicorn was
# launched from.
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
ENV_API_KEY = os.getenv("openrouter_api_key", "")

# OpenRouter keys look like `sk-or-v1-` followed by a long alphanumeric body.
# The check is deliberately loose (no fixed length) so it keeps working if
# OpenRouter changes the key format, while still catching obvious typos.
OPENROUTER_KEY_PATTERN = re.compile(r"^sk-or-[A-Za-z0-9-]{20,}$")

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


# Hard limits so a malicious or buggy client cannot exhaust memory by
# posting huge payloads (audio references are base64 data URIs).
MAX_INPUT_CHARS = 20_000
MAX_INSTRUCTIONS_CHARS = 2_000
MAX_VOICE_CHARS = 500
MAX_REFERENCE_BYTES = 20 * 1024 * 1024  # 20 MB per reference audio
MAX_REFERENCES = 5
ALLOWED_FORMATS = {"mp3", "wav", "pcm", "opus", "flac", "aac"}
ALLOWED_SPEED = (0.25, 4.0)


def _clean_str(value: Any, name: str, max_len: int) -> str:
    """Validate an optional string field, returning '' when absent."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"'{name}' must be a string.")
    value = value.strip()
    if len(value) > max_len:
        raise HTTPException(
            status_code=422,
            detail=f"'{name}' exceeds the maximum of {max_len} characters.",
        )
    return value


def _validate_references(raw: Any) -> list[dict[str, Any]]:
    """Validate the optional `input_references` array (voice cloning)."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise HTTPException(
            status_code=422, detail="'input_references' must be a non-empty array."
        )
    if len(raw) > MAX_REFERENCES:
        raise HTTPException(
            status_code=422,
            detail=f"Too many references; the maximum is {MAX_REFERENCES}.",
        )
    refs: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=422, detail="Each reference must be an object."
            )
        if item.get("type") == "input_audio":
            audio = item.get("input_audio")
            if not isinstance(audio, dict) or not isinstance(audio.get("data"), str):
                raise HTTPException(
                    status_code=422,
                    detail="Audio references need an 'input_audio.data' string.",
                )
            # Data URIs are `data:<mime>;base64,<payload>`. Reject anything
            # that would decode to more than MAX_REFERENCE_BYTES.
            data = audio["data"]
            if len(data) > MAX_REFERENCE_BYTES * 4 // 3 + 256:
                raise HTTPException(
                    status_code=413,
                    detail="Reference audio is too large (max 20 MB).",
                )
            refs.append({"type": "input_audio", "input_audio": {"data": data}})
        elif item.get("type") == "text":
            text = _clean_str(item.get("text"), "reference text", MAX_INPUT_CHARS)
            if text:
                refs.append({"type": "text", "text": text})
        else:
            raise HTTPException(
                status_code=422,
                detail="Reference types must be 'input_audio' or 'text'.",
            )
    return refs


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def parse_pcm_params(
    content_type: str | None,
    default_rate: int = 24000,
    default_channels: int = 1,
) -> tuple[int, int]:
    """Parse sample rate and channel count from OpenRouter's Content-Type.

    Raw PCM responses carry their metadata as header directives, e.g.
    `audio/pcm;rate=44100;channels=2`. Both values differ per provider
    (Fish Audio emits 44.1 kHz; some providers stream stereo), so they must
    be read per response rather than assumed. Falls back to 24 kHz mono
    when a directive is absent.
    """
    sample_rate = default_rate
    channels = default_channels
    if content_type:
        rate_match = re.search(r"rate=(\d+)", content_type, re.IGNORECASE)
        if rate_match:
            sample_rate = int(rate_match.group(1))
        channel_match = re.search(r"channels=(\d+)", content_type, re.IGNORECASE)
        if channel_match:
            channels = int(channel_match.group(1))
    return sample_rate, channels


def wrap_pcm_as_wav(
    pcm: bytes,
    sample_rate: int = 24000,
    channels: int = 1,
    bits_per_sample: int = 16,
) -> bytes:
    """Prefix raw PCM bytes with a RIFF/WAVE header so browsers can play them.

    OpenRouter's `pcm` response format is a raw byte stream with no container,
    which `<audio>` elements cannot play. The stream is 16-bit little-endian
    PCM; the sample rate and channel count come from the response's
    Content-Type header (see `parse_pcm_params`) and must match, or playback
    pitch/speed shift.
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


# --------------------------------------------------------------------------- #
# API key storage                                                              #
# --------------------------------------------------------------------------- #
# The browser never persists the key. The user pastes it once, the front end
# POSTs it here over localhost, and it is written to the backend's own .env
# file on the user's machine. Subsequent requests need no key at all: the
# backend reads it from the environment. The key is never echoed back to any
# client — only a boolean "is a key configured" is exposed.


@app.get("/api/key/status")
def key_status() -> dict[str, bool]:
    """Report whether an OpenRouter key is configured, without revealing it."""
    return {"configured": bool(ENV_API_KEY)}


@app.post("/api/key")
async def save_key(request: Request) -> dict[str, str]:
    """Validate the pasted key and store it in the local .env file."""
    global ENV_API_KEY

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Request body must be JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    key = str(body.get("key", "")).strip()
    if not key or not OPENROUTER_KEY_PATTERN.match(key):
        raise HTTPException(
            status_code=422,
            detail="That does not look like a valid OpenRouter API key. "
            "Keys start with 'sk-or-' and come from "
            "https://openrouter.ai/workspaces/default/keys",
        )

    try:
        ENV_PATH.touch(exist_ok=True)
        set_key(str(ENV_PATH), "openrouter_api_key", key)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not write the key to {ENV_PATH.name}: {exc}",
        ) from exc

    ENV_API_KEY = key
    return {"status": "saved"}


@app.delete("/api/key")
def delete_key() -> dict[str, str]:
    """Remove the stored key from the .env file and memory."""
    global ENV_API_KEY

    try:
        if ENV_PATH.exists():
            unset_key(str(ENV_PATH), "openrouter_api_key")
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not update {ENV_PATH.name}: {exc}",
        ) from exc

    ENV_API_KEY = ""
    return {"status": "cleared"}


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

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Request body must be JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    model = _clean_str(body.get("model"), "model", MAX_VOICE_CHARS)
    text = _clean_str(body.get("input"), "input", MAX_INPUT_CHARS)
    if not model or not text:
        raise HTTPException(status_code=422, detail="Both 'model' and 'input' are required.")

    payload: dict[str, Any] = {"model": model, "input": text}

    voice = _clean_str(body.get("voice"), "voice", MAX_VOICE_CHARS)
    if voice:
        payload["voice"] = voice
    instructions = _clean_str(body.get("instructions"), "instructions", MAX_INSTRUCTIONS_CHARS)
    if instructions:
        payload["instructions"] = instructions

    response_format = _clean_str(body.get("response_format"), "response_format", 10)
    if response_format:
        if response_format not in ALLOWED_FORMATS:
            raise HTTPException(
                status_code=422,
                detail=f"'response_format' must be one of: {', '.join(sorted(ALLOWED_FORMATS))}.",
            )
        payload["response_format"] = response_format

    speed = body.get("speed")
    if speed not in (None, ""):
        if not isinstance(speed, (int, float)) or isinstance(speed, bool):
            raise HTTPException(status_code=422, detail="'speed' must be a number.")
        if not ALLOWED_SPEED[0] <= speed <= ALLOWED_SPEED[1]:
            raise HTTPException(
                status_code=422,
                detail=f"'speed' must be between {ALLOWED_SPEED[0]} and {ALLOWED_SPEED[1]}.",
            )
        payload["speed"] = speed

    references = _validate_references(body.get("input_references"))
    if references:
        payload["input_references"] = references

    provider = body.get("provider")
    if isinstance(provider, dict):
        payload["provider"] = provider

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
    # minimal WAV container using the rate/channels from the Content-Type
    # header and serve it as audio/wav.
    if content_type.split(";")[0].strip().lower() == "audio/pcm" or (
        body.get("response_format") == "pcm"
    ):
        sample_rate, channels = parse_pcm_params(content_type)
        content = wrap_pcm_as_wav(
            content, sample_rate=sample_rate, channels=channels
        )
        media_type = "audio/wav"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "X-Generation-Id": resp.headers.get("X-Generation-Id", ""),
            "Content-Disposition": 'inline; filename="generated-audio"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"message": resp.text[:500]}
