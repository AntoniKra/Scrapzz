import os
import json
import asyncio
import sys
import time
import re
import threading
from typing import List, Optional, Literal
from urllib.parse import urlparse, urljoin

# Playwright na Windows wymaga ProactorEventLoop (subprocessy w asyncio)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AnyHttpUrl, BaseModel, Field
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

class CzesneEntry(BaseModel):
    wariant: str = Field(description="Opis wariantu opłaty, np. 'I stopień stacjonarne', 'II stopień niestacjonarne'")
    kwota: str = Field(description="Kwota lub opis opłaty, np. '2400 zł/semestr', 'bezpłatne'")


class KierunekStudiow(BaseModel):
    kierunek: str = Field(
        description="Oficjalna nazwa kierunku DOKŁADNIE jak na stronie — w oryginalnym języku, bez tłumaczenia"
    )
    stopien: Literal["1_stopnia", "2_stopnia", "jednolite_magisterskie"] = Field(description="Stopień studiów")
    tytul: Literal["licencjat", "inżynier", "magister"] = Field(description="Uzyskiwany tytuł zawodowy")
    tryb: List[Literal["stacjonarne", "niestacjonarne"]] = Field(description="Dostępne tryby studiów")
    semestry: int = Field(description="Liczba semestrów — zawsze liczba całkowita, nigdy null")
    wydzial: Optional[str] = Field(None, description="Wydział prowadzący kierunek")
    jezyk: str = Field(default="polski", description="Język wykładowy")
    specjalizacje: List[str] = Field(default_factory=list, description="Lista dostępnych specjalizacji")
    rekrutacja: List[str] = Field(default_factory=list, description="Wymagane przedmioty maturalne lub zasady rekrutacji")
    czesne: Optional[List[CzesneEntry]] = Field(None, description="Lista opłat wg wariantów. Null jeśli brak danych.")
    opis: str = Field(description="Krótkie, 2-3 zdaniowe podsumowanie profilu kierunku na podstawie tekstu")


# ── Schematy API ─────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: AnyHttpUrl

class ScrapeResultItem(BaseModel):
    url: str
    data: Optional[KierunekStudiow] = None
    error: Optional[str] = None

class ScrapeResponse(BaseModel):
    status: str
    links_found: int
    links_processed: int
    results: List[ScrapeResultItem]


# ── Konfiguracja Gemini ───────────────────────────────────────────────────────

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_MIN_INTERVAL = 4.0
MAX_COURSES_PER_RUN = 10

_gemini_client = genai.Client()
_last_gemini_call = 0.0
_gemini_lock = threading.Lock()


class GeminiQuotaError(RuntimeError):
    """Wyczerpany limit API Gemini — nie retryuj w nieskończoność."""


def _parse_retry_delay(error: Exception) -> float:
    msg = str(error)
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", msg, re.IGNORECASE)
    if match:
        return min(float(match.group(1)) + 5, 90)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return 65
    return 15


def call_gemini_with_retry(
    prompt: str,
    config: dict,
    retries: int = 2,
) -> str:
    """Wywołuje Gemini z rate-limitingiem i max 2 próbami przy błędzie."""
    global _last_gemini_call
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        with _gemini_lock:
            elapsed = time.time() - _last_gemini_call
            if elapsed < GEMINI_MIN_INTERVAL:
                gap = GEMINI_MIN_INTERVAL - elapsed
                print(f"⏳ Odstęp Gemini: {gap:.1f}s...")
                time.sleep(gap)
            try:
                response = _gemini_client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=config,
                )
                _last_gemini_call = time.time()
                return response.text
            except Exception as e:
                last_error = e
                _last_gemini_call = time.time()

        print(f"⚠️  Próba {attempt}/{retries} nieudana: {last_error}")
        is_rate_limit = "429" in str(last_error) or "RESOURCE_EXHAUSTED" in str(last_error)
        if is_rate_limit and attempt >= retries:
            raise GeminiQuotaError(
                f"Limit API Gemini wyczerpany (model: {GEMINI_MODEL}). "
                "Odczekaj chwilę i spróbuj ponownie."
            ) from last_error
        if attempt < retries:
            wait = _parse_retry_delay(last_error)
            print(f"⏳ Czekam {wait:.0f}s przed ponowną próbą...")
            time.sleep(wait)

    raise GeminiQuotaError(
        f"Gemini nie odpowiedział (model: {GEMINI_MODEL}). Spróbuj za chwilę."
    ) from last_error


