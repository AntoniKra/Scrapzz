import os
import json
import asyncio
import sys
import time
import re
import threading
from pathlib import Path
from typing import List, Optional, Literal
from urllib.parse import urlparse, urljoin, parse_qsl, urlencode, urlunparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

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
    warnings: List[str] = Field(default_factory=list)

class ScrapeResponse(BaseModel):
    status: str
    links_found: int
    links_processed: int
    results: List[ScrapeResultItem]


# ── Konfiguracja Gemini ───────────────────────────────────────────────────────

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_MIN_INTERVAL = 4.0
MAX_COURSES_PER_RUN = 10

TRACKING_QUERY_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "gbraid", "wbraid", "fbclid", "_gl", "_ga",
})


def _strip_tracking_params(url: str) -> str:
    """Usuwa znane parametry trackingowe z URL katalogu przed crawlem."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in TRACKING_QUERY_PARAMS]
    new_query = urlencode(kept)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


_gemini_client: Optional[genai.Client] = None
_last_gemini_call = 0.0
_gemini_lock = threading.Lock()


class GeminiNotConfiguredError(RuntimeError):
    """Brak GEMINI_API_KEY — scrapowanie wymaga klucza w .env."""


class GeminiQuotaError(RuntimeError):
    """Wyczerpany limit API Gemini — nie retryuj w nieskończoność."""


def _gemini_api_key_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        if not _gemini_api_key_configured():
            raise GeminiNotConfiguredError(
                "Brak GEMINI_API_KEY w pliku .env w katalogu głównym projektu."
            )
        _gemini_client = genai.Client()
    return _gemini_client


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
                response = _get_gemini_client().models.generate_content(
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
    r"(oferta-studiow|/kierunek/|pokazKierunek|/studia/|/program/|/oferta/studia|"
    r"studia-i-stopnia/|studia-ii-stopnia/|/kierunki-studiow/[^/?#]+)",
    re.I,
)

# Strony katalogu / paginacji — nie strony konkretnego kierunku.
CATALOG_PAGE_PATTERN = re.compile(
    r"(oferta-studiow/strona(?:-\d+)?/?(?:\?|$)|"
    r"(?:^|/)studia-i-stopnia/?(?:\?|#|$)|"
    r"(?:^|/)studia-ii-stopnia/?(?:\?|#|$)|"
    r"(?:^|/)strona-\d+/?(?:\?|#|$)|"
    r"(?:^|/)page/\d+/?(?:\?|#|$)|"
    r"(?:^|/)kierunki-studiow/?(?:\?|#|$))",
    re.I,
)


def _normalize_url_for_compare(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc.lower()}{path.lower()}"


def _is_junk_url(url: str) -> bool:
    """Filtruje śmieciowe ścieżki — nie sprawdza hosta (np. rekrutacja.p.lodz.pl)."""
    parsed = urlparse(url)
    haystack = f"{parsed.path}?{parsed.query}".lower()
    if COURSE_PATH_PATTERN.search(haystack):
        return False
    return bool(JUNK_URL_PATTERN.search(haystack))


def _is_catalog_list_url(url: str) -> bool:
    """True dla stron listy kierunków / paginacji, nie pojedynczego kierunku."""
    parsed = urlparse(url)
    haystack = f"{parsed.path}?{parsed.query}"
    return bool(CATALOG_PAGE_PATTERN.search(haystack))


def _is_course_detail_url(url: str, catalog_url: Optional[str] = None) -> bool:
    """Hybrid: odrzuć śmieci i katalogi; resztę (Gemini/HTML) wpuszczaj bez sztywnej whitelist."""
    if _is_junk_url(url):
        return False
    if _is_catalog_list_url(url):
        return False
    if catalog_url and _normalize_url_for_compare(url) == _normalize_url_for_compare(catalog_url):
        return False
    return True


COOKIE_BANNER_PATTERN = re.compile(
    r"(cookiebot|cookieyes|plików cookie|pliki cookie|akceptuj wszystko|"
    r"accept all|consent|cybotcookiebotdialog|omcookie)",
    re.I,
)
CATALOG_CONTENT_PATTERN = re.compile(
    r"(card_post|filter-results|kierunek studiów|/oferta/studia-|/kierunek/|"
    r"oferta-studiow/[^/\s]+|invulstructure|studia-i-stopnia/|/kierunki-studiow/[^)\s\"'<]+)",
    re.I,
)

# Cookie + czekanie na treść katalogu (WordPress, TYPO3/UŁ, ATA) + zamknięcie menu mobilnego.
CATALOG_JS_PREP = """
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const acceptCookies = () => {
    const selectors = [
      '[data-omcookie-panel-save="all"]',
      '.cookie-panel__button--color--green',
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
      if (/akceptuj wszystkie|akceptuj|accept all|zgoda|^accept$/i.test(t)) { el.click(); return true; }
    }
    return false;
  };
  const closeMobileMenu = () => {
    for (const el of document.querySelectorAll('button, a, [role="button"]')) {
      const t = (el.textContent || '').trim();
      if (/^zamknij$/i.test(t)) { el.click(); return true; }
    }
    return false;
  };
  const catalogReady = () => {
    if (document.querySelector('.card_post_item')) return true;
    if (document.querySelector('#filter-results a[href*="/oferta/"]')) return true;
    const anchors = document.querySelectorAll(
      'a[href*="/rekrutacja/oferta-studiow/"], a[href*="/oferta-studiow/"], a[href*="/kierunek/"]'
    );
    for (const a of anchors) {
      const href = a.getAttribute('href') || '';
      if (/oferta-studiow\\/strona/i.test(href)) continue;
      if (/oferta-studiow\\/.+/i.test(href) || /\\/kierunek\\//i.test(href)) return true;
    }
    // Vistula: kierunki są pod /kierunki-studiow/<slug>
    for (const a of document.querySelectorAll('a[href*="/kierunki-studiow/"]')) {
      const href = a.getAttribute('href') || '';
      if (/\\/kierunki-studiow\\/[^/?#]+/i.test(href)) return true;
    }
    return false;
  };
  acceptCookies();
  await sleep(800);
  acceptCookies();
  closeMobileMenu();
  await sleep(400);
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    if (catalogReady()) break;
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

# Strony kierunków: cookie + czekanie na AJAX cennika (ideis: .all-tuitions) + przełączenie lat.
COURSE_JS_PREP = """
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const acceptCookies = () => {
    const selectors = [
      '[data-omcookie-panel-save="all"]',
      '.cookie-panel__button--color--green',
      '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
      '#CybotCookiebotDialogBodyButtonAccept',
      'button[id*="accept"]',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) { el.click(); return true; }
    }
    for (const el of document.querySelectorAll('button, a, [role="button"]')) {
      const t = (el.textContent || '').trim().toLowerCase();
      if (/akceptuj wszystkie|akceptuj|accept all|zgoda|^accept$/i.test(t)) { el.click(); return true; }
    }
    return false;
  };
  const feeHasAmounts = (root) => {
    const text = (root?.textContent || '').replace(/\\s+/g, ' ').trim();
    return text.length > 30 && /\\d+\\s*zł/i.test(text);
  };
  const feesReady = () => {
    for (const b of document.querySelectorAll('.all-tuitions, .mode-tab, [class*="tuition"]')) {
      if (feeHasAmounts(b)) return true;
    }
    const body = document.body?.innerText || '';
    return /czesne/i.test(body) && /\\d+\\s*zł/i.test(body);
  };
  acceptCookies();
  await sleep(600);
  acceptCookies();
  for (const sel of document.querySelectorAll('.mode-tab select')) {
    try {
      for (const opt of sel.options) {
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        await sleep(350);
      }
    } catch (_) {}
  }
  const anchor = document.querySelector('.all-tuitions, .mode-tab, [class*="tuition"]');
  if (anchor) anchor.scrollIntoView({ behavior: 'instant', block: 'center' });
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    if (feesReady()) break;
    await sleep(350);
  }
  await sleep(400);
})();
"""

COURSE_JS_SCROLL = """
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const h = document.body?.scrollHeight || 0;
  for (const y of [0, h * 0.35, h * 0.55, h * 0.75, h]) {
    window.scrollTo(0, y);
    await sleep(450);
  }
  const anchor = document.querySelector('.all-tuitions, .mode-tab');
  if (anchor) anchor.scrollIntoView({ behavior: 'instant', block: 'center' });
  await sleep(300);
})();
"""


def _extract_course_links_from_crawl(result, base_url: str, catalog_url: Optional[str] = None) -> List[str]:
    """Fallback: linki kierunków z HTML/crawl4ai links, gdy markdown/Gemini zawiodą."""
    catalog_url = catalog_url or base_url
    candidates: List[str] = []
    links_obj = getattr(result, "links", None) or {}
    if isinstance(links_obj, dict):
        for bucket in ("internal", "external"):
            for item in links_obj.get(bucket, []) or []:
                href = item.get("href") if isinstance(item, dict) else str(item)
                if href:
                    candidates.append(urljoin(base_url, href))

    for html_attr in ("html", "cleaned_html", "fit_html"):
        html = getattr(result, html_attr, None) or ""
        if not html:
            continue
        for match in re.finditer(r"""href=["']([^"']+)["']""", html):
            candidates.append(urljoin(base_url, match.group(1)))

    out: List[str] = []
    for candidate in candidates:
        if not candidate.startswith(("http://", "https://")):
            continue
        if _is_course_detail_url(candidate, catalog_url=catalog_url):
            out.append(candidate)
    return list(dict.fromkeys(out))


def _catalog_markdown_looks_js_blocked(markdown: str) -> bool:
    """Heurystyka: markdown to głównie baner cookie, brak treści katalogu."""
    if not markdown or not markdown.strip():
        return True
    if CATALOG_CONTENT_PATTERN.search(markdown):
        return False
    if len(markdown.strip()) < 8000:
        return True
    head = markdown[:3000]
    if COOKIE_BANNER_PATTERN.search(head):
        return True
    return False


# Akapity PR wydziału (rankingi, nagrody) — wycinane przed ekstrakcją; zawór przy utracie >15% tekstu.
FACULTY_PROMO_PARAGRAPH_PATTERN = re.compile(
    r"(wyróżnienie\s+w\s+rankingu|builder\s+ranking|"
    r"otrzymał[ao]?\s+(?:to\s+)?(?:prestiżowe\s+)?wyróżnienie|"
    r"dziekan\s+wydziału|jako\s+jedyn[yae]\s+spośród\s+uczelni)",
    re.I,
)
PROMO_FILTER_MIN_RETAINED_RATIO = 0.85
# Przy filtrze linii — akapit z crawl4ai bywa ogromny; ratio nie ma sensu, pilnujemy markerów treści.
PROMO_CONTENT_MARKERS = ("zł", "semestr", "czas trwania", "opłat", "stacjonarn", "niestacjonarn", "licencjat", "inżynier")

OPIS_PROMO_WARNING_PATTERN = re.compile(
    r"(builder\s+ranking|wyróżnienie\s+w\s+rankingu|dziekan\s+wydziału)",
    re.I,
)


def _markdown_lost_critical_content(original: str, filtered: str) -> bool:
    """True gdy po filtrze zniknęły fragmenty ważne dla czesne/semestry/tryb."""
    orig = original.lower()
    filt = filtered.lower()
    for marker in PROMO_CONTENT_MARKERS:
        if marker in orig and marker not in filt:
            return True
    return False


def _strip_promo_from_line(line: str) -> tuple[str, int]:
    """Usuwa linię lub pojedyncze zdania z PR — nie cały długi blok markdownu."""
    if not FACULTY_PROMO_PARAGRAPH_PATTERN.search(line):
        return line, 0
    if len(line) <= 500:
        return "", 1
    sentences = re.split(r"(?<=[.!?…])\s+", line)
    kept = [s for s in sentences if s and not FACULTY_PROMO_PARAGRAPH_PATTERN.search(s)]
    removed = len(sentences) - len(kept)
    if not kept:
        return "", max(removed, 1)
    return " ".join(kept), removed


def prepare_markdown_for_extraction(markdown: str) -> str:
    """Usuwa linie/zdania z oczywistym PR wydziału; przy utracie kluczowej treści zwraca oryginał."""
    if not markdown:
        return markdown

    original_len = len(markdown)
    kept: List[str] = []
    removed = 0
    for line in markdown.split("\n"):
        cleaned, n = _strip_promo_from_line(line)
        removed += n
        if cleaned:
            kept.append(cleaned)

    if not removed:
        return markdown

    filtered = "\n".join(kept)
    if _markdown_lost_critical_content(markdown, filtered):
        print(
            f"⚠️ Filtr PR wydziału usunął {removed} linii, ale zniknęły dane oferty "
            "— używam oryginalnego markdownu."
        )
        return markdown

    retained_ratio = len(filtered) / original_len if original_len else 1.0
    if retained_ratio < PROMO_FILTER_MIN_RETAINED_RATIO:
        print(
            f"⚠️ Filtr PR wydziału usunął {removed} linii, ale zostawił "
            f"{retained_ratio:.0%} tekstu — używam oryginalnego markdownu."
        )
        return markdown

    print(
        f"ℹ️ Filtr PR wydziału: usunięto {removed} linii "
        f"({original_len} → {len(filtered)} znaków)."
    )
    return filtered


def _opis_promo_warning(kierunek: KierunekStudiow, url: str) -> Optional[str]:
    if OPIS_PROMO_WARNING_PATTERN.search(kierunek.opis):
        print(f"⚠️ Pole 'opis' może zawierać treść PR wydziału (sprawdź ręcznie): {url}")
        return "opis_moze_zawierac_pr_wydzialu"
    return None


FEE_SIGNAL_PATTERN = re.compile(
    r"(czesne|opłat|cena za rok|\b\d+\s*rat\b|/semestr)",
    re.I,
)
FEE_AMOUNT_PATTERN = re.compile(r"\d[\d\s,.]*\s*zł", re.I)


def _text_has_fee_signals(text: str) -> bool:
    if not text:
        return False
    return bool(FEE_AMOUNT_PATTERN.search(text) and FEE_SIGNAL_PATTERN.search(text))


def _html_to_plain_fragment(html_fragment: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html_fragment, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_fee_sections_from_html(html: str) -> str:
    """Wyciąga tekst cennika z HTML, gdy markdown go pomija (np. karuzele AJAX ideis)."""
    if not html:
        return ""

    chunks: List[str] = []
    for match in re.finditer(
        r'class="all-tuitions[^"]*"[^>]*data-year="(\d+)"[^>]*>(.*?)</div>\s*</div>',
        html,
        re.S | re.I,
    ):
        plain = _html_to_plain_fragment(match.group(2))
        if _text_has_fee_signals(plain):
            chunks.append(f"[Opłaty rok {match.group(1)}] {plain[:1200]}")

    for match in re.finditer(r'class="mode-tab"[^>]*>(.*?)</div>\s*</div>\s*</div>', html, re.S | re.I):
        plain = _html_to_plain_fragment(match.group(1))
        if _text_has_fee_signals(plain) and len(plain) > 40:
            chunks.append(plain[:1500])

    for match in re.finditer(r'class="bottom__prices"[^>]*>(.*?)</div>', html, re.S | re.I):
        plain = _html_to_plain_fragment(match.group(1))
        if plain and FEE_AMOUNT_PATTERN.search(plain):
            chunks.append(f"[Cena od] {plain}")

    if not chunks:
        for match in re.finditer(r".{0,40}(czesne|cennik|opłaty).{0,2500}", html, re.I | re.S):
            plain = _html_to_plain_fragment(match.group())
            if _text_has_fee_signals(plain):
                chunks.append(plain[:1500])
                break

    if not chunks:
        return ""
    return "## Sekcja opłat (z HTML strony)\n" + "\n\n".join(dict.fromkeys(chunks))


def augment_markdown_with_fee_content(markdown: str, html: str) -> str:
    """Dokleja cennik z HTML, gdy markdown nie zawiera kwot (typowe dla ideis / sliderów)."""
    if _text_has_fee_signals(markdown):
        return markdown
    snippet = _extract_fee_sections_from_html(html)
    if not snippet:
        return markdown
    print("ℹ️ Markdown bez opłat — doklejam sekcję cennika z HTML.")
    return f"{markdown.rstrip()}\n\n---\n{snippet}\n"


def _parse_ideis_tuition_block(plain: str, mode_label: str, year: str) -> Optional[CzesneEntry]:
    roczna = re.search(
        r"1 rata\s+(\d[\d\s]*)\s*zł[^0-9]{0,120}Cena za rok:\s*(\d[\d\s]*)\s*zł",
        plain,
        re.I,
    )
    if roczna:
        amount = re.sub(r"\s", "", roczna.group(2))
        wariant = f"{mode_label} rok {year}".strip()
        return CzesneEntry(wariant=wariant[:100], kwota=f"{amount} zł/rok")

    if re.search(r"\b1 rata\b", plain, re.I):
        rok = re.search(r"Cena za rok:\s*(\d[\d\s]*)\s*zł", plain, re.I)
        if rok:
            amount = re.sub(r"\s", "", rok.group(1))
            wariant = f"{mode_label} rok {year}".strip()
            return CzesneEntry(wariant=wariant[:100], kwota=f"{amount} zł/rok")

    low = re.search(r"Najniższa cena z ostatnich 30 dni:\s*(\d[\d\s]*)\s*zł", plain, re.I)
    if low:
        amount = re.sub(r"\s", "", low.group(1))
        try:
            if int(amount) > 2500:
                return None
        except ValueError:
            pass
        wariant = f"{mode_label} rok {year}".strip()
        return CzesneEntry(wariant=wariant[:100], kwota=f"{amount} zł/mies")
    return None


def extract_czesne_from_html(html: str) -> Optional[List[CzesneEntry]]:
    """Deterministyczny fallback cennika — ideis (.all-tuitions) i ogólne sekcje opłat."""
    if not html or not _text_has_fee_signals(html):
        return None

    entries: List[CzesneEntry] = []
    seen_keys: set[tuple[str, str]] = set()

    for tab_match in re.finditer(
        r'class="mode-tab"[^>]*>(.*?)(?=class="mode-tab"|class="bottom__prices"|\Z)',
        html,
        re.S | re.I,
    ):
        tab_html = tab_match.group(1)
        title_match = re.search(
            r'mode-tab__mode-title[^>]*>.*?<span>([^<]+)</span>',
            tab_html,
            re.S | re.I,
        )
        mode_label = _html_to_plain_fragment(title_match.group(1)) if title_match else "Opłaty"
        for tu_match in re.finditer(
            r'class="all-tuitions[^"]*"[^>]*data-year="(\d+)"[^>]*>(.*?)</div>\s*</div>',
            tab_html,
            re.S | re.I,
        ):
            plain = _html_to_plain_fragment(tu_match.group(2))
            entry = _parse_ideis_tuition_block(plain, mode_label, tu_match.group(1))
            if not entry:
                continue
            key = (entry.wariant.strip(), entry.kwota.strip())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            entries.append(entry)

    if not entries:
        snippet = _extract_fee_sections_from_html(html)
        if snippet:
            for line in snippet.splitlines():
                low = re.search(r"Najniższa cena z ostatnich 30 dni:\s*(\d[\d\s]*)\s*zł", line, re.I)
                rok = re.search(r"Cena za rok:\s*(\d[\d\s]*)\s*zł", line, re.I)
                year_match = re.search(r"\[Opłaty rok (\d+)\]", line)
                wariant = f"Opłaty rok {year_match.group(1)}" if year_match else "Opłaty"
                if rok:
                    amount = re.sub(r"\s", "", rok.group(1))
                    entry = CzesneEntry(wariant=wariant, kwota=f"{amount} zł/rok")
                elif low:
                    amount = re.sub(r"\s", "", low.group(1))
                    entry = CzesneEntry(wariant=wariant, kwota=f"{amount} zł/mies")
                else:
                    continue
                key = (entry.wariant.strip(), entry.kwota.strip())
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                entries.append(entry)

    return entries[:10] if entries else None


INSTALLMENT_CZESNE_PATTERN = re.compile(
    r"(rata|ratal|płatność w \d+ rat|"
    r"\d+\s*[x×]\s*\d|"
    r"miesięczn|"
    r"za rok akademicki|opłata roczna|rocznie)",
    re.I,
)
SEMESTER_WARIANT_PATTERN = re.compile(
    r"(za semestr|semestral|/semestr|płatność za semestr)",
    re.I,
)
HAS_SEMESTER_UNIT_PATTERN = re.compile(r"/\s*semestr|za\s+semestr", re.I)


def _is_installment_czesne_entry(entry: CzesneEntry) -> bool:
    return bool(INSTALLMENT_CZESNE_PATTERN.search(f"{entry.wariant} {entry.kwota}"))


def _ensure_semester_unit(entry: CzesneEntry) -> CzesneEntry:
    kwota = entry.kwota.strip()
    if HAS_SEMESTER_UNIT_PATTERN.search(kwota) or not re.search(r"\d", kwota):
        return entry
    if SEMESTER_WARIANT_PATTERN.search(entry.wariant) and re.search(r"zł", kwota, re.I):
        kwota_clean = kwota.rstrip(".")
        return CzesneEntry(wariant=entry.wariant, kwota=f"{kwota_clean}/semestr")
    return entry


def normalize_czesne(entries: Optional[List[CzesneEntry]]) -> Optional[List[CzesneEntry]]:
    """Usuwa raty, deduplikuje wpisy, uzupełnia /semestr — przy pustej liście zwraca oryginał."""
    if not entries:
        return entries

    original_count = len(entries)
    filtered = [e for e in entries if not _is_installment_czesne_entry(e)]
    if not filtered:
        filtered = list(entries)

    seen: set[tuple[str, str]] = set()
    deduped: List[CzesneEntry] = []
    for entry in filtered:
        key = (entry.wariant.strip(), entry.kwota.strip())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(_ensure_semester_unit(entry))

    if len(deduped) < original_count:
        print(f"ℹ️ Czesne: {original_count} → {len(deduped)} wpisów po normalizacji.")

    return deduped if deduped else entries


OFFER_ZONE_CONTACT_PATTERN = re.compile(
    r"kontakt dla kandydata|podkomisji rekrutacyjnej|kontakt rekrutacyjny",
    re.I,
)
TRYB_STACJONARNE_PATTERN = re.compile(r"stacjonarn", re.I)
TRYB_NIESTACJONARNE_PATTERN = re.compile(r"niestacjonarn|zaoczn|wieczorow", re.I)
OFFER_ZONE_MAX_CHARS = 6000


def _offer_zone_markdown(markdown: str) -> str:
    """Górna część strony — opis oferty, przed sekcją kontaktu rekrutacyjnego wydziału."""
    match = OFFER_ZONE_CONTACT_PATTERN.search(markdown)
    if match:
        return markdown[: match.start()]
    return markdown[:OFFER_ZONE_MAX_CHARS]


def normalize_tryb(
    tryb: List[Literal["stacjonarne", "niestacjonarne"]],
    markdown: str,
) -> tuple[List[Literal["stacjonarne", "niestacjonarne"]], Optional[str]]:
    """Koryguje tryb tylko gdy LLM dodał drugi tryb z kontaktu, a nagłówek wskazuje jeden."""
    if len(tryb) <= 1:
        return tryb, None

    zone = _offer_zone_markdown(markdown)
    has_stacjonarne = bool(TRYB_STACJONARNE_PATTERN.search(zone))
    has_niestacjonarne = bool(TRYB_NIESTACJONARNE_PATTERN.search(zone))

    if has_stacjonarne and not has_niestacjonarne:
        print("ℹ️ Tryb: skorygowano do stacjonarne (strefa oferty wskazuje jeden tryb).")
        return ["stacjonarne"], "tryb_skorygowany_w_normalizacji"
    if has_niestacjonarne and not has_stacjonarne:
        print("ℹ️ Tryb: skorygowano do niestacjonarne (strefa oferty wskazuje jeden tryb).")
        return ["niestacjonarne"], "tryb_skorygowany_w_normalizacji"
    return tryb, None


def extract_with_llm(text: str) -> tuple[KierunekStudiow, List[str]]:
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
        "Tryby WYŁĄCZNIE tej konkretnej oferty (tego URL) — nie całego wydziału ani uczelni.\n"
        "Priorytet źródeł: linia bezpośrednio pod H1 / nazwą kierunku (np. 'Stacjonarne Studia I stopnia…') → "
        "metadane oferty tuż pod tytułem (forma studiów, tryb).\n"
        "Jeden tryb w nagłówku oferty → tablica z jednym elementem, nawet gdy niżej w kontakcie wydziału "
        "wspomniano inny tryb.\n"
        "Dwa tryby tylko gdy w strefie oferty (pod H1) wprost widać oba dla TEJ SAMEJ strony.\n"
        "KATEGORYCZNIE NIE używaj do trybu: sekcji 'Kontakt dla kandydata', podkomisji rekrutacyjnych, "
        "e-maili/telefonów rekrutacji, stopek wydziału, legend filtrów katalogu.\n"
        "Mapowanie: 'zaoczne', 'wieczorowe' → niestacjonarne.\n\n"
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
        "Źródło: wyłącznie sekcja cennika / opłat za TEN kierunek. "
        "Ignoruj stypendia, ulgi, opłaty rekrutacyjne i wpisowe (chyba że to jedyna kwota).\n\n"
        "NORMALIZACJA (bez zmiany liczb, bez uśredniania):\n"
        "- Preferuj opłatę za SEMESTR ('NNNN zł/semestr'), gdy strona podaje ją wprost.\n"
        "- Gdy strona ma TYLKO raty miesięczne lub roczne — wpisz je dosłownie (np. '599 zł/mies', "
        "'6920 zł/rok'). NIE zwracaj null tylko dlatego, że brak etykiety 'za semestr'.\n"
        "- Pomiń pełną macierz rat (12 rat, 10 rat, …) — max 1 reprezentatywny wariant na tryb×rok "
        "(np. 'Najniższa cena z ostatnich 30 dni' albo '1 rata' / 'Cena za rok').\n"
        "- wariant: krótko, np. 'stacjonarne I rok', 'Czesne równe rok 2' — bez kopiowania całej etykiety.\n"
        "- Gdy tabela ma podział na lata (I rok, II rok…): jeden wpis na rok × tryb, gdy kwoty się różnią.\n"
        "- Cel: zwykle 2–10 sensownych wpisów.\n"
        "NIGDY nie uśredniaj, nie przeliczaj rat na semestr, nie wymyślaj — tylko to co jest w tekście.\n\n"
        "## POLE 'opis'\n"
        "2-3 zdania o profilu PROGRAMU studiów: czego się uczysz, jaki charakter ma kształcenie, "
        "perspektywy zawodowe — wyłącznie na podstawie tekstu strony.\n"
        "Priorytet źródeł: wstęp pod nagłówkiem H1 → sekcje Program / profil kierunku / "
        "'Ten program jest dla Ciebie' / 'Co możesz robić po studiach'.\n"
        "KATEGORYCZNIE NIE używaj do opisu: rankingów uczelni, nagród i wyróżnień wydziału, "
        "aktualności, cytatów dziekana, karuzel 'dlaczego warto', jeśli dotyczą prestiżu "
        "uczelni/wydziału, a nie treści programu.\n\n"
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
    warnings: List[str] = []
    kierunek = KierunekStudiow.model_validate_json(response_text)
    kierunek.czesne = normalize_czesne(kierunek.czesne)
    kierunek.tryb, tryb_warning = normalize_tryb(kierunek.tryb, text)
    if tryb_warning:
        warnings.append(tryb_warning)
    return kierunek, warnings


_HUB_LINK_TEXTS = re.compile(
    r"(zobacz wszystkie dostępne kierunki|wszystkie dostępne kierunki|"
    r"lista kierunków|kierunki studiów)",
    re.I,
)
_HUB_PATH_PATTERN = re.compile(r"^/kierunki-studiow/?$", re.I)


def _extract_catalog_hub_url(result, base_url: str, current_url: str) -> Optional[str]:
    """Wykrywa link-hub do właściwego katalogu kierunków (np. Vistula /kierunki-studiow).

    Zwraca URL tylko gdy: ten sam host, ścieżka = /kierunki-studiow, tekst linku to znana fraza.
    """
    base_host = urlparse(base_url).netloc.lower()
    # Preferuj result.links (crawl4ai parsuje <a> z tekstem)
    links_obj = getattr(result, "links", None) or {}
    if isinstance(links_obj, dict):
        for bucket in ("internal", "external"):
            for item in links_obj.get(bucket, []) or []:
                if not isinstance(item, dict):
                    continue
                href = item.get("href", "") or ""
                text = item.get("text", "") or ""
                if not href:
                    continue
                full = urljoin(base_url, href)
                parsed = urlparse(full)
                if parsed.netloc.lower() != base_host:
                    continue
                if _HUB_PATH_PATTERN.match(parsed.path):
                    if _HUB_LINK_TEXTS.search(text.strip()):
                        return full
    # Fallback: regex na HTML — <a href="...kierunki-studiow...">tekst</a>
    html = getattr(result, "html", None) or getattr(result, "cleaned_html", None) or ""
    if html:
        for m in re.finditer(
            r'<a[^>]+href=["\']([^"\']*kierunki-studiow/?)["\'][^>]*>(.*?)</a>',
            html, re.I | re.S,
        ):
            href, text = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
            if not _HUB_LINK_TEXTS.search(text.strip()):
                continue
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            if parsed.netloc.lower() == base_host and _HUB_PATH_PATTERN.match(parsed.path):
                return full
    return None


def _collect_links_from_crawl_result(
    result,
    crawl_url: str,
    catalog_url: str,
) -> tuple[list[str], list[str]]:
    """Zwraca (gemini_links, html_links) dla jednego crawla.

    gemini_links: lista po call_gemini_with_retry (caller podaje gotową listę).
    html_links:   lista z _extract_course_links_from_crawl po filtrze.
    Funkcja obsługuje tylko html_links — gemini_links są przekazywane z zewnątrz.
    """
    html_links = _extract_course_links_from_crawl(result, crawl_url, catalog_url=catalog_url)
    return html_links


def _gemini_links_from_markdown(md: str, base_url: str, catalog_url: str) -> tuple[list[str], int]:
    """Wysyła markdown do Gemini, zwraca (przefiltrowane_linki, raw_count)."""
    prompt = (
        f"Analizujesz stronę katalogu: {base_url}\n\n"
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
    response_text = call_gemini_with_retry(
        prompt,
        {"response_mime_type": "application/json", "temperature": 0.0},
    )
    try:
        parsed_resp = json.loads(response_text)
    except json.JSONDecodeError:
        parsed_resp = []
    raw: List[str] = parsed_resp if isinstance(parsed_resp, list) else parsed_resp.get("links", [])
    raw_count = len(raw)
    links = [urljoin(base_url, u) for u in raw if isinstance(u, str) and u.strip()]
    links = [
        u for u in links
        if u.startswith(("http://", "https://")) and _is_course_detail_url(u, catalog_url=catalog_url)
    ]
    return list(dict.fromkeys(links)), raw_count


def _merge_links(gemini_links: list[str], html_links: list[str]) -> list[str]:
    """Merge Gemini (priorytet) + HTML z bezpiecznikiem przeciw zalewaniu DOM-śmieciami."""
    g = len(gemini_links)
    h = len(html_links)
    if h > 150 or (g > 0 and h > 10 * g):
        print(
            f"ℹ️ Bezpiecznik merge: html={h} przy gemini={g} — używam tylko Gemini."
        )
        return gemini_links
    merged = list(dict.fromkeys(gemini_links + html_links))
    return merged


async def _crawl_catalog(url: str, crawler) -> object:
    """Jeden crawl katalogu ze standardową konfiguracją."""
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=5,
        delay_before_return_html=5.0,
        magic=True,
        js_code_before_wait=CATALOG_JS_PREP,
        js_code=CATALOG_JS_SCROLL,
    )
    return await crawler.arun(url=url, config=run_config)


async def _links_from_crawl(url: str, crawler) -> tuple[list[str], object]:
    """Crawl + Gemini + HTML → (final_links, result). Przy błędzie zwraca ([], None)."""
    result = await _crawl_catalog(url, crawler)
    if not result.success:
        print(f"❌ Nie udało się pobrać katalogu {url}: {result.error_message}")
        return [], None

    md = result.markdown or ""
    print(f"📄 Markdown ze strony {url}: {len(md)} znaków")
    print(f"📄 Podgląd (pierwsze 500 znaków):\n{md[:500]}\n---")
    if _catalog_markdown_looks_js_blocked(md):
        print("⚠️ Markdown katalogu wygląda na pusty lub zablokowany (cookie/JS).")

    gemini_links, raw_count = await asyncio.to_thread(
        _gemini_links_from_markdown, md, url, url
    )
    print(f"ℹ️ links_gemini={len(gemini_links)} (raw={raw_count})")
    if raw_count and not gemini_links:
        print(f"⚠️ Gemini zwrócił {raw_count} linków, ale post-filter odrzucił wszystkie.")

    html_links = _extract_course_links_from_crawl(result, url, catalog_url=url)
    print(f"ℹ️ links_html={len(html_links)}")

    if gemini_links:
        final = _merge_links(gemini_links, html_links)
    else:
        # HTML fallback gdy Gemini = 0
        final = html_links
        if html_links:
            print(f"ℹ️ Fallback HTML: {len(html_links)} linków.")

    return final, result


async def get_course_links(url: str, crawler: AsyncWebCrawler) -> List[str]:
    url = _strip_tracking_params(url)
    print(f"🔍 Pobieram katalog kierunków: {url}")

    links, result = await _links_from_crawl(url, crawler)

    # ── Hub detection — katalog-pośrednik (np. Vistula /oferta-edukacyjna/...) ──
    hub_url: Optional[str] = None
    if len(links) < 20 and result is not None:
        hub_url = _extract_catalog_hub_url(result, url, url)
        if hub_url:
            hub_url = _strip_tracking_params(hub_url)
            print(f"ℹ️ hub_url={hub_url} — wykonuję dodatkowy crawl katalogu głównego.")
            try:
                hub_links, _ = await _links_from_crawl(hub_url, crawler)
                print(f"ℹ️ links_hub={len(hub_links)}")
                if len(hub_links) > len(links):
                    links = hub_links
                    print("ℹ️ Używam wyników z hub_url (więcej kierunków).")
                else:
                    print("ℹ️ Hub nie poprawił wyniku — zostaję przy URL wejściowym.")
            except Exception as e:
                print(f"⚠️ Hub crawl nieudany ({hub_url}): {e} — zostaję przy URL wejściowym.")
        else:
            print("ℹ️ hub_url=brak")

    if not links and result is not None:
        md = getattr(result, "markdown", "") or ""
        if _catalog_markdown_looks_js_blocked(md):
            print(
                "⚠️ 0 linków — prawdopodobnie katalog ładuje kierunki przez JavaScript/AJAX "
                "(np. WordPress + filtr). Sprawdź, czy w markdownie widać karty kierunków."
            )

    print(f"ℹ️ final_links={len(links)}")
    if links:
        sample = links[:20]
        print("ℹ️ Pierwsze 20 linków:")
        for i, lnk in enumerate(sample, 1):
            print(f"   {i:2}. {lnk}")
    print(f"✅ Znaleziono {len(links)} linków do kierunków.")
    return links

# ── Endpointy ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "gemini_configured": _gemini_api_key_configured(),
    }


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
        js_code_before_wait=COURSE_JS_PREP,
        js_code=COURSE_JS_SCROLL,
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
            html = getattr(result, "html", None) or getattr(result, "cleaned_html", None) or ""
            print(f"  📄 Markdown: {len(md)} znaków")
            try:
                item_warnings: List[str] = []
                md = prepare_markdown_for_extraction(md)
                md_before_fees = md
                md = augment_markdown_with_fee_content(md, html)
                if md != md_before_fees:
                    item_warnings.append("cennik_doklejony_do_markdown")
                kierunek, llm_warnings = await asyncio.to_thread(extract_with_llm, md)
                item_warnings.extend(llm_warnings)
                if not kierunek.czesne:
                    fallback_czesne = extract_czesne_from_html(html)
                    if fallback_czesne:
                        kierunek.czesne = normalize_czesne(fallback_czesne)
                        item_warnings.append("czesne_uzupelnione_z_html_fallback")
                        print(
                            f"ℹ️ Czesne uzupełnione z HTML "
                            f"({len(kierunek.czesne or [])} wpisów)."
                        )
                    elif _text_has_fee_signals(md):
                        item_warnings.append("czesne_null_mimo_sygnalow_w_tekscie")
                        print(
                            f"⚠️ Tekst zawiera opłaty, ale LLM zwrócił null dla czesne: {url}"
                        )
                opis_warning = _opis_promo_warning(kierunek, url)
                if opis_warning:
                    item_warnings.append(opis_warning)
                results.append(
                    ScrapeResultItem(url=url, data=kierunek, warnings=item_warnings)
                )
                print(f"✅ [{i}/{MAX_COURSES_PER_RUN}] {kierunek.kierunek} | {kierunek.stopien} | {'/'.join(kierunek.tryb)}")
            except GeminiNotConfiguredError as e:
                raise HTTPException(status_code=503, detail=str(e)) from e
            except GeminiQuotaError as e:
                print(f"❌ [{i}/{MAX_COURSES_PER_RUN}] Limit API Gemini: {e}")
                results.append(ScrapeResultItem(url=url, error=str(e)))
            except Exception as e:
                print(f"❌ [{i}/{MAX_COURSES_PER_RUN}] Błąd ekstrakcji: {e}")
                results.append(ScrapeResultItem(url=url, error=str(e)))
    except GeminiNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
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