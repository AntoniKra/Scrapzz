import os
import sys

from openai import OpenAI, APIError, AuthenticationError, RateLimitError

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

MODEL = "qwen/qwen3-next-80b-a3b-instruct:free"
PROMPT = "Cześć, jak się dziś masz?"


def main() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("❌ Brak klucza OPENROUTER_API_KEY w zmiennych środowiskowych.")
        print("   Ustaw go np.: export OPENROUTER_API_KEY='sk-or-...'")
        sys.exit(1)

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    print(f"🔗 OpenRouter → model: {MODEL}")
    print(f"📤 Zapytanie: {PROMPT}\n")

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": PROMPT}],
        )
        answer = response.choices[0].message.content
        print("✅ Odpowiedź modelu:\n")
        print(answer)

        if response.usage:
            print(
                f"\n📊 Tokeny — prompt: {response.usage.prompt_tokens}, "
                f"odpowiedź: {response.usage.completion_tokens}, "
                f"razem: {response.usage.total_tokens}"
            )

    except AuthenticationError:
        print("❌ Błąd autoryzacji — sprawdź poprawność OPENROUTER_API_KEY.")
        sys.exit(1)
    except RateLimitError as e:
        print(f"❌ Limit zapytań (429): {e}")
        sys.exit(1)
    except APIError as e:
        print(f"❌ Błąd API OpenRouter: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Nieoczekiwany błąd: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