JUNK_URL_PATTERN = re.compile(
    r"(psychotest|rekrutac|kontakt|faq|poznajmy|open.?day|aktualno|"
    r"news|blog|podyplomow|mba|kursy|szkolen(?![a-z])|regulamin|bip|"
    r"mapa.?strony|o.?uczelni|studenci|absolwent)",
    re.I,
)

COURSE_PATH_PATTERN = re.compile(
    r"(oferta-studiow|/kierunek/|pokazKierunek|/studia/|/program/|/oferta/studia)",
    re.I,
)

def _is_junk_url(url: str) -> bool:
    """Filtruje śmieciowe ścieżki — nie sprawdza hosta (np. rekrutacja.p.lodz.pl)."""
    parsed = urlparse(url)
    haystack = f"{parsed.path}?{parsed.query}".lower()
    if COURSE_PATH_PATTERN.search(haystack):
        return False
    return bool(JUNK_URL_PATTERN.search(haystack))


COOKIE_BANNER_PATTERN = re.compile(
    r"(cookiebot|cookieyes|plików cookie|pliki cookie|akceptuj wszystko|"
    r"accept all|consent|cybotcookiebotdialog)",
    re.I,
)
CATALOG_CONTENT_PATTERN = re.compile(
    r"(card_post|filter-results|kierunek studiów|/oferta/studia-|/kierunek/)",
    re.I,
)

# Tylko dla crawla katalogu — cookie + czekanie na AJAX + scroll (lazy-load).
CATALOG_JS_PREP = """
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const acceptCookies = () => {
    const selectors = [
      '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
      '#CybotCookiebotDialogBodyButtonAccept',
      '#CybotCookiebotDialogBodyButtonDecline',
      'button[id*="accept"]',
      'a[id*="accept"]',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) { el.click(); return true; }
    }
    for (const el of document.querySelectorAll('button, a, [role="button"]')) {
      const t = (el.textContent || '').trim().toLowerCase();
      if (/akceptuj|accept all|zgoda|^accept$/i.test(t)) { el.click(); return true; }
    }
    return false;
  };
  acceptCookies();
  await sleep(800);
  acceptCookies();
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    if (document.querySelector('.card_post_item, #filter-results a[href*="/oferta/"]')) break;
    await sleep(400);
  }
})();
"""

CATALOG_JS_SCROLL = """
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 6; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(700);
  }
  window.scrollTo(0, 0);
  await sleep(300);
})();
"""


def _catalog_markdown_looks_js_blocked(markdown: str) -> bool:
    """Heurystyka: markdown to głównie baner cookie, brak treści katalogu."""
    if not markdown or len(markdown.strip()) < 8000:
        return True
    head = markdown[:3000]
    if COOKIE_BANNER_PATTERN.search(head) and not CATALOG_CONTENT_PATTERN.search(markdown):
        return True
    return False


