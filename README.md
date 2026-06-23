# Scrapzz

> Szczegółowy opis projektu, architektura i workflow: [PROJECT.md](PROJECT.md)

Agregator danych o kierunkach studiów — spider (Crawl4AI + Playwright) + ekstrakcja strukturalna (Gemini) + frontend React.

## Wymagania

| Narzędzie | Wersja |
|-----------|--------|
| Python | 3.12+ (patrz `.python-version`) |
| Node.js | 20 LTS lub nowszy |
| Git | dowolna aktualna |
| Klucz API | [Google AI Studio](https://aistudio.google.com/apikey) → `GEMINI_API_KEY` |

## Szybki start

### 1. Klonowanie

```bash
git clone https://github.com/AntoniKra/Scrapzz.git
cd Scrapzz
```

### 2. Backend (terminal 1)

```bash
python -m venv venv
```

**Windows (Git Bash / PowerShell):**
```bash
source venv/Scripts/activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

**Linux / macOS:**
```bash
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Edytuj plik `.env` w katalogu głównym projektu i wklej swój klucz:

```env
GEMINI_API_KEY=twój_klucz_tutaj
GEMINI_MODEL=gemini-3.1-flash-lite
```

Uruchom serwer:

```bash
python scraper.py
```

Backend: [http://127.0.0.1:8000](http://127.0.0.1:8000)  
Dokumentacja API: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)  
Health check: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

### 3. Frontend (terminal 2)

```bash
cd frontend
copy .env.example .env    # Windows
# cp .env.example .env    # Linux / macOS
npm ci
npm run dev
```

UI: [http://localhost:5173](http://localhost:5173)

Domyślnie frontend łączy się z backendem pod `http://127.0.0.1:8000`. Inny adres ustaw w `frontend/.env`:

```env
VITE_API_BASE=http://127.0.0.1:8000
```

## Struktura projektu

```
Scrapzz/
├── scraper.py          # FastAPI + spider + ekstrakcja Gemini
├── requirements.txt    # zależności Pythona
├── .env.example        # szablon konfiguracji (skopiuj → .env)
├── frontend/           # React + Vite
│   ├── .env.example
│   └── src/App.jsx     # UI skanowania
└── README.md
```

## Zmienne środowiskowe

| Zmienna | Gdzie | Opis |
|---------|-------|------|
| `GEMINI_API_KEY` | `.env` (root) | **Wymagane** — klucz Google Gemini |
| `GEMINI_MODEL` | `.env` (root) | Model LLM (domyślnie `gemini-3.1-flash-lite`) |
| `VITE_API_BASE` | `frontend/.env` | URL backendu dla UI |

Plik `.env` ładuje się automatycznie przy starcie `scraper.py` (`python-dotenv`). Nie commituj `.env` — jest w `.gitignore`.

## Rozwiązywanie problemów

### `Executable doesn't exist` / Playwright

Po `pip install` uruchom:

```bash
playwright install chromium
```

### Błąd autoryzacji Gemini / 401 / 403

- Sprawdź `GEMINI_API_KEY` w `.env`
- Upewnij się, że plik `.env` leży w **katalogu głównym** projektu (obok `scraper.py`)
- Zrestartuj `python scraper.py` po zmianie `.env`

### Frontend: „Nie można połączyć się z backendem”

- Backend musi działać na porcie **8000**
- Sprawdź `VITE_API_BASE` w `frontend/.env`
- Po zmianie `frontend/.env` zrestartuj `npm run dev`

### Limit API Gemini (429)

Spider ma wbudowany rate limiting i retry. Przy darmowym planie ogranicz liczbę kierunków (`MAX_COURSES_PER_RUN` w `scraper.py`) lub odczekaj i spróbuj ponownie.

### Windows

Kod ustawia `WindowsProactorEventLoopPolicy` dla Playwright — wymagane na Windows. Uvicorn uruchamia się z `reload=False` na Windows (ograniczenie Playwright + asyncio).

## Testy regresji

Deterministyczne testy filtrów linków, normalizacji i ekstrakcji czesne — **bez** crawla, Gemini i internetu.

```bash
source venv/Scripts/activate   # Windows Git Bash
pip install -r requirements-dev.txt
pytest
```

Fixture’y w `tests/fixtures/` to małe próbki (URL-e, fragment HTML/markdown) z uczelni referencyjnych.

## Licencja

Projekt prywatny — repozytorium [AntoniKra/Scrapzz](https://github.com/AntoniKra/Scrapzz).
