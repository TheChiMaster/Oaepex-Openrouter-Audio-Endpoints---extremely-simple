import os
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
OPENROUTER_API_KEY = os.getenv("openrouter_api_key", "")


def audio_request(text_input: str, output_path: str):
    """Sends prompt text to OpenRouter TTS API and saves the resulting audio file."""
    response = requests.post(
        url="https://openrouter.ai/api/v1/audio/speech",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "fish-audio/drama-3",
            "input": text_input,
            "response_format": "mp3",
        },
    )
    response.raise_for_status()

    with open(output_path, "wb") as f:
        f.write(response.content)

    generation_id = response.headers.get("X-Generation-Id")
    print(f"Successfully saved to '{output_path}'. (Generation ID: {generation_id})")


def main():
    print("=== OpenRouter Audio Generator ===")

    if not OPENROUTER_API_KEY:
        print("Error: Please set your 'openrouter_api_key' in the .env file.")
        return

    counter = 1
    while True:
        user_input = input("\nEnter text to synthesize (or type 'exit' to quit): ").strip()

        if user_input.lower() in ("exit", "quit"):
            print("Goodbye!")
            break

        if not user_input:
            print("Input cannot be empty. Please try again.")
            continue

        filename = f"output_{counter}.mp3"

        try:
            audio_request(user_input, filename)
            counter += 1
        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")


if __name__ == "__main__":
    main()