def extract_with_llm(text: str) -> KierunekStudiow:
    prompt = (
        "Jesteś precyzyjnym ekstraherem danych o kierunkach studiów na polskich uczelniach.\n\n"
        "## ZASADA NADRZĘDNA\n"
        "Zwracasz ZAWSZE JEDEN obiekt JSON. Nigdy tablicę. "
        "Jeśli strona opisuje studia I i II stopnia jednocześnie — wpisz oba stopnie do pola "
        "'specjalizacje' z dopiskiem, np. 'Zarządzanie (I stopień)', 'Zarządzanie (II stopień)'. "
        "Nie twórz sztucznych podziałów — jeden URL to jeden obiekt.\n\n"
        "## POLE 'kierunek' — BEZ TŁUMACZENIA\n"
        "Przepisz oficjalną nazwę kierunku DOKŁADNIE tak, jak występuje na TEJ stronie — "
        "w oryginalnym języku. KATEGORYCZNY ZAKAZ tłumaczenia na polski lub na angielski.\n"
        "  ✓ Strona ma 'Bachelor Management' → 'Bachelor Management' (NIE 'Zarządzanie')\n"
        "  ✓ Strona ma 'Informatyka' → 'Informatyka' (NIE 'Computer Science')\n"
        "  ✓ Strona ma 'Business and English-Language Studies in Economics' → przepisz dosłownie\n"
        "Źródła (w tej kolejności): główny nagłówek H1 strony → podtytuł oferty → tytuł HTML przed ' | '.\n"
        "NIE bierz nazwy z menu, breadcrumbs ani linków nawigacyjnych — mogą być w innym języku.\n"
        "BEZWZGLĘDNIE ZABRONIAJ: tłumaczenia nazwy, etykiet systemowych ('realizowany bez podziału...'), "
        "opisów wariantów ('specjalność: ...'), komunikatów administracyjnych.\n"
        "Jeśli brak wyraźnego nagłówka (np. USOS): tabela 'Nazwa programu' / 'Dyscyplina', potem breadcrumbs.\n"
        "Kody wariantów (-bpns, -nst) ignoruj — użyj nazwy programu nadrzędnego.\n\n"
        "## POLE 'stopien'\n"
        "Jedna z wartości: 1_stopnia / 2_stopnia / jednolite_magisterskie.\n"
        "Jeśli strona ma oba stopnia — wybierz stopień dominujący (np. ten z tytułem sekcji).\n\n"
        "## POLE 'tytul'\n"
        "Wynika ze stopnia i profilu: licencjat lub inżynier (1_stopnia), "
        "magister (2_stopnia / jednolite_magisterskie).\n\n"
        "## POLE 'tryb'\n"
        "Tryby WYŁĄCZNIE tej oferty. Źródło: podtytuł lub nagłówek bezpośrednio pod nazwą kierunku. "
        "Jeden tryb → tablica z jednym elementem. Dwa tryby tylko gdy strona wprost stwierdza "
        "dostępność w obu dla TEJ SAMEJ oferty. NIE wyciągaj ze stopek ani sekcji kontaktowych.\n\n"
        "## POLE 'semestry'\n"
        "Szukaj AKTYWNIE w całym tekście: 'N semestrów', 'N lat' (pomnóż × 2), "
        "'czas trwania: N', tabela planu studiów (policz semestry). "
        "Przykłady przeliczenia: '3 lata' → 6, '3,5 roku' → 7, '2 lata' → 4. "
        "Tylko jeśli strona absolutnie nic nie podaje — użyj typowych wartości jako ostatni resort: "
        "licencjat=6, inżynier=7, magister (II st.)=4, jednolite=10.\n\n"
        "## POLE 'wydzial'\n"
        "Pełna nazwa wydziału prowadzącego kierunek. Null jeśli brak.\n\n"
        "## POLE 'jezyk'\n"
        "Język wykładowy programu opisany na stronie. 'angielski' jeśli oferta jest po angielsku "
        "(np. 'studies in English', angielski nagłówek kierunku). Domyślnie 'polski'.\n\n"
        "## POLE 'specjalizacje'\n"
        "Lista specjalizacji w ORYGINALNYM brzmieniu ze strony — bez tłumaczenia. "
        "Pusta tablica [] jeśli brak.\n\n"
        "## POLE 'rekrutacja'\n"
        "Lista konkretnych przedmiotów maturalnych (np. 'matematyka', 'fizyka', 'język obcy'). "
        "Pusta tablica [] jeśli brak.\n\n"
        "## POLE 'czesne' — KATEGORYCZNY ZAKAZ ZMYŚLANIA\n"
        "Podaj opłaty TYLKO jeśli konkretne kwoty fizycznie znajdują się w dostarczonym tekście. "
        "Jeśli tekst nie zawiera żadnej kwoty — zwróć null, nie zgaduj.\n"
        "Jeśli kwoty są — każdy wiersz tabeli to osobny obiekt {wariant, kwota}:\n"
        "  wariant: opis (np. 'stacjonarne', 'niestacjonarne', 'I stopień stacjonarne')\n"
        "  kwota: przepisz DOSŁOWNIE z tekstu (np. '2400 zł/semestr')\n"
        "NIGDY nie uśredniaj, nie interpretuj, nie wymyślaj — tylko to co jest w tekście.\n\n"
        "## POLE 'opis'\n"
        "2-3 zdania o profilu, charakterze i perspektywach — wyłącznie na podstawie tekstu.\n\n"
        "Ignoruj nawigację, stopki, bannery cookie i reklamy.\n"
        "Zwróć WYŁĄCZNIE poprawny obiekt JSON. Żadnych komentarzy ani dodatkowego tekstu.\n\n"
        f"Tekst strony:\n{text}"
    )
    response_text = call_gemini_with_retry(
        prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": KierunekStudiow,
            "temperature": 0.0,
        },
    )
    return KierunekStudiow.model_validate_json(response_text)


