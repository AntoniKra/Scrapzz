"""Testy mappera WKS preview — bez crawla i Gemini."""

from scraper import (
    CzesneEntry,
    KierunekStudiow,
    ScrapeResponse,
    ScrapeResultItem,
)
from wks_mapper import (
    build_link_key,
    build_links,
    map_scrape_response_to_wks_preview,
    normalize_kierunek_name,
)


def _sample_data(**kwargs) -> KierunekStudiow:
    defaults = {
        "kierunek": "Zarządzanie",
        "stopien": "1_stopnia",
        "tytul": "licencjat",
        "tryb": ["stacjonarne"],
        "semestry": 6,
        "wydzial": "Wydział Nauk Ekonomicznych",
        "jezyk": "polski",
        "specjalizacje": [],
        "rekrutacja": ["matematyka"],
        "czesne": [CzesneEntry(wariant="stacjonarne I rok", kwota="4500 zł/semestr")],
        "opis": "Krótki opis kierunku.",
    }
    defaults.update(kwargs)
    return KierunekStudiow(**defaults)


def test_build_link_key_stacjonarne_i():
    assert build_link_key("stacjonarne", "1_stopnia") == "stacjonarne_i"
    assert build_link_key("niestacjonarne", "2_stopnia") == "niestacjonarne_ii"
    assert build_link_key("stacjonarne", "jednolite_magisterskie") == "stacjonarne_jednolite"


def test_build_links_multiple_tryby():
    links = build_links(
        ["stacjonarne", "niestacjonarne"],
        "1_stopnia",
        "https://example.pl/kierunek/zarzadzanie",
    )
    assert links == {
        "stacjonarne_i": "https://example.pl/kierunek/zarzadzanie",
        "niestacjonarne_i": "https://example.pl/kierunek/zarzadzanie",
    }


def test_map_valid_result_to_wydzialy():
    response = ScrapeResponse(
        status="success",
        links_found=1,
        links_processed=1,
        results=[
            ScrapeResultItem(
                url="https://vistula.edu.pl/kierunki-studiow/zarzadzanie",
                data=_sample_data(
                    specjalizacje=["Marketing i sprzedaż"],
                ),
                warnings=["tryb_skorygowany_w_normalizacji"],
            ),
        ],
    )
    preview = map_scrape_response_to_wks_preview(response)

    assert preview["export_kind"] == "wks_preview"
    assert preview["id_uczelni"] is None
    assert len(preview["wydzialy"]) == 1

    wydzial = preview["wydzialy"][0]
    assert wydzial["nazwa"] == "Zarządzanie"
    assert wydzial["kierunki"] == [{
        "id_kierunku": None,
        "nazwa": "Marketing i sprzedaż",
        "priorytet": None,
        "nabor_zimowy": 0,
        "links": [],
        "specjalnosci": [],
    }]
    assert wydzial["links"]["stacjonarne_i"] == "https://vistula.edu.pl/kierunki-studiow/zarzadzanie"
    assert wydzial["scrapzz_data"]["czesne"][0]["kwota"] == "4500 zł/semestr"
    assert wydzial["scrapzz_data"]["source_urls"] == [
        "https://vistula.edu.pl/kierunki-studiow/zarzadzanie"
    ]

    summary = preview["summary"]
    assert summary["valid_results_count"] == 1
    assert summary["wydzialy_count"] == 1
    assert summary["specjalizacje_count"] == 1
    assert summary["programs_without_specjalizacje_count"] == 0
    assert summary["error_results_count"] == 0


def test_empty_specjalizacje_leaves_kierunki_empty():
    response = ScrapeResponse(
        status="success",
        links_found=1,
        links_processed=1,
        results=[
            ScrapeResultItem(
                url="https://example.pl/program",
                data=_sample_data(specjalizacje=[]),
            ),
        ],
    )
    preview = map_scrape_response_to_wks_preview(response)
    assert preview["wydzialy"][0]["kierunki"] == []
    assert preview["summary"]["programs_without_specjalizacje_count"] == 1


def test_error_result_goes_to_failed_urls():
    response = ScrapeResponse(
        status="success",
        links_found=2,
        links_processed=2,
        results=[
            ScrapeResultItem(
                url="https://example.pl/ok",
                data=_sample_data(kierunek="Informatyka"),
            ),
            ScrapeResultItem(
                url="https://example.pl/fail",
                error="timeout",
            ),
        ],
    )
    preview = map_scrape_response_to_wks_preview(response)
    assert preview["summary"]["valid_results_count"] == 1
    assert preview["summary"]["error_results_count"] == 1
    assert preview["summary"]["failed_urls"] == [{
        "url": "https://example.pl/fail",
        "error": "timeout",
    }]
    assert len(preview["wydzialy"]) == 1


def test_merge_same_kierunek_and_stopien():
    response = ScrapeResponse(
        status="success",
        links_found=2,
        links_processed=2,
        results=[
            ScrapeResultItem(
                url="https://example.pl/a",
                data=_sample_data(
                    tryb=["stacjonarne"],
                    specjalizacje=["Spec A"],
                ),
            ),
            ScrapeResultItem(
                url="https://example.pl/b",
                data=_sample_data(
                    tryb=["niestacjonarne"],
                    specjalizacje=["Spec B"],
                ),
            ),
        ],
    )
    preview = map_scrape_response_to_wks_preview(response)
    assert len(preview["wydzialy"]) == 1
    wydzial = preview["wydzialy"][0]
    assert wydzial["links"]["stacjonarne_i"] == "https://example.pl/a"
    assert wydzial["links"]["niestacjonarne_i"] == "https://example.pl/b"
    assert {k["nazwa"] for k in wydzial["kierunki"]} == {"Spec A", "Spec B"}
    assert wydzial["scrapzz_data"]["source_urls"] == [
        "https://example.pl/a",
        "https://example.pl/b",
    ]
    assert preview["summary"]["potential_duplicates"] == ["Zarządzanie|1_stopnia"]


def test_no_merge_different_kierunek_names():
    response = ScrapeResponse(
        status="success",
        links_found=2,
        links_processed=2,
        results=[
            ScrapeResultItem(
                url="https://example.pl/a",
                data=_sample_data(kierunek="Zarządzanie"),
            ),
            ScrapeResultItem(
                url="https://example.pl/b",
                data=_sample_data(kierunek="Ekonomia"),
            ),
        ],
    )
    preview = map_scrape_response_to_wks_preview(response)
    assert len(preview["wydzialy"]) == 2
    assert preview["summary"]["potential_duplicates"] == []


def test_normalize_kierunek_name_strips_case():
    assert normalize_kierunek_name("  Zarządzanie ") == normalize_kierunek_name("zarzadzanie")
