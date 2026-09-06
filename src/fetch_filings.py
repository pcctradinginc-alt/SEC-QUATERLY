"""
fetch_filings.py
Fetches the latest 13F-HR filings for all configured filers from SEC EDGAR.

Uses the EDGAR full-text search API to find the infotable XML directly,
bypassing the unreliable index.json approach.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import requests

import os

from config import (
    run_date,
    DATA_DIR, FILERS, OPENFIGI_API_KEY, OPENFIGI_BATCH, OPENFIGI_URL,
    SEC_HEADERS, SEC_RATE_LIMIT_SLEEP,
)


def _sleep():
    time.sleep(SEC_RATE_LIMIT_SLEEP)


def edgar_get(url: str) -> requests.Response:
    _sleep()
    resp = requests.get(url, headers=SEC_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp


# ── Step 1: Get the 13F filing FOR THE TARGET QUARTER ────────────────────────

def target_report_date(as_of: str | None = None) -> str:
    """
    The quarter-end this run is about: the most recent quarter-end whose 13F
    deadline (45 days later) has passed.

    Taking whatever 13F a CIK filed most recently is wrong - a filer that has
    not reported yet would contribute a year-old book, and that book would then
    be diffed against the current quarter as if it were fresh.
    """
    override = os.environ.get("SEC_TARGET_REPORT_DATE", "").strip()
    if override:
        return override
    today = date.fromisoformat(as_of or run_date())
    ends = [date(today.year - 1, 12, 31), date(today.year, 3, 31),
            date(today.year, 6, 30), date(today.year, 9, 30), date(today.year, 12, 31)]
    due = [q for q in ends if (today - q).days >= 45]
    return max(due).isoformat() if due else ends[0].isoformat()


def get_latest_13f_filing(cik: str, want_report_date: str | None = None) -> dict | None:
    """Latest 13F-HR / 13F-HR/A whose reportDate matches the target quarter."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        data = edgar_get(url).json()
    except Exception as e:
        print(f"  ⚠️  Could not fetch submissions for CIK {cik}: {e}")
        return None

    filings      = data.get("filings", {}).get("recent", {})
    forms        = filings.get("form", [])
    accessions   = filings.get("accessionNumber", [])
    dates        = filings.get("filingDate", [])
    primary_docs = filings.get("primaryDocument", [])

    report_dates = filings.get("reportDate", [])

    want = want_report_date or target_report_date()

    # Collect every filing for the target quarter, then take the one filed last.
    # A 13F-HR/A restates the original, so the newest filing is the truth; an
    # amendment wins a tie on the same filing date. Relying on EDGAR's array
    # order alone would leave that to an undocumented assumption.
    matches, seen_quarters = [], []
    for i, form in enumerate(forms):
        if form not in ("13F-HR", "13F-HR/A"):
            continue
        rd = report_dates[i] if i < len(report_dates) else dates[i]
        if rd != want:
            seen_quarters.append(rd)
            continue
        matches.append({
            "cik":             cik,
            "accessionNumber": accessions[i],
            "filingDate":      dates[i],
            # reportDate = quarter-end (period of report). Always earlier than
            # filingDate, and used as the price anchor and the Form 4 window start.
            "reportDate":      rd,
            "form":            form,
            "isAmendment":     form == "13F-HR/A",
            "primaryDocument": primary_docs[i] if i < len(primary_docs) else "",
        })

    if matches:
        best = max(matches, key=lambda m: (m["filingDate"], m["isAmendment"]))
        if len(matches) > 1:
            print(f"  ↺ {len(matches)} filings for {want}; using {best['form']} "
                  f"filed {best['filingDate']}")
        return best

    if seen_quarters:
        print(f"  ⏭️  STALE_FILER: no 13F for {want} (newest on file: {max(seen_quarters)}) - excluded")
    else:
        print(f"  ℹ️  No 13F-HR found for CIK {cik}")
    return None


# ── Step 2: Get all files in filing and find infotable ────────────────────────