async def get_course_links(url: str, crawler: AsyncWebCrawler) -> List[str]:
    print(f"🔍 Pobieram katalog kierunków: {url}")
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=5,
        delay_before_return_html=4.0,
        magic=True,
        remove_consent_popups=True,
        js_code_before_wait=CATALOG_JS_PREP,
        js_code=CATALOG_JS_SCROLL,
    )
    result = await crawler.arun(url=url, config=run_config)
    if not result.success:
        print(f"❌ Nie udało się pobrać katalogu: {result.error_message}")
        return []

    md = result.markdown or ""
    print(f"📄 Markdown ze strony: {len(md)} znaków")
    print(f"📄 Podgląd (pierwsze 500 znaków):\n{md[:500]}\n---")
    if _catalog_markdown_looks_js_blocked(md):
        print(
            "⚠️ Markdown katalogu wygląda na pusty lub zablokowany (cookie/JS). "
            "Treść kierunków mogła się nie załadować."
        )

    prompt = (
        f"Analizujesz stronę katalogu: {url}\n\n"
        "Jesteś rygorystycznym filtrem linków dla agregatora kierunków studiów. "
        "Twoim JEDYNYM zadaniem jest zwrócić tablicę JSON z linkami prowadzącymi "
        "WYŁĄCZNIE do stron ze szczegółowym opisem KONKRETNEGO, nazwanego kierunku studiów "
        "(np. Informatyka, Budownictwo, Automatyka i Robotyka, Zarządzanie). "
        "Linki mogą być bezwzględne (https://...) lub względne (/studia/informatyka).\n\n"
        "KATEGORYCZNIE ODRZUĆ i NIE zwracaj linków do:\n"
        "- nawigacji, menu, stopki, breadcrumbs, map strony\n"
        "- strony głównej uczelni lub wydziału\n"
        "- kontaktu, o uczelni, historii, aktualności, wydarzeń, galerii\n"
        "- rekrutacji ogólnej, zasad przyjęć, terminów, harmonogramów\n"
        "- psychotestów, ankiet, open day, dni otwartych\n"
        "- studiów podyplomowych, MBA, kursów dokształcających, certyfikatów\n"
        "- profili wydziałów lub katedr bez konkretnego kierunku\n"
        "- mediów społecznościowych, plików PDF, obrazków, mailto:, tel:\n"
        "- wyszukiwarki, logowania, panelu studenta\n\n"
        "ZASADA: Jeśli link NIE prowadzi ewidentnie do strony opisującej JEDEN, "
        "konkretny, nazwany kierunek studiów — bezwzględnie go pomiń. "
        "Lepiej zwrócić mniej linków niż jeden śmieciowy.\n\n"
        'Odpowiedz TYLKO i WYŁĄCZNIE tablicą JSON, np.: ["https://...", "/inny-kierunek"]\n'
        "Żadnych komentarzy, wyjaśnień ani dodatkowego tekstu.\n\n"
        f"Tekst strony:\n{md}"
    )
    response_text = await asyncio.to_thread(
        call_gemini_with_retry,
        prompt,
        {"response_mime_type": "application/json", "temperature": 0.0},
    )
    print(f"🤖 Surowa odpowiedź Gemini: {response_text!r}")
    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError:
        parsed = []
    links: List[str] = parsed if isinstance(parsed, list) else parsed.get("links", [])
    raw_count = len(links)
    links = [
        urljoin(url, u) for u in links
        if isinstance(u, str) and u.strip()
    ]
    links = [
        u for u in links
        if u.startswith(("http://", "https://")) and not _is_junk_url(u)
    ]
    links = list(dict.fromkeys(links))
    if raw_count and not links:
        print(f"⚠️ Gemini zwrócił {raw_count} linków, ale post-filter odrzucił wszystkie.")
    if not links and _catalog_markdown_looks_js_blocked(md):
        print(
            "⚠️ 0 linków — prawdopodobnie katalog ładuje kierunki przez JavaScript/AJAX "
            "(np. WordPress + filtr). Sprawdź, czy w markdownie widać karty kierunków."
        )
    print(f"✅ Znaleziono {len(links)} linków do kierunków.")
    return links

