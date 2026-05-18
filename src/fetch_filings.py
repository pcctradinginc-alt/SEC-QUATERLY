"""
fetch_filings.py
Fetches the latest 13F-HR filings for all configured filers from SEC EDGAR.

Uses the EDGAR full-text search API to find the infotable XML directly,
bypassing the unreliable index.json approach.
"""

import json
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import requests

import os

from config import (
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


# ── Step 1: Get latest 13F filing metadata ────────────────────────────────────

def get_latest_13f_filing(cik: str) -> dict | None:
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

    for i, form in enumerate(forms):
        if form in ("13F-HR", "13F-HR/A"):
            return {
                "cik":             cik,
                "accessionNumber": accessions[i],
                "filingDate":      dates[i],
                # reportDate = quarter-end (period of report), e.g. 2024-12-31.
                # Always earlier than filingDate (which can be up to 45 days later).
                # Use this as the price-comparison anchor so we measure from
                # when the manager actually held the position, not when they disclosed it.
                "reportDate":      report_dates[i] if i < len(report_dates) else dates[i],
                "form":            form,
                "isAmendment":     form == "13F-HR/A",
                "primaryDocument": primary_docs[i] if i < len(primary_docs) else "",
            }

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


def find_infotable_filename(items: list[dict]) -> str | None:
    """Find the information table XML from list of filing files."""
    # Pass 1: name contains 'informationtable'
    for item in items:
        name = item.get("name", "").lower()
        if "informationtable" in name and name.endswith(".xml"):
            return item["name"]

    # Pass 2: any xml that isn't the primary/cover/summary doc
    skip_keywords = ["primary", "cover", "summary", "header", "form13f"]
    for item in items:
        name = item.get("name", "").lower()
        if name.endswith(".xml") and not any(k in name for k in skip_keywords):
            return item["name"]

    # Pass 3: second xml file (first is usually primary doc)
    xml_files = [i["name"] for i in items if i.get("name","").lower().endswith(".xml")]
    if len(xml_files) >= 2:
        return xml_files[1]
    if len(xml_files) == 1:
        return xml_files[0]

    return None


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
            el = entry.find(f"{prefix}{tag_name}", ns)
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


def map_cusips_to_tickers(cusips: list[str]) -> dict[str, str]:
    mapping = {}
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
                                    for figi_item in result["data"]:
                                        ticker = figi_item.get("ticker", "")
                                        exch   = figi_item.get("exchCode", "")
                                        if exch in ("US", "UN", "UW", "UA"):
                                            mapping[cusip] = ticker
                                            break
                                    else:
                                        mapping[cusip] = result["data"][0].get("ticker", "")
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
                    for figi_item in result["data"]:
                        ticker = figi_item.get("ticker", "")
                        exch   = figi_item.get("exchCode", "")
                        if exch in ("US", "UN", "UW", "UA"):
                            mapping[cusip] = ticker
                            break
                    else:
                        mapping[cusip] = result["data"][0].get("ticker", "")
        except Exception as e:
            print(f"  ⚠️  OpenFIGI batch failed: {e}")
        time.sleep(0.5)

    return mapping


# ── Step 4b: SEC name-based ticker fallback ──────────────────────────────────

def _build_sec_name_map() -> dict[str, str]:
    """
    Downloads SEC's company_tickers.json (free, no key, no rate limit).
    Returns {normalised_name: ticker} for ~10k US-listed companies.
    Used as fallback when OpenFIGI CUSIP mapping fails.
    """
    url = "https://data.sec.gov/files/company_tickers.json"
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
    today_str   = date.today().isoformat()
    output_path = DATA_DIR / f"{today_str}_raw_holdings.json"

    all_data   = {}
    all_cusips = set()

    print(f"\n{'='*60}")
    print(f"SEC EDGAR Fetch – {today_str}")
    print(f"{'='*60}")

    for name, cik in FILERS.items():
        print(f"\n▶ {name} (CIK: {cik})")

        filing_meta = get_latest_13f_filing(cik)
        if not filing_meta:
            all_data[name] = {"error": "no_filing", "cik": cik}
            continue

        print(f"  Filing: {filing_meta['form']} on {filing_meta['filingDate']}"
              + (" ⚠️ AMENDMENT" if filing_meta["isAmendment"] else ""))

        xml_text = download_infotable(filing_meta)
        if not xml_text:
            all_data[name] = {"error": "no_xml", "cik": cik, "meta": filing_meta}
            continue

        holdings = parse_infotable(xml_text)
        if not holdings:
            all_data[name] = {"error": "no_holdings_parsed", "cik": cik, "meta": filing_meta}
            continue

        print(f"  ✅ {len(holdings)} positions parsed")

        # AQR and similar quant funds file 10k+ positions (index replication).
        # Cluster signal from a quant fund holding 20k stocks is meaningless.
        # Cap at top 500 by value so they don't flood the CUSIP pool.
        MAX_POSITIONS_PER_FILER = 500
        original_count = len(holdings)
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
            "total_value":sum(h["value_usd_thousands"] for h in holdings),
            "fetched_at": datetime.utcnow().isoformat(),
        }

    print(f"\n🔍 Mapping {len(all_cusips)} CUSIPs to tickers via OpenFIGI...")
    cusip_to_ticker = map_cusips_to_tickers(list(all_cusips))
    print(f"   OpenFIGI mapped: {len(cusip_to_ticker)} / {len(all_cusips)}")

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
        "cusip_to_ticker": cusip_to_ticker,
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

    missing = len(FILERS) - filers_ok
    if missing > len(FILERS) * 0.3:
        raise RuntimeError(f"Too many filers missing: {missing}/{len(FILERS)}")


if __name__ == "__main__":
    run()
