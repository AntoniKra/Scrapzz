"""Regresja normalizacji czesne i trybu — deterministyczne, bez Gemini."""

import json
from pathlib import Path

from scraper import CzesneEntry, normalize_czesne, normalize_tryb

FIXTURES = Path(__file__).parent / "fixtures"


def test_normalize_czesne_drops_installments():
    entries = [
        CzesneEntry(wariant="stacjonarne I rok", kwota="3500 zł/semestr"),
        CzesneEntry(wariant="1 rata", kwota="599 zł"),
        CzesneEntry(wariant="stacjonarne", kwota="6920 zł za rok akademicki"),
    ]
    result = normalize_czesne(entries)
    assert result is not None
    assert len(result) == 1
    assert result[0].kwota == "3500 zł/semestr"


def test_normalize_czesne_adds_semester_unit():
    entries = [
        CzesneEntry(wariant="stacjonarne za semestr", kwota="2400 zł"),
    ]
    result = normalize_czesne(entries)
    assert result is not None
    assert result[0].kwota == "2400 zł/semestr"


def test_normalize_czesne_deduplicates():
    entries = [
        CzesneEntry(wariant="stacjonarne", kwota="3000 zł/semestr"),
        CzesneEntry(wariant="stacjonarne", kwota="3000 zł/semestr"),
    ]
    result = normalize_czesne(entries)
    assert result is not None
    assert len(result) == 1


def test_normalize_czesne_empty_returns_none():
    assert normalize_czesne(None) is None
    assert normalize_czesne([]) == []


def test_normalize_tryb_ul_contact_ignored():
    markdown = (FIXTURES / "ul_tryb_markdown.txt").read_text(encoding="utf-8")
    tryb, warning = normalize_tryb(["stacjonarne", "niestacjonarne"], markdown)
    assert tryb == ["stacjonarne"]
    assert warning == "tryb_skorygowany_w_normalizacji"


def test_normalize_tryb_single_unchanged():
    markdown = "Stacjonarne studia I stopnia\n\nProgram studiów."
    tryb, warning = normalize_tryb(["stacjonarne"], markdown)
    assert tryb == ["stacjonarne"]
    assert warning is None


def test_kozminski_semester_czesne_from_fixture():
    data = json.loads((FIXTURES / "kozminski_czesne_input.json").read_text(encoding="utf-8"))
    entries = [CzesneEntry(**item) for item in data["input"]]
    result = normalize_czesne(entries)
    assert result is not None
    assert len(result) == data["expected_count"]
    assert [e.kwota for e in result] == data["expected_kwotas"]
