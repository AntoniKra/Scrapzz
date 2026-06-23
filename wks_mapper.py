"""Mapper ScrapeResponse → WKS-style preview JSON (bez importu do bazy)."""

from __future__ import annotations

import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from scraper import CzesneEntry, KierunekStudiow, ScrapeResponse, ScrapeResultItem

_STOPNIEN_SUFFIX = {
    "1_stopnia": "i",
    "2_stopnia": "ii",
    "jednolite_magisterskie": "jednolite",
}

_TRYB_PREFIX = {
    "stacjonarne": "stacjonarne",
    "niestacjonarne": "niestacjonarne",
    "online": "online",
}


def normalize_kierunek_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name.strip().lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def build_link_key(tryb: str, stopien: str) -> Optional[str]:
    prefix = _TRYB_PREFIX.get(tryb)
    suffix = _STOPNIEN_SUFFIX.get(stopien)
    if not prefix or not suffix:
        return None
    return f"{prefix}_{suffix}"


def build_links(tryb_list: List[str], stopien: str, url: str) -> Dict[str, str]:
    links: Dict[str, str] = {}
    for tryb in tryb_list:
        key = build_link_key(tryb, stopien)
        if key:
            links[key] = url
    return links


def _specjalizacja_entry(nazwa: str) -> Dict[str, Any]:
    return {
        "id_kierunku": None,
        "nazwa": nazwa,
        "priorytet": None,
        "nabor_zimowy": 0,
        "links": [],
        "specjalnosci": [],
    }


def _czesne_to_json(czesne: Optional[List[CzesneEntry]]) -> Optional[List[Dict[str, str]]]:
    if czesne is None:
        return None
    return [entry.model_dump() for entry in czesne]


def _scrapzz_data_from_item(item: ScrapeResultItem) -> Dict[str, Any]:
    data = item.data
    assert data is not None
    return {
        "source_urls": [item.url],
        "stopien": data.stopien,
        "tytul": data.tytul,
        "tryb": list(data.tryb),
        "semestry": data.semestry,
        "wydzial": data.wydzial,
        "jezyk": data.jezyk,
        "rekrutacja": list(data.rekrutacja),
        "czesne": _czesne_to_json(data.czesne),
        "opis": data.opis,
        "warnings": list(item.warnings),
    }


def _wydzial_from_item(item: ScrapeResultItem) -> Dict[str, Any]:
    data = item.data
    assert data is not None
    return {
        "id_wydzialu": None,
        "nazwa": data.kierunek,
        "priorytet": None,
        "nabor_zimowy": 0,
        "links": build_links(list(data.tryb), data.stopien, item.url),
        "kierunki": [_specjalizacja_entry(s) for s in data.specjalizacje],
        "scrapzz_data": _scrapzz_data_from_item(item),
    }


def _merge_links(
    existing: Dict[str, str],
    new: Dict[str, str],
) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    merged = dict(existing)
    conflicts: List[Dict[str, str]] = []
    for key, url in new.items():
        if key in merged and merged[key] != url:
            conflicts.append({
                "key": key,
                "kept_url": merged[key],
                "discarded_url": url,
            })
        elif key not in merged:
            merged[key] = url
    return merged, conflicts


def _merge_kierunki(
    left: List[Dict[str, Any]],
    right: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    by_name: Dict[str, Dict[str, Any]] = {}
    for entry in left + right:
        name = entry["nazwa"].strip()
        if name not in by_name:
            by_name[name] = entry
    return list(by_name.values())


def _merge_wydzial(
    left: Dict[str, Any],
    right: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, str]], str]:
    links, conflicts = _merge_links(left["links"], right["links"])
    merge_key = f"{left['nazwa']}|{left['scrapzz_data']['stopien']}"
    source_urls = list(dict.fromkeys(
        left["scrapzz_data"]["source_urls"] + right["scrapzz_data"]["source_urls"]
    ))
    warnings = list(dict.fromkeys(
        left["scrapzz_data"]["warnings"] + right["scrapzz_data"]["warnings"]
    ))
    merged = {
        **left,
        "links": links,
        "kierunki": _merge_kierunki(left["kierunki"], right["kierunki"]),
        "scrapzz_data": {
            **left["scrapzz_data"],
            "source_urls": source_urls,
            "warnings": warnings,
        },
    }
    return merged, conflicts, merge_key


def map_scrape_response_to_wks_preview(
    response: ScrapeResponse,
    id_uczelni: Optional[int] = None,
) -> Dict[str, Any]:
    """Mapuje ScrapeResponse na WKS-style preview JSON."""
    failed_urls: List[Dict[str, str]] = []
    valid_count = 0
    error_count = 0
    warnings_count = 0
    programs_without_specjalizacje = 0
    specjalizacje_count = 0
    link_conflicts: List[Dict[str, str]] = []
    potential_duplicates: List[str] = []

    wydzialy_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for item in response.results:
        if item.data is None:
            error_count += 1
            failed_urls.append({
                "url": item.url,
                "error": item.error or "brak data",
            })
            continue

        valid_count += 1
        warnings_count += len(item.warnings)
        if not item.data.specjalizacje:
            programs_without_specjalizacje += 1
        specjalizacje_count += len(item.data.specjalizacje)

        merge_key = (
            normalize_kierunek_name(item.data.kierunek),
            item.data.stopien,
        )
        wydzial = _wydzial_from_item(item)

        if merge_key in wydzialy_by_key:
            merged, conflicts, dup_label = _merge_wydzial(
                wydzialy_by_key[merge_key], wydzial
            )
            wydzialy_by_key[merge_key] = merged
            link_conflicts.extend(conflicts)
            potential_duplicates.append(dup_label)
        else:
            wydzialy_by_key[merge_key] = wydzial

    wydzialy = sorted(wydzialy_by_key.values(), key=lambda w: w["nazwa"].lower())

    return {
        "schema_version": 1,
        "export_kind": "wks_preview",
        "id_uczelni": id_uczelni,
        "scrapzz_run": {
            "status": response.status,
            "links_found": response.links_found,
            "links_processed": response.links_processed,
        },
        "wydzialy": wydzialy,
        "kierunki_bez_wydzialu": [],
        "specjalnosci_bez_kierunku": [],
        "summary": {
            "input_results_count": len(response.results),
            "valid_results_count": valid_count,
            "error_results_count": error_count,
            "wydzialy_count": len(wydzialy),
            "specjalizacje_count": specjalizacje_count,
            "programs_without_specjalizacje_count": programs_without_specjalizacje,
            "warnings_count": warnings_count,
            "failed_urls": failed_urls,
            "link_conflicts": link_conflicts,
            "potential_duplicates": potential_duplicates,
            "mapping_note": (
                "Scrapzz result.data.kierunek → wydzialy[].nazwa; "
                "result.data.specjalizacje → wydzialy[].kierunki[]. "
                "Pełne dane techniczne (w tym czesne) w scrapzz_data."
            ),
        },
    }
