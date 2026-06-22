import { useState } from 'react';
import './App.css';

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000';

const STOPIEN_LABEL = {
  '1_stopnia': 'I stopień',
  '2_stopnia': 'II stopień',
  'jednolite_magisterskie': 'Jednolite magisterskie',
};

// ── Badge ─────────────────────────────────────────────────────────────────────

function Badge({ children, variant = 'default' }) {
  return <span className={`badge badge--${variant}`}>{children}</span>;
}

// ── CourseCard ────────────────────────────────────────────────────────────────

function CourseCard({ item }) {
  const [expanded, setExpanded] = useState(false);
  const { data, url, error } = item;

  if (error || !data) {
    return (
      <div className="card card--error">
        <p className="card__error-title">Błąd ekstrakcji</p>
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          className="card__url"
          onClick={e => e.stopPropagation()}
        >
          {url}
        </a>
        {error && <p className="card__error-detail">{error}</p>}
      </div>
    );
  }

  return (
    <article
      className={`card${expanded ? ' card--expanded' : ''}`}
      onClick={() => setExpanded(prev => !prev)}
      role="button"
      tabIndex={0}
      onKeyDown={e => (e.key === 'Enter' || e.key === ' ') && setExpanded(prev => !prev)}
      aria-expanded={expanded}
    >
      <div className="card__header">
        <div className="card__tags">
          {data.tryb.map(t => (
            <Badge key={t} variant={t === 'stacjonarne' ? 'teal' : 'orange'}>
              {t}
            </Badge>
          ))}
          <Badge variant="indigo">{STOPIEN_LABEL[data.stopien] ?? data.stopien}</Badge>
        </div>
        <span className="card__chevron" aria-hidden="true">
          {expanded ? '▲' : '▼'}
        </span>
      </div>

      <h2 className="card__title">{data.kierunek}</h2>

      <ul className="card__meta">
        <li>
          <span className="meta__label">Tytuł</span>
          <span className="meta__value">{data.tytul}</span>
        </li>
        <li>
          <span className="meta__label">Semestry</span>
          <span className="meta__value">{data.semestry ?? '—'}</span>
        </li>
        <li>
          <span className="meta__label">Język</span>
          <span className="meta__value">{data.jezyk}</span>
        </li>
        <li>
          <span className="meta__label">Czesne</span>
          <span className={`meta__value${!data.czesne?.length ? ' meta__value--muted' : ''}`}>
            {data.czesne?.length === 1
              ? data.czesne[0].kwota
              : data.czesne?.length > 1
              ? `${data.czesne.length} wariantów ↓`
              : 'Brak danych'}
          </span>
        </li>
        {data.wydzial && (
          <li>
            <span className="meta__label">Wydział</span>
            <span className="meta__value">{data.wydzial}</span>
          </li>
        )}
      </ul>

      {expanded && (
        <div className="card__details">
          <hr className="card__divider" />

          {data.opis && <p className="card__opis">{data.opis}</p>}

          {data.czesne?.length > 0 && (
            <div className="details__section">
              <h3 className="details__heading">Opłaty</h3>
              <table className="czesne-table">
                <tbody>
                  {data.czesne.map((e, i) => (
                    <tr key={i}>
                      <td className="czesne-table__wariant">{e.wariant}</td>
                      <td className="czesne-table__kwota">{e.kwota}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {data.specjalizacje?.length > 0 && (
            <div className="details__section">
              <h3 className="details__heading">Specjalizacje</h3>
              <div className="tags-group">
                {data.specjalizacje.map((s, i) => (
                  <Badge key={i} variant="subtle">{s}</Badge>
                ))}
              </div>
            </div>
          )}

          {data.rekrutacja?.length > 0 && (
            <div className="details__section">
              <h3 className="details__heading">Wymagane przedmioty</h3>
              <div className="tags-group">
                {data.rekrutacja.map((r, i) => (
                  <Badge key={i} variant="ghost">{r}</Badge>
                ))}
              </div>
            </div>
          )}

          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="card__source-link"
            onClick={e => e.stopPropagation()}
          >
            Źródło ↗
          </a>
        </div>
      )}
    </article>
  );
}

// ── LoadingPanel ──────────────────────────────────────────────────────────────

function LoadingPanel() {
  return (
    <div className="terminal" role="status" aria-live="polite">
      <div className="terminal__header">
        <span className="terminal__dot terminal__dot--red" />
        <span className="terminal__dot terminal__dot--yellow" />
        <span className="terminal__dot terminal__dot--green" />
        <span className="terminal__title">scrapzz — spider</span>
      </div>
      <div className="terminal__body">
        <div className="spinner" aria-hidden="true" />
        <p className="terminal__msg">Pobieranie i analiza danych przez AI.</p>
        <p className="terminal__msg terminal__msg--muted">
          To może potrwać do 2–3 minut...
        </p>
      </div>
    </div>
  );
}

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [url, setUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [response, setResponse] = useState(null);

  const handleSubmit = async e => {
    e.preventDefault();
    if (!url.trim()) return;

    setLoading(true);
    setError(null);
    setResponse(null);

    try {
      const res = await fetch(`${API_BASE}/api/scrape`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const json = await res.json();

      if (!res.ok) {
        setError(json.detail ?? `Błąd serwera: ${res.status}`);
      } else {
        setResponse(json);
      }
    } catch {
      setError('Nie można połączyć się z backendem. Sprawdź, czy serwer działa na porcie 8000.');
    } finally {
      setLoading(false);
    }
  };

  const successCount = response?.results.filter(r => r.data).length ?? 0;

  return (
    <div className="app">
      <header className="app__header">
        <h1 className="app__logo">
          Scrapzz <span className="app__logo-accent">AI</span>
        </h1>
        <p className="app__tagline">Agregator danych o kierunkach studiów</p>
      </header>

      <main className="app__main">
        <section className="search-section">
          <form className="search-form" onSubmit={handleSubmit}>
            <input
              className="search-input"
              type="url"
              value={url}
              onChange={e => setUrl(e.target.value)}
              placeholder="https://uczelnia.edu.pl/studia"
              required
              disabled={loading}
              aria-label="URL katalogu kierunków uczelni"
            />
            <button className="search-btn" type="submit" disabled={loading}>
              {loading ? 'Skanowanie...' : 'Skanuj'}
            </button>
          </form>
        </section>

        {loading && <LoadingPanel />}

        {error && (
          <div className="error-banner" role="alert">
            <span className="error-banner__icon" aria-hidden="true">⚠</span>
            <span>{error}</span>
          </div>
        )}

        {response && (
          <>
            <div className="stats-bar" aria-label="Statystyki skanowania">
              <span>
                Znalezione linki: <strong>{response.links_found}</strong>
              </span>
              <span className="stats-bar__sep" aria-hidden="true">·</span>
              <span>
                Przetworzone: <strong>{response.links_processed}</strong>
              </span>
              <span className="stats-bar__sep" aria-hidden="true">·</span>
              <span>
                Sukces: <strong>{successCount}</strong>
              </span>
            </div>

            <div className="results-grid">
              {response.results.map((item, i) => (
                <CourseCard key={i} item={item} />
              ))}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
