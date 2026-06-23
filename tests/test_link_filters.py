"""Regresja filtrów linków — deterministyczne, bez crawla i Gemini."""

import json
from pathlib import Path

from scraper import _is_catalog_list_url, _is_course_detail_url, _is_junk_url

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
