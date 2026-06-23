"""Regresja filtrów linków — deterministyczne, bez crawla i Gemini."""

import json
from pathlib import Path

from scraper import (
    _is_catalog_list_url,
    _is_course_detail_url,
    _is_junk_url,
    _merge_links,
    _strip_tracking_params,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_ideis_course_urls_pass_hybrid_filter():
    data = _load("ideis_link_urls.json")
    catalog = data["catalog_url"]
    kept = [_is_course_detail_url(u, catalog_url=catalog) for u in data["course_urls"]]
    assert all(kept), "Kierunki Ideis nie powinny odpadać w hybrid filtrze"


def test_ideis_junk_and_catalog_rejected():
    data = _load("ideis_link_urls.json")
    catalog = data["catalog_url"]
    for url in data["rejected_urls"]:
        assert not _is_course_detail_url(url, catalog_url=catalog), url


def test_ideis_regression_not_zero_links():
    """Regresja: sztywna whitelist kiedyś dała 0 linków — tu musi zostać >0."""
    data = _load("ideis_link_urls.json")
    catalog = data["catalog_url"]
    all_urls = data["course_urls"] + data["rejected_urls"]
    kept = [u for u in all_urls if _is_course_detail_url(u, catalog_url=catalog)]
    assert len(kept) >= len(data["course_urls"])


def test_pl_course_urls_on_rekrutacja_host():
    data = _load("pl_link_urls.json")
    catalog = data["catalog_url"]
    for url in data["course_urls"]:
        assert _is_course_detail_url(url, catalog_url=catalog), url
    for url in data["rejected_urls"]:
        assert not _is_course_detail_url(url, catalog_url=catalog), url


def test_pl_junk_does_not_use_hostname():
    assert not _is_junk_url("https://rekrutacja.p.lodz.pl/kierunek/informatyka")
    assert _is_junk_url("https://rekrutacja.p.lodz.pl/kontakt")


def test_ul_catalog_pagination_rejected():
    data = _load("ul_catalog_urls.json")
    for url in data["catalog_list_urls"]:
        assert _is_catalog_list_url(url)
        assert not _is_course_detail_url(url, catalog_url=data["catalog_url"])


def test_ul_course_url_passes():
    data = _load("ul_catalog_urls.json")
    url = data["course_urls"][0]
    assert _is_course_detail_url(url, catalog_url=data["catalog_url"])


def test_ata_course_urls_pass_hybrid_filter():
    data = _load("ata_link_urls.json")
    catalog = data["catalog_url"]
    for url in data["course_urls"]:
        assert _is_course_detail_url(url, catalog_url=catalog), url


def test_ata_junk_and_catalog_rejected():
    data = _load("ata_link_urls.json")
    catalog = data["catalog_url"]
    for url in data["rejected_urls"]:
        assert not _is_course_detail_url(url, catalog_url=catalog), url


def test_kozminski_course_urls_pass():
    data = _load("kozminski_link_urls.json")
    catalog = data["catalog_url"]
    for url in data["course_urls"]:
        assert _is_course_detail_url(url, catalog_url=catalog), url


def test_kozminski_podyplomowe_and_junk_rejected():
    data = _load("kozminski_link_urls.json")
    catalog = data["catalog_url"]
    for url in data["rejected_urls"]:
        assert not _is_course_detail_url(url, catalog_url=catalog), url
    assert _is_junk_url("https://www.kozminski.edu.pl/pl/oferta-edukacyjna/studia-podyplomowe")


def test_vistula_kierunki_studiow_course_urls_pass():
    """Kierunki pod /kierunki-studiow/<slug> muszą przechodzić hybrid filter."""
    data = _load("vistula_link_urls.json")
    catalog = data["catalog_url"]
    for url in data["course_urls"]:
        assert _is_course_detail_url(url, catalog_url=catalog), url


def test_vistula_hub_root_not_a_course():
    """/kierunki-studiow bez sluga to katalog/hub — nie może przejść jako kierunek."""
    data = _load("vistula_link_urls.json")
    catalog = data["catalog_url"]
    for url in data["hub_urls"]:
        assert not _is_course_detail_url(url, catalog_url=catalog), (
            f"{url} nie powinien być traktowany jako kierunek"
        )


def test_vistula_nav_and_junk_rejected():
    data = _load("vistula_link_urls.json")
    catalog = data["catalog_url"]
    for url in data["rejected_urls"]:
        assert not _is_course_detail_url(url, catalog_url=catalog), url


def test_strip_tracking_params():
    dirty = (
        "https://vistula.edu.pl/oferta-edukacyjna/i-stopnia-licencjackie"
        "?_gl=1abc&gclid=XYZ&gbraid=ABC&foo=bar"
    )
    clean = _strip_tracking_params(dirty)
    assert "gclid" not in clean
    assert "_gl" not in clean
    assert "gbraid" not in clean
    assert "foo=bar" in clean
    assert clean.startswith("https://vistula.edu.pl/oferta-edukacyjna/")


def test_merge_links_gemini_priority():
    """Gemini linki są na początku; merge nie dodaje duplikatów."""
    gemini = ["https://example.pl/kierunek/a", "https://example.pl/kierunek/b"]
    html = ["https://example.pl/kierunek/b", "https://example.pl/kierunek/c"]
    result = _merge_links(gemini, html)
    assert result[0] == gemini[0]
    assert len(result) == 3
    assert "https://example.pl/kierunek/b" in result


def test_merge_links_bezpiecznik_150():
    """Jeśli html > 150 — nie merge, tylko Gemini."""
    gemini = [f"https://x.pl/kierunek/{i}" for i in range(10)]
    html = [f"https://x.pl/other/{i}" for i in range(160)]
    result = _merge_links(gemini, html)
    assert result == gemini


def test_merge_links_bezpiecznik_10x():
    """Jeśli html > 10× gemini — nie merge, tylko Gemini."""
    gemini = [f"https://x.pl/kierunek/{i}" for i in range(5)]
    html = [f"https://x.pl/other/{i}" for i in range(55)]
    result = _merge_links(gemini, html)
    assert result == gemini


def test_merge_links_html_fallback_gdy_gemini_zero():
    """Gdy gemini=[], bezpiecznik nie blokuje (nie dzielimy przez 0)."""
    gemini: list = []
    html = [f"https://x.pl/kierunek/{i}" for i in range(20)]
    result = _merge_links(gemini, html)
    assert result == html
