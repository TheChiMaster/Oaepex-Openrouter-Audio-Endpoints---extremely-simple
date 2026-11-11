# Back end functions
- Send and receive requests and audio content
- Nothing  should  ever commit to disc unless the user downloads it specifically, triggered by the front end.
- Keep main.py unchanged for testing functionality
- Script to call the openrouter speech endpoint for all available audio endpoint models, and the parameters they use in json to feed to front end.
- error handling for when back end reports no models avaialbe on data controls, zdr, etc

## API key storage
The pasted key is stored server-side in `backend/.env` (gitignored) — never in
the browser. Endpoints:
- `GET /api/key/status` — `{ "configured": true|false }`, never reveals the key
- `POST /api/key` — validates the key format and writes it to `.env`
- `DELETE /api/key` — removes it from `.env`

Once saved, the front end no longer needs to send the key; the backend reads
it from the environment for every OpenRouter call. Run
`uv run python test_key_endpoints.py` to smoke test these endpoints against a
throwaway .env file.

Check for all available models: https://openrouter.ai/api/v1/models?output_modalities=speech
The URL to check for a specific model's params, which is nhot fully disclosed in the availability check,  is: https://openrouter.ai/api/v1/models/fish-audio/drama-3/endpoints
