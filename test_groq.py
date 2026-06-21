import os
import sys

from groq import Groq, GroqError, APIError, AuthenticationError, RateLimitError

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

MODEL = "llama-3.3-70b-versatile"
PROMPT = "Cześć! Jak się dziś masz? Odpowiedz w jednym zdaniu."


def main() -> None:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("❌ Brak klucza GROQ_API_KEY w zmiennych środowiskowych.")
        print("   Ustaw go np.: export GROQ_API_KEY='gsk_...'")
        sys.exit(1)

    client = Groq(api_key=api_key)

    print(f"🔗 Groq → model: {MODEL}")
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
        print("❌ Błąd autoryzacji — sprawdź poprawność GROQ_API_KEY.")
        sys.exit(1)
    except RateLimitError as e:
        print(f"❌ Limit zapytań (429): {e}")
        sys.exit(1)
    except APIError as e:
        print(f"❌ Błąd API Groq: {e}")
        sys.exit(1)
    except GroqError as e:
        print(f"❌ Błąd Groq: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Nieoczekiwany błąd: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