def get_filing_files(cik: str, accession: str) -> list[dict]:
    """
    Fetches the list of files in a filing using the EDGAR submissions API.
    Returns list of {name, type} dicts.
    """
    cik_int     = int(cik)
    acc_nodash  = accession.replace("-", "")

    # Try index.json (directory listing)
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/index.json"
    try:
        resp = edgar_get(url)
        data = resp.json()
        items = data.get("directory", {}).get("item", [])
        if isinstance(items, dict):
            items = [items]
        if items:
            print(f"    Files: {[i.get('name','') for i in items]}")
            return items
    except Exception as e:
        print(f"    ⚠️  index.json failed: {e}")

    # Fallback: try the EDGAR filing index page as text
    url2 = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{accession}-index.htm"
    try:
        resp2 = edgar_get(url2)
        # Parse filenames from HTML
        import re
        names = re.findall(r'href="([^"]+\.xml)"', resp2.text, re.IGNORECASE)
        items = [{"name": n.split("/")[-1]} for n in names]
        if items:
            print(f"    Files (from htm): {[i['name'] for i in items]}")
            return items
    except Exception as e:
        print(f"    ⚠️  index.htm also failed: {e}")

    return []


# The cover page is never the holdings table. Everything else is a candidate.
_COVER_DOC_NAMES = ("primary_doc.xml", "primarydoc.xml")


def find_infotable_filename(items: list[dict]) -> str | None:
    """
    Find the information-table XML among a filing's files.

    Filers name this file inconsistently: `informationtable.xml`,
    `form13fInfoTable.xml`, `Form13FInfoTable.xml`,
    `form13f-1786738348_infotable.xml`, ... The common substring is
    "infotable", so that is matched first (case-insensitively).

    The cover page (`primary_doc.xml`) is excluded at every step: it parses
    as valid XML but contains no <infoTable> entries, so selecting it makes
    the filer silently drop out of the run with zero holdings.
    """
    xml_files = [
        i["name"] for i in items
        if i.get("name", "").lower().endswith(".xml")
        and i["name"].lower() not in _COVER_DOC_NAMES
    ]

    # Pass 1: the usual naming - "infotable" covers "informationtable" too
    for name in xml_files:
        if "infotable" in name.lower() or "informationtable" in name.lower():
            return name

    # Pass 2: any remaining XML that is not a cover/summary/header document
    skip_keywords = ("cover", "summary", "header")
    for name in xml_files:
        if not any(k in name.lower() for k in skip_keywords):
            return name

    # Pass 3: whatever XML is left (cover page already excluded above)
    return xml_files[0] if xml_files else None


_DATE_IN_NAME = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})|(\d{2})(\d{2})(20\d{2})")


def verify_period_of_report(filing_meta: dict, filename: str) -> bool:
    """
    A filer may name its information table anything - SurgoCap ships a Q2-2026
    filing whose table is called `Surgo_13F_09302025.xml`. The filename is not
    evidence either way, so when it carries a date that contradicts the target
    quarter, check the filing's own cover page (`primary_doc.xml`), which is the
    authoritative period of report.
    """
    want = filing_meta.get("reportDate", "")
    m = _DATE_IN_NAME.search(filename or "")
    if not m or not want:
        return True
    groups = [g for g in m.groups() if g]
    stamp = "".join(groups)
    if want.replace("-", "") in stamp:
        return True

    cik_int = int(filing_meta["cik"])
    acc = filing_meta["accessionNumber"].replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/primary_doc.xml"
    try:
        text = edgar_get(url).text
    except Exception:
        return True                      # cover page unavailable: trust SEC metadata
    found = re.search(r"<periodOfReport>([^<]+)</periodOfReport>", text)
    if not found:
        return True
    period = found.group(1).strip()
    normalised = period
    if "-" in period and len(period) == 10 and period[2] == "-":      # MM-DD-YYYY
        mm, dd, yyyy = period.split("-")
        normalised = f"{yyyy}-{mm}-{dd}"
    ok = normalised == want
    if not ok:
        print(f"    ⚠️  {filename}: cover page reports period {normalised}, expected {want}")
    else:
        print(f"    ✓ filename suggests another period; cover page confirms {want}")
    return ok


