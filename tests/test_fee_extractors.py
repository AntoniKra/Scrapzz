"""Regresja ekstrakcji czesne z HTML — fragment Ideis, bez crawla."""

from pathlib import Path

from scraper import extract_czesne_from_html

FIXTURES = Path(__file__).parent / "fixtures"


def test_ideis_fee_snippet_extracts_year_price():
  html = (FIXTURES / "ideis_fee_snippet.html").read_text(encoding="utf-8")
  entries = extract_czesne_from_html(html)
  assert entries is not None
  assert len(entries) >= 1
  assert any("6920" in e.kwota.replace(" ", "") for e in entries)


def test_empty_html_returns_none():
  assert extract_czesne_from_html("") is None
  assert extract_czesne_from_html("<html><body>Brak opłat</body></html>") is None
