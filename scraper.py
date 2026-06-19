import os
import json
import asyncio
import sys
import time
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from google import genai
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# 1. Definicja Schematu Danych (Pydantic)
class KierunekStudiow(BaseModel):
    kierunek: str = Field(description="Nazwa kierunku studiów")
    stopien: Literal["1_stopnia", "2_stopnia", "jednolite_magisterskie"] = Field(description="Stopień studiów")
    tytul: Literal["licencjat", "inżynier", "magister"] = Field(description="Uzyskiwany tytuł zawodowy")
    tryb: List[Literal["stacjonarne", "niestacjonarne"]] = Field(description="Dostępne tryby studiów")
    semestry: Optional[int] = Field(None, description="Liczba semestrów w formie liczby całkowitej")
    wydzial: Optional[str] = Field(None, description="Wydział prowadzący kierunek")
    jezyk: str = Field(default="polski", description="Język wykładowy")
    specjalizacje: List[str] = Field(default_factory=list, description="Lista dostępnych specjalizacji")
    rekrutacja: List[str] = Field(default_factory=list, description="Wymagane przedmioty maturalne lub zasady rekrutacji")
    czesne: Optional[str] = Field(None, description="Koszty studiów. Jeśli brak informacji lub studia są darmowe, zwróć null")
    opis: str = Field(description="Krótkie, 2-3 zdaniowe podsumowanie profilu kierunku na podstawie tekstu")


def call_gemini_with_retry(prompt: str, config: dict, retries: int = 3) -> str:
    client = genai.Client()
    for attempt in range(1, retries + 1):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=config,
            )
            return response.text
        except Exception as e:
            print(f"⚠️  Próba {attempt}/{retries} nieudana: {e}")
            if attempt < retries:
                wait = 10 * attempt
                print(f"⏳ Czekam {wait}s przed ponowną próbą...")
                time.sleep(wait)
    raise RuntimeError("Gemini nie odpowiedział po wszystkich próbach.")


def extract_with_llm(text: str) -> KierunekStudiow:
    prompt = (
        "Jesteś ekspertem ds. rekrutacji. Przeanalizuj tekst ze strony uczelni "
        "i wyciągnij informacje o kierunku studiów, ignorując nawigację i stopki. "
        f"Zwróć dane precyzyjnie dopasowane do schematu.\n\nTekst:\n{text}"
    )
    response_text = call_gemini_with_retry(
        prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": KierunekStudiow,
            "temperature": 0.1,
        },
    )
    return KierunekStudiow.model_validate_json(response_text)


async def get_course_links(url: str, crawler: AsyncWebCrawler) -> List[str]:
    print(f"🔍 Pobieram katalog kierunków: {url}")
    run_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, word_count_threshold=5)
    result = await crawler.arun(url=url, config=run_config)
    if not result.success:
        print(f"❌ Nie udało się pobrać katalogu: {result.error_message}")
        return []

    prompt = (
        "Przeanalizuj poniższy tekst strony uczelni. "
        "Zwróć wyłącznie tablicę JSON zawierającą bezwzględne adresy URL (zaczynające się od http) "
        "prowadzące do stron poszczególnych kierunków studiów. "
        "Ignoruj linki do nawigacji, stopki, mediów społecznościowych i innych sekcji. "
        "Odpowiedz TYLKO tablicą JSON, bez żadnego dodatkowego tekstu.\n\n"
        f"Tekst strony:\n{result.markdown}"
    )
    response_text = call_gemini_with_retry(
        prompt,
        config={"response_mime_type": "application/json", "temperature": 0.0},
    )
    links: List[str] = json.loads(response_text)
    print(f"✅ Znaleziono {len(links)} linków do kierunków.")
    return links


async def extract_course_info(url: str):
    print(f"🕵️ Rozpoczynam skanowanie: {url}")

    browser_config = BrowserConfig(
        headless=True,
        java_script_enabled=True,
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS, 
        word_count_threshold=10, 
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)
        
        if result.success:
            filename = "scraped_pja.md"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(result.markdown)
            print(f"✅ Zapisano surowy tekst do '{filename}'.")

            print("🧠 Wysyłam do Gemini...")
            kierunek = extract_with_llm(result.markdown)
            print("\n🎉 Wynik ekstrakcji:\n")
            print(json.dumps(kierunek.model_dump(), indent=4, ensure_ascii=False))
        else:
            print(f"\n❌ Błąd podczas skanowania: {result.error_message}")

async def main():
    CATALOG_URL = "https://pja.edu.pl/studia/"

    browser_config = BrowserConfig(headless=True, java_script_enabled=True)
    run_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, word_count_threshold=10)

    async with AsyncWebCrawler(config=browser_config) as crawler:
        links = await get_course_links(CATALOG_URL, crawler)

        if not links:
            print("Brak linków do przetworzenia.")
            return

        for i, url in enumerate(links[:5], 1):
            print(f"\n{'='*60}")
            print(f"[{i}/5] Przetwarzam: {url}")
            result = await crawler.arun(url=url, config=run_config)
            if result.success:
                filename = f"scraped_{i}.md"
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(result.markdown)
                print(f"📄 Zapisano surowy tekst do '{filename}'.")
                print("🧠 Wysyłam do Gemini...")
                kierunek = extract_with_llm(result.markdown)
                print("\n🎉 Wynik ekstrakcji:\n")
                print(json.dumps(kierunek.model_dump(), indent=4, ensure_ascii=False))
            else:
                print(f"❌ Błąd scrapowania: {result.error_message}")

            if i < min(5, len(links)):
                print("⏳ Czekam 3 sekundy...")
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())