def download_infotable(filing_meta: dict) -> str | None:
    cik_int    = int(filing_meta["cik"])
    accession  = filing_meta["accessionNumber"]
    acc_nodash = accession.replace("-", "")

    items = get_filing_files(filing_meta["cik"], accession)
    infotable_filename = find_infotable_filename(items)

    if not infotable_filename:
        # Last resort: try common filename patterns directly
        candidates = [
            "informationtable.xml",
            f"{acc_nodash}-informationtable.xml",
            "form13fInfoTable.xml",
            "Form13FInfoTable.xml",
            "Form13fInfoTable.xml",
            "infotable.xml",
        ]
        for candidate in candidates:
            url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{candidate}"
            try:
                resp = edgar_get(url)
                if resp.status_code == 200 and "<" in resp.text:
                    print(f"    ✅ Found via direct guess: {candidate}")
                    return resp.text
            except Exception:
                continue

        print(f"    ⚠️  Could not find infotable XML for {accession}")
        return None

    if not verify_period_of_report(filing_meta, infotable_filename):
        print(f"    ⚠️  Rejecting {infotable_filename}: it does not belong to "
              f"{filing_meta.get('reportDate')}")
        return None

    xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{infotable_filename}"
    try:
        xml_text = edgar_get(xml_url).text
        print(f"    ✅ Downloaded: {infotable_filename} ({len(xml_text)} chars)")
        return xml_text
    except Exception as e:
        print(f"    ⚠️  Could not download {infotable_filename}: {e}")
        return None


# ── Step 3: Parse XML holdings ────────────────────────────────────────────────

def parse_infotable(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"    ⚠️  XML parse error: {e}")
        return []

    tag = root.tag
    ns_uri = ""
    if tag.startswith("{"):
        ns_uri = tag[1:tag.index("}")]

    ns     = {"ns": ns_uri} if ns_uri else {}
    prefix = "ns:" if ns_uri else ""

    holdings = []
    for entry in root.findall(f".//{prefix}infoTable", ns):
        def _t(tag_name):
            """
            Search anywhere below <infoTable>, not just its direct children.

            The share count lives nested:
                <shrsOrPrnAmt><sshPrnamt>12561737</sshPrnamt></shrsOrPrnAmt>
            A direct-child lookup returns nothing and silently yields 0 shares -
            which made every position look NEW, produced zero EXITs across the
            whole universe and disabled the share-count delta the ranking is
            built on.
            """
            el = entry.find(f"{prefix}{tag_name}", ns)
            if el is None:
                el = entry.find(f".//{prefix}{tag_name}", ns)
            return el.text.strip() if el is not None and el.text else ""

        try:
            value_raw  = _t("value")
            shares_raw = _t("sshPrnamt")
            holding = {
                "cusip":               _t("cusip"),
                "nameOfIssuer":        _t("nameOfIssuer"),
                "titleOfClass":        _t("titleOfClass"),
                "value_usd_thousands": int(value_raw.replace(",", "")) if value_raw else 0,
                "shares":              int(shares_raw.replace(",", "")) if shares_raw else 0,
                "sshPrnamtType":       _t("sshPrnamtType"),
                "putCall":             _t("putCall") or None,
                "investmentDiscretion":_t("investmentDiscretion"),
            }
            if holding["cusip"]:
                holdings.append(holding)
        except (ValueError, AttributeError) as e:
            continue

    return holdings


# ── Step 4: CUSIP → Ticker via OpenFIGI ──────────────────────────────────────

def _openfigi_headers() -> dict:
    """Returns headers for OpenFIGI. Uses API key from env if available."""
    api_key = os.environ.get("OPENFIGI_API_KEY", OPENFIGI_API_KEY).strip()
    h = {"Content-Type": "application/json"}
    if api_key:
        h["X-OPENFIGI-APIKEY"] = api_key
    return h


# OpenFIGI marketSector / securityType2 values that mean "an ordinary share we
# can price and trade". Anything else (warrants, rights, units, depositary
# receipts on a foreign line, bonds) is not the security the signal is about.
_TRADABLE_SECURITY_TYPES = {
    "COMMON STOCK", "REIT", "MUTUAL FUND", "ETP", "DEPOSITARY RECEIPT",
    "CLOSED-END FUND", "ROYALTY TRUST", "TRACKING STOCK",
}
_US_EXCHANGES = ("US", "UN", "UW", "UA", "UQ", "UR", "UV")


def _figi_is_tradable_equity(item: dict) -> bool:
    if (item.get("marketSector") or "").upper() != "EQUITY":
        return False
    stype = (item.get("securityType2") or item.get("securityType") or "").upper()
    return not stype or stype in _TRADABLE_SECURITY_TYPES