# ── Endpointy ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/scrape", response_model=ScrapeResponse)
async def run_spider(request: ScrapeRequest):
    catalog_url = str(request.url)
    print(f"🤖 Gemini model: {GEMINI_MODEL}")
    results: List[ScrapeResultItem] = []
    links: List[str] = []

    browser_config = BrowserConfig(headless=True, java_script_enabled=True)
    course_run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=10,
        delay_before_return_html=5.0,
        magic=True,
        excluded_tags=["nav", "footer", "header", "script", "style", "form"],
        exclude_external_links=True,
    )

    crawler = AsyncWebCrawler(config=browser_config)
    await crawler.start()
    try:
        links = await get_course_links(catalog_url, crawler)
        if not links:
            raise HTTPException(status_code=404, detail="Nie znaleziono linków do kierunków.")

        for i, url in enumerate(links[:MAX_COURSES_PER_RUN], 1):
            print(f"[{i}/{MAX_COURSES_PER_RUN}] Przetwarzam: {url}")
            result = await crawler.arun(url=url, config=course_run_config)

            if not result.success:
                print(f"❌ [{i}/{MAX_COURSES_PER_RUN}] Błąd scrapowania: {result.error_message}")
                results.append(ScrapeResultItem(url=url, error=result.error_message))
                continue

            md = result.markdown or ""
            print(f"  📄 Markdown: {len(md)} znaków")
            try:
                kierunek = await asyncio.to_thread(extract_with_llm, md)
                results.append(ScrapeResultItem(url=url, data=kierunek))
                print(f"✅ [{i}/{MAX_COURSES_PER_RUN}] {kierunek.kierunek} | {kierunek.stopien} | {'/'.join(kierunek.tryb)}")
            except GeminiQuotaError as e:
                print(f"❌ [{i}/{MAX_COURSES_PER_RUN}] Limit API Gemini: {e}")
                results.append(ScrapeResultItem(url=url, error=str(e)))
            except Exception as e:
                print(f"❌ [{i}/{MAX_COURSES_PER_RUN}] Błąd ekstrakcji: {e}")
                results.append(ScrapeResultItem(url=url, error=str(e)))
    except GeminiQuotaError as e:
        raise HTTPException(status_code=429, detail=str(e))
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