"""Regression tests for EDGAR document selection (see run 33989900549)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fetch_filings as ff  # noqa: E402


def files(*names):
    return [{"name": n} for n in names]


def test_never_selects_the_cover_page():
    """primary_doc.xml parses fine but holds no <infoTable> rows - picking it
    silently dropped 8 of 52 filers from a real run."""
    for listing in [
        files("form13fInfoTable.xml", "primary_doc.xml"),
        files("primary_doc.xml", "form13fInfoTable.xml"),
        files("Form13FInfoTable.xml", "primary_doc.xml"),
        files("Form13fInfoTable.xml", "primary_doc.xml"),
        files("form13f-1786738348_infotable.xml", "primary_doc.xml"),
        files("informationtable.xml", "primary_doc.xml"),
        files("0001234567-26-000001-index.html", "form13fInfoTable.xml", "primary_doc.xml"),
    ]:
        picked = ff.find_infotable_filename(listing)
        assert picked is not None
        assert picked.lower() not in ff._COVER_DOC_NAMES, listing
        assert "infotable" in picked.lower() or "informationtable" in picked.lower()


def test_non_xml_and_empty_listings():
    assert ff.find_infotable_filename(files("a.txt", "b.html")) is None
    assert ff.find_infotable_filename([]) is None


def test_falls_back_to_any_non_cover_xml():
    assert ff.find_infotable_filename(files("primary_doc.xml", "holdings.xml")) == "holdings.xml"
    # cover page only -> nothing usable
    assert ff.find_infotable_filename(files("primary_doc.xml")) is None


def test_sec_ticker_map_uses_www_host():
    """data.sec.gov/files/company_tickers.json returns 404; www.sec.gov serves it."""
    import inspect
    src = inspect.getsource(ff._build_sec_name_map)
    assert "https://www.sec.gov/files/company_tickers.json" in src
    assert "data.sec.gov/files/company_tickers.json" not in src