def map_cusips_to_tickers(cusips: list[str]) -> dict[str, str]:
    """
    {cusip: ticker} plus, in `map_cusips_to_tickers.metadata`, what OpenFIGI
    said the instrument actually is.

    A syntactically valid symbol is not proof of a tradable US common share:
    without a US equity match the old code fell back to the first FIGI hit of
    any kind, so a warrant or a foreign line could enter the universe wearing a
    plausible ticker.
    """
    mapping = {}
    meta: dict[str, dict] = {}
    map_cusips_to_tickers.metadata = meta
    if not cusips:
        return mapping

    headers       = _openfigi_headers()
    unique_cusips = list(set(cusips))
    total         = len(unique_cusips)

    for i in range(0, total, OPENFIGI_BATCH):
        batch   = unique_cusips[i:i + OPENFIGI_BATCH]
        payload = [{"idType": "ID_CUSIP", "idValue": c} for c in batch]
        try:
            resp = requests.post(OPENFIGI_URL, json=payload, headers=headers, timeout=20)
            if resp.status_code == 429:
                time.sleep(60)
                resp = requests.post(OPENFIGI_URL, json=payload, headers=headers, timeout=20)
            if resp.status_code == 413:
                # Batch too large for unauthenticated limit (10 items max).
                # Retry the same batch in chunks of 10 automatically.
                print(f"  ⚠️  OpenFIGI 413 – batch {len(batch)} too large, retrying in chunks of 10…")
                for j in range(0, len(batch), 10):
                    chunk   = batch[j:j + 10]
                    c_payload = [{"idType": "ID_CUSIP", "idValue": c} for c in chunk]
                    try:
                        r2 = requests.post(OPENFIGI_URL, json=c_payload, headers=headers, timeout=20)
                        if r2.status_code == 200:
                            for cusip, result in zip(chunk, r2.json()):
                                if "data" in result and result["data"]:
                                    _record_figi_match(cusip, result["data"], mapping, meta)
                    except Exception as e2:
                        print(f"  ⚠️  OpenFIGI chunk failed: {e2}")
                    time.sleep(0.5)
                continue
            if resp.status_code != 200:
                print(f"  ⚠️  OpenFIGI returned HTTP {resp.status_code}, skipping batch")
                continue
            results = resp.json()
            for cusip, result in zip(batch, results):
                if "data" in result and result["data"]:
                    _record_figi_match(cusip, result["data"], mapping, meta)
        except Exception as e:
            print(f"  ⚠️  OpenFIGI batch failed: {e}")
        time.sleep(0.5)

    return mapping


def _record_figi_match(cusip: str, data: list[dict], mapping: dict, meta: dict) -> None:
    """Prefer a US-listed ordinary share; record what was actually matched."""
    for item in data:
        if (item.get("exchCode") or "") in _US_EXCHANGES and _figi_is_tradable_equity(item):
            mapping[cusip] = item.get("ticker", "")
            meta[cusip] = {"exchange": item.get("exchCode"), "market_sector": item.get("marketSector"),
                           "security_type": item.get("securityType2") or item.get("securityType"),
                           "security_type_validated": True}
            return
    first = data[0]
    mapping[cusip] = first.get("ticker", "")
    meta[cusip] = {"exchange": first.get("exchCode"), "market_sector": first.get("marketSector"),
                   "security_type": first.get("securityType2") or first.get("securityType"),
                   "security_type_validated": False}


# ── Step 4b: SEC name-based ticker fallback ──────────────────────────────────

def _build_sec_name_map() -> dict[str, str]:
    """
    Downloads SEC's company_tickers.json (free, no key, no rate limit).
    Returns {normalised_name: ticker} for ~10k US-listed companies.
    Used as fallback when OpenFIGI CUSIP mapping fails.
    """
    # NOTE: this file is served from www.sec.gov - data.sec.gov returns 404.
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        resp = edgar_get(url)
        data = resp.json()
    except Exception as e:
        print(f"  ⚠️  SEC ticker map download failed: {e}")
        return {}

    name_map: dict[str, str] = {}
    suffix_strip = (
        " INC", " CORP", " LTD", " LLC", " LP", " PLC",
        " CO", " HOLDINGS", " GROUP", " TRUST", " FUND",
        " INCORPORATED", " CORPORATION", " LIMITED",
    )
    for entry in data.values():
        raw    = entry.get("title", "").upper().strip()
        ticker = entry.get("ticker", "").strip()
        if not raw or not ticker:
            continue
        name_map[raw] = ticker
        # Also index the name with trailing legal suffixes removed
        for sfx in suffix_strip:
            if raw.endswith(sfx):
                name_map[raw[: -len(sfx)].strip()] = ticker
                break
    return name_map


