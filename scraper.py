import os
import json
import asyncio
import sys
import time
import re
from typing import List, Optional, Literal

# Playwright na Windows wymaga ProactorEventLoop (subprocessy w asyncio)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from google import genai
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

app = FastAPI(title="Scrapzz API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev only — na produkcji wpisz konkretny adres Reacta
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


# ── Schematy API ─────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str

class ScrapeResultItem(BaseModel):
    url: str
    data: Optional[KierunekStudiow] = None
    error: Optional[str] = None

class ScrapeResponse(BaseModel):
    status: str
    links_found: int
    links_processed: int
    results: List[ScrapeResultItem]


# Limit darmowego planu: 20 req/min → min. 4s między zapytaniami
GEMINI_MIN_INTERVAL = 4.0
MAX_COURSES_PER_RUN = 4

JUNK_URL_PATTERN = re.compile(
    r"(psychotest|rekrutac|kontakt|faq|poznajmy|open.?day|aktualno|"
    r"news|blog|podyplomow|mba|kursy|szkolen|regulamin|bip|"
    r"mapa.?strony|o.?uczelni|studenci|absolwent)",
    re.I,
)
_last_gemini_call = 0.0


class GeminiQuotaError(RuntimeError):
    """Wyczerpany limit API Gemini — nie retryuj w nieskończoność."""


def _parse_retry_delay(error: Exception) -> float:
    """Czyta sugerowany czas oczekiwania z odpowiedzi Google (429/503)."""
    msg = str(error)
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", msg, re.IGNORECASE)
    if match:
        return min(float(match.group(1)) + 5, 90)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return 65
    return 15


def call_gemini_with_retry(prompt: str, config: dict, retries: int = 2) -> str:
    """Max 2 próby — przy 429 nie blokuj requestu na 5 minut."""
    global _last_gemini_call
    client = genai.Client()
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        elapsed = time.time() - _last_gemini_call
        if elapsed < GEMINI_MIN_INTERVAL:
            gap = GEMINI_MIN_INTERVAL - elapsed
            print(f"⏳ Odstęp między zapytaniami: {gap:.1f}s...")
            time.sleep(gap)
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config=config,
            )
            _last_gemini_call = time.time()
            return response.text
        except Exception as e:
            last_error = e
            print(f"⚠️  Próba {attempt}/{retries} nieudana: {e}")
            is_rate_limit = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
            if is_rate_limit and attempt >= retries:
                raise GeminiQuotaError(
                    "Limit API Gemini wyczerpany (darmowy plan: 20 zapytań/min). "
                    "Odczekaj 1–2 minuty i spróbuj ponownie."
                ) from e
            if attempt < retries:
                wait = _parse_retry_delay(e)
                print(f"⏳ Czekam {wait:.0f}s przed ponowną próbą...")
                time.sleep(wait)

    raise GeminiQuotaError(
        "Gemini nie odpowiedział — limit API wyczerpany. Spróbuj za 1–2 minuty."
    ) from last_error


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
        "Jesteś rygorystycznym filtrem linków. Twoim JEDYNYM zadaniem jest zwrócić "
        "tablicę JSON z bezwzględnymi URL (http/https) prowadzącymi WYŁĄCZNIE do stron "
        "szczegółowego opisu KONKRETNEGO kierunku studiów (np. informatyka, budownictwo, "
        "automatyka, zarządzanie).\n\n"
        "KATEGORYCZNIE ODRZUĆ i NIE zwracaj linków do:\n"
        "- nawigacji, menu, stopki, breadcrumbs\n"
        "- strony głównej, kontaktu, mapy strony, FAQ, aktualności, wydarzeń\n"
        "- rekrutacji, zasad przyjęć, terminów, psychotestów, „poznajmy się”, open day\n"
        "- studiów podyplomowych, MBA, kursów, szkoleń, certyfikatów\n"
        "- profili wydziałów bez konkretnego kierunku\n"
        "- mediów społecznościowych, plików PDF, mailto:, tel:\n\n"
        "Jeśli link NIE prowadzi ewidentnie do opisu jednego, nazwanego kierunku — pomiń go.\n"
        "W razie wątpliwości — pomiń. Lepiej pusta tablica niż śmieć.\n"
        "Odpowiedz TYLKO tablicą JSON (List[str]), bez żadnego dodatkowego tekstu.\n\n"
        f"Tekst strony:\n{result.markdown}"
    )
    response_text = await asyncio.to_thread(
        call_gemini_with_retry,
        prompt,
        {"response_mime_type": "application/json", "temperature": 0.0},
    )
    links: List[str] = json.loads(response_text)
    links = [
        u for u in links
        if u.startswith(("http://", "https://")) and not JUNK_URL_PATTERN.search(u)
    ]
    links = list(dict.fromkeys(links))
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

# ── Endpointy ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/scrape", response_model=ScrapeResponse)
async def run_spider(request: ScrapeRequest):
    catalog_url = request.url
    results: List[ScrapeResultItem] = []

    browser_config = BrowserConfig(headless=True, java_script_enabled=True)
    course_run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=10,
        delay_before_return_html=3.0,
    )

    crawler = AsyncWebCrawler(config=browser_config)
    await crawler.start()
    try:
        try:
            links = await get_course_links(catalog_url, crawler)
        except GeminiQuotaError as e:
            raise HTTPException(status_code=429, detail=str(e))

        if not links:
            raise HTTPException(status_code=404, detail="Nie znaleziono linków do kierunków.")

        for i, url in enumerate(links[:MAX_COURSES_PER_RUN], 1):
            print(f"[{i}/{MAX_COURSES_PER_RUN}] Przetwarzam: {url}")
            result = await crawler.arun(url=url, config=course_run_config)

            if result.success:
                try:
                    kierunek = await asyncio.to_thread(extract_with_llm, result.markdown)
                    results.append(ScrapeResultItem(url=url, data=kierunek))
                    print(f"✅ [{i}/{MAX_COURSES_PER_RUN}] Wyekstrahowano: {kierunek.kierunek}")
                except GeminiQuotaError as e:
                    print(f"❌ [{i}/{MAX_COURSES_PER_RUN}] Limit API: {e}")
                    results.append(ScrapeResultItem(url=url, error=str(e)))
                except Exception as e:
                    print(f"❌ [{i}/{MAX_COURSES_PER_RUN}] Błąd ekstrakcji: {e}")
                    results.append(ScrapeResultItem(url=url, error=str(e)))
            else:
                print(f"❌ [{i}/{MAX_COURSES_PER_RUN}] Błąd scrapowania: {result.error_message}")
                results.append(ScrapeResultItem(url=url, error=result.error_message))

            if i < min(MAX_COURSES_PER_RUN, len(links)):
                await asyncio.sleep(3)
    finally:
        try:
            await crawler.close()
        except Exception as exc:
            print(f"⚠️ Zamknięcie crawlera: {exc}")

    return ScrapeResponse(
        status="success",
        links_found=len(links),
        links_processed=len(results),
        results=results,
    )


if __name__ == "__main__":
    import uvicorn
    # reload=True psuje Playwright na Windows (subprocessy w asyncio)
    uvicorn.run(
        "scraper:app",
        host="0.0.0.0",
        port=8000,
        reload=sys.platform != "win32",
    )