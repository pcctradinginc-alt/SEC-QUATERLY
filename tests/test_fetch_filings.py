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


REAL_INFOTABLE = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>ALLY FINL INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>02005N100</cusip>
    <value>577211815</value>
    <shrsOrPrnAmt>
      <sshPrnamt>12561737</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>DFND</investmentDiscretion>
    <votingAuthority><Sole>12561737</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
</informationTable>"""


def test_share_count_is_read_from_the_nested_element():
    """<sshPrnamt> sits inside <shrsOrPrnAmt>. A direct-child lookup returned
    nothing and silently produced 0 shares for every position ever parsed,
    which made every holding look NEW and produced zero EXITs universe-wide."""
    holdings = ff.parse_infotable(REAL_INFOTABLE)
    assert len(holdings) == 1
    assert holdings[0]["shares"] == 12561737
    assert holdings[0]["sshPrnamtType"] == "SH"
    assert holdings[0]["value_usd_thousands"] == 577211815


def test_target_report_date_follows_the_45_day_deadline():
    t = ff.target_report_date
    assert t("2026-09-06") == "2026-06-30"
    assert t("2026-08-16") == "2026-06-30"   # just past the deadline
    assert t("2026-08-13") == "2026-03-31"   # deadline not reached yet
    assert t("2026-02-16") == "2025-12-31"
    assert t("2026-01-10") == "2025-12-31"


class _Resp:
    def __init__(self, payload): self._p = payload
    def json(self): return self._p


def test_amendment_supersedes_the_original_for_the_same_quarter(monkeypatch):
    """A 13F-HR/A restates the original; the latest filing for the quarter wins."""
    recent = {
        "form":            ["13F-HR/A", "13F-HR", "13F-HR"],
        "accessionNumber": ["acc-amend", "acc-orig", "acc-old"],
        "filingDate":      ["2026-08-20", "2026-08-14", "2026-05-15"],
        "reportDate":      ["2026-06-30", "2026-06-30", "2026-03-31"],
        "primaryDocument": ["a.xml", "b.xml", "c.xml"],
    }
    monkeypatch.setattr(ff, "edgar_get", lambda url: _Resp({"filings": {"recent": recent}}))
    got = ff.get_latest_13f_filing("0000000001", "2026-06-30")
    assert got["accessionNumber"] == "acc-amend"
    assert got["isAmendment"] is True


def test_a_filer_without_the_target_quarter_is_excluded(monkeypatch):
    recent = {
        "form":            ["13F-HR"],
        "accessionNumber": ["acc-old"],
        "filingDate":      ["2025-11-14"],
        "reportDate":      ["2025-09-30"],
        "primaryDocument": ["c.xml"],
    }
    monkeypatch.setattr(ff, "edgar_get", lambda url: _Resp({"filings": {"recent": recent}}))
    assert ff.get_latest_13f_filing("0000000001", "2026-06-30") is None


def test_a_us_ordinary_share_is_preferred_over_any_first_hit():
    """Without a US equity match the old code took the first FIGI row of any
    kind, so a warrant could enter the universe wearing a plausible ticker."""
    mapping, meta = {}, {}
    ff._record_figi_match("X", [
        {"ticker": "WTF", "exchCode": "LN", "marketSector": "Equity", "securityType2": "Warrant"},
        {"ticker": "ABC", "exchCode": "UN", "marketSector": "Equity", "securityType2": "Common Stock"},
    ], mapping, meta)
    assert mapping["X"] == "ABC"
    assert meta["X"]["security_type_validated"] is True


def test_a_non_equity_match_is_recorded_as_unvalidated():
    mapping, meta = {}, {}
    ff._record_figi_match("Y", [
        {"ticker": "WRNT", "exchCode": "LN", "marketSector": "Equity", "securityType2": "Warrant"},
    ], mapping, meta)
    assert meta["Y"]["security_type_validated"] is False
    assert meta["Y"]["security_type"] == "Warrant"

    mapping2, meta2 = {}, {}
    ff._record_figi_match("Z", [
        {"ticker": "BOND", "exchCode": "UN", "marketSector": "Corp", "securityType2": "Note"},
    ], mapping2, meta2)
    assert meta2["Z"]["security_type_validated"] is False