def _sec_ticker_fallback(
    all_data: dict, cusip_to_ticker: dict[str, str]
) -> dict[str, str]:
    """
    For every CUSIP that OpenFIGI could not resolve, attempts a name-based
    lookup against the SEC company_tickers.json master list.
    Returns a dict of newly discovered {cusip: ticker} pairs.
    """
    unresolved: list[dict] = []
    for filer_data in all_data.values():
        for h in filer_data.get("holdings", []):
            if h.get("cusip") and not cusip_to_ticker.get(h["cusip"]):
                unresolved.append(h)

    if not unresolved:
        return {}

    # De-duplicate by CUSIP – keep first seen nameOfIssuer
    cusip_to_name: dict[str, str] = {}
    for h in unresolved:
        c = h["cusip"]
        if c not in cusip_to_name:
            cusip_to_name[c] = h.get("nameOfIssuer", "").upper().strip()

    print(f"\n🔍 SEC name fallback for {len(cusip_to_name)} unresolved CUSIPs…")
    sec_map = _build_sec_name_map()
    if not sec_map:
        return {}

    suffix_strip = (
        " INC", " CORP", " LTD", " LLC", " LP", " PLC",
        " CO", " HOLDINGS", " GROUP", " TRUST", " FUND",
    )
    resolved: dict[str, str] = {}
    for cusip, name in cusip_to_name.items():
        # Try exact match
        if name in sec_map:
            resolved[cusip] = sec_map[name]
            continue
        # Try with trailing suffix removed
        for sfx in suffix_strip:
            if name.endswith(sfx):
                short = name[: -len(sfx)].strip()
                if short in sec_map:
                    resolved[cusip] = sec_map[short]
                    break

    if resolved:
        print(f"   ✅ SEC fallback resolved {len(resolved)} additional CUSIPs")
    else:
        print(f"   ℹ️  SEC fallback: no additional matches found")
    return resolved


# ── Step 5: Split check ───────────────────────────────────────────────────────

def _is_valid_equity_ticker(ticker: str) -> bool:
    """
    Rejects non-equity strings that OpenFIGI sometimes returns:
      - Bond descriptions like 'BRKR 6.375 09/01/28'
      - Multi-word strings
      - Tickers longer than 6 chars (options, bonds)
    """
    if not ticker or " " in ticker:
        return False
    if len(ticker) > 6:
        return False
    # Must start with a letter (real equity tickers never start with a digit)
    if not ticker[0].isalpha():
        return False
    import re
    return bool(re.match(r'^[A-Z0-9./\-]+$', ticker.upper()))


def check_recent_splits(tickers: list[str]) -> dict[str, float]:
    try:
        import yfinance as yf
        from datetime import timedelta
    except ImportError:
        return {}

    splits = {}
    cutoff = date.today() - timedelta(days=120)

    for ticker in tickers:
        if not _is_valid_equity_ticker(ticker):
            continue
        yf_ticker = ticker.replace("/", "-")   # BRK/B → BRK-B for yfinance
        try:
            hist   = yf.Ticker(yf_ticker).splits
            if hist.empty:
                continue
            recent = hist[hist.index.date >= cutoff]
            if not recent.empty:
                ratio = float(recent.iloc[-1])
                splits[ticker] = ratio   # store under original ticker key
                print(f"  ⚠️  Split: {ticker} ratio {ratio}")
        except Exception:
            pass
    return splits


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    today_str   = run_date()
    output_path = DATA_DIR / f"{today_str}_raw_holdings.json"

    all_data   = {}
    all_cusips = set()

    print(f"\n{'='*60}")
    print(f"SEC EDGAR Fetch – {today_str}")
    print(f"{'='*60}")

    want = target_report_date(today_str)
    print(f"Target quarter (period of report): {want}\n")

    for name, cik in FILERS.items():
        print(f"\n▶ {name} (CIK: {cik})")

        filing_meta = get_latest_13f_filing(cik, want)
        if not filing_meta:
            all_data[name] = {"error": "stale_or_missing_filing", "cik": cik}
            continue

        print(f"  Filing: {filing_meta['form']} on {filing_meta['filingDate']}"
              + (" ⚠️ AMENDMENT" if filing_meta["isAmendment"] else ""))

        xml_text = download_infotable(filing_meta)
        if not xml_text:
            all_data[name] = {"error": "no_xml", "cik": cik, "meta": filing_meta}
            continue

        holdings = parse_infotable(xml_text)
        if not holdings:
            print(f"  ⚠️  {name}: downloaded XML contained no <infoTable> rows - "
                  f"wrong document selected? Filer contributes NOTHING to this run.")
            all_data[name] = {"error": "no_holdings_parsed", "cik": cik, "meta": filing_meta}
            continue

        print(f"  ✅ {len(holdings)} positions parsed")

        # AQR and similar quant funds file 10k+ positions (index replication).
        # Cluster signal from a quant fund holding 20k stocks is meaningless.
        # Cap at top 500 by value so they don't flood the CUSIP pool.
        MAX_POSITIONS_PER_FILER = 500
        original_count = len(holdings)
        # Portfolio weights must divide by the FULL reported book. Capping first
        # shrinks the denominator and inflates every remaining position's weight
        # (a $3bn slice of a $100bn book would read as 4.3% of $70bn).
        full_value = sum(h["value_usd_thousands"] for h in holdings)
        if original_count > MAX_POSITIONS_PER_FILER:
            holdings = sorted(holdings, key=lambda h: h["value_usd_thousands"], reverse=True)
            holdings = holdings[:MAX_POSITIONS_PER_FILER]
            print(f"  ✂️  Capped to top {MAX_POSITIONS_PER_FILER} positions by value "
                  f"(was {original_count} total)")

        for h in holdings:
            if h["cusip"]:
                all_cusips.add(h["cusip"])

        all_data[name] = {
            "cik":        cik,
            "meta":       filing_meta,
            "holdings":   holdings,
            "full_reported_value": full_value,
            "full_position_count": original_count,
            "is_capped":  original_count > MAX_POSITIONS_PER_FILER,
            "total_value":sum(h["value_usd_thousands"] for h in holdings),
            "fetched_at": datetime.utcnow().isoformat(),
        }

    print(f"\n🔍 Mapping {len(all_cusips)} CUSIPs to tickers via OpenFIGI...")
    cusip_to_ticker = map_cusips_to_tickers(list(all_cusips))
    figi_meta = getattr(map_cusips_to_tickers, "metadata", {})
    validated = sum(1 for m in figi_meta.values() if m.get("security_type_validated"))
    print(f"   OpenFIGI mapped: {len(cusip_to_ticker)} / {len(all_cusips)} "
          f"({validated} confirmed as US-listed ordinary shares)")

    # Fallback: SEC company_tickers.json for any CUSIP OpenFIGI couldn't resolve
    sec_extra = _sec_ticker_fallback(all_data, cusip_to_ticker)
    cusip_to_ticker.update(sec_extra)
    if sec_extra:
        print(f"   Total after SEC fallback: {len(cusip_to_ticker)} / {len(all_cusips)}")

    all_tickers = set()
    for filer_data in all_data.values():
        if "holdings" not in filer_data:
            continue
        for h in filer_data["holdings"]:
            ticker = cusip_to_ticker.get(h["cusip"], "")
            h["ticker"] = ticker if _is_valid_equity_ticker(ticker) else ""
            if h["ticker"]:
                all_tickers.add(h["ticker"])

    print(f"\n🔀 Checking splits on {len(all_tickers)} tickers...")
    splits = check_recent_splits(list(all_tickers))

    output = {
        "date":            today_str,
        "report_date":     want,
        "cusip_to_ticker": cusip_to_ticker,
        "cusip_security_meta": figi_meta,
        "recent_splits":   splits,
        "filers":          all_data,
    }

    # Atomic write: write to temp file first, then rename (prevents corrupt files on crash)
    tmp_path = output_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    tmp_path.replace(output_path)

    filers_ok = sum(1 for v in all_data.values() if "holdings" in v)
    print(f"\n✅ Saved to {output_path}")
    print(f"   Filers with data: {filers_ok} / {len(FILERS)}")

    dropped = {n: v.get("error") for n, v in all_data.items() if "holdings" not in v}
    if dropped:
        print("   Filers with NO data:")
        for n, err in sorted(dropped.items()):
            print(f"     - {n}: {err}")

    missing = len(FILERS) - filers_ok
    if missing > len(FILERS) * 0.3:
        raise RuntimeError(f"Too many filers missing: {missing}/{len(FILERS)}")


if __name__ == "__main__":
    run()
