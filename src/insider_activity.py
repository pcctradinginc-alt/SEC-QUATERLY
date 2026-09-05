"""
insider_activity.py
SEC Form 4 insider-transaction look-up for the signal engine.

For each candidate ticker, fetches every Form 4 filed with the issuer on
EDGAR since the last 13F quarter-end, parses the non-derivative table and
aggregates open-market purchases (transaction code "P", acquired) and
open-market sales (code "S", disposed). The result feeds the 15-point
"insider" factor of the deterministic SIGNAL SCORE.

Determinism: results are cached on disk per (ticker, window_start, as_of)
so a re-run on the same day reads the identical snapshot instead of
re-querying EDGAR. The score transform itself is a pure function.

Only data the SEC publishes is used - no third-party insider feeds.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import date

import requests

from config import (
    INSIDER_CACHE_DIR, INSIDER_CLUSTER_FULL_COUNT, INSIDER_MAX_FORM4_PER_TICKER,
    INSIDER_MIN_PURCHASE_USD, INSIDER_VALUE_FULL_USD, SEC_HEADERS,
    SEC_RATE_LIMIT_SLEEP,
)

EDGAR_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_SUBMISSIONS    = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_ARCHIVE        = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"


def _get(url: str, timeout: int = 30) -> requests.Response:
    time.sleep(SEC_RATE_LIMIT_SLEEP)
    resp = requests.get(url, headers=SEC_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp


# ── Ticker → issuer CIK ───────────────────────────────────────────────────────

_TICKER_MAP_CACHE: dict[str, str] | None = None


def load_ticker_cik_map() -> dict[str, str]:
    """{TICKER: 10-digit CIK} from SEC's master list (cached per process + on disk for the day)."""
    global _TICKER_MAP_CACHE
    if _TICKER_MAP_CACHE is not None:
        return _TICKER_MAP_CACHE

    disk = INSIDER_CACHE_DIR / f"ticker_cik_map_{date.today().isoformat()}.json"
    if disk.exists():
        _TICKER_MAP_CACHE = json.load(open(disk))
        return _TICKER_MAP_CACHE

    mapping: dict[str, str] = {}
    try:
        data = _get(EDGAR_TICKER_MAP_URL).json()
        for entry in data.values():
            t = (entry.get("ticker") or "").upper().strip()
            cik = entry.get("cik_str")
            if t and cik:
                mapping[t] = str(cik).zfill(10)
    except Exception as e:
        print(f"  ⚠️  Could not download SEC ticker map: {e}")

    if mapping:
        with open(disk, "w") as f:
            json.dump(mapping, f)
    _TICKER_MAP_CACHE = mapping
    return mapping


def ticker_to_cik(ticker: str) -> str | None:
    m = load_ticker_cik_map()
    t = ticker.upper().strip()
    for cand in (t, t.replace("/", "-"), t.replace("/", "."), t.replace(".", "-")):
        if cand in m:
            return m[cand]
    return None


# ── Form 4 discovery ──────────────────────────────────────────────────────────

def list_form4_filings(cik: str, since: str, until: str) -> list[dict]:
    """
    All Form 4 / 4/A filings for the issuer with `since` < filingDate <= `until`,
    newest first, capped at INSIDER_MAX_FORM4_PER_TICKER.
    """
    try:
        data = _get(EDGAR_SUBMISSIONS.format(cik=cik)).json()
    except Exception as e:
        print(f"    ⚠️  submissions fetch failed for CIK {cik}: {e}")
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms  = recent.get("form", [])
    accs   = recent.get("accessionNumber", [])
    fdates = recent.get("filingDate", [])
    docs   = recent.get("primaryDocument", [])

    out = []
    for i, form in enumerate(forms):
        if form not in ("4", "4/A"):
            continue
        fd = fdates[i]
        if fd <= since or fd > until:
            continue
        out.append({
            "accession":   accs[i],
            "filing_date": fd,
            "form":        form,
            "primary_doc": docs[i] if i < len(docs) else "",
        })
        if len(out) >= INSIDER_MAX_FORM4_PER_TICKER:
            break
    return out


def _raw_xml_name(primary_doc: str) -> str:
    # EDGAR lists the XSL-rendered path (xslF345X05/wk-form4_123.xml);
    # the raw XML is the same filename without the stylesheet directory.
    return primary_doc.split("/")[-1]


def fetch_form4_xml(cik: str, filing: dict) -> str | None:
    if not filing.get("primary_doc"):
        return None
    url = EDGAR_ARCHIVE.format(
        cik_int=int(cik),
        acc_nodash=filing["accession"].replace("-", ""),
        doc=_raw_xml_name(filing["primary_doc"]),
    )
    try:
        text = _get(url).text
        return text if "<ownershipDocument" in text else None
    except Exception as e:
        print(f"    ⚠️  Form 4 download failed {filing['accession']}: {e}")
        return None


# ── Form 4 parsing ────────────────────────────────────────────────────────────

def _val(el, path: str) -> str:
    node = el.find(path)
    if node is None:
        return ""
    v = node.find("value")
    txt = (v.text if v is not None else node.text) or ""
    return txt.strip()


def _num(s: str) -> float:
    try:
        return float(s.replace(",", "")) if s else 0.0
    except ValueError:
        return 0.0


def parse_form4(xml_text: str) -> dict:
    """
    Returns {
      issuer: {cik, name, symbol},
      owners: [{name, is_director, is_officer, is_ten_pct, title}],
      transactions: [{date, code, acquired, shares, price, value_usd, security}]
    }
    Only the non-derivative table counts (open-market share transactions);
    derivative exercises/grants are not "insider buying" in the conviction sense.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {"issuer": {}, "owners": [], "transactions": []}

    issuer = {
        "cik":    (root.findtext("issuer/issuerCik") or "").strip().zfill(10),
        "name":   (root.findtext("issuer/issuerName") or "").strip(),
        "symbol": (root.findtext("issuer/issuerTradingSymbol") or "").strip().upper(),
    }

    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        owners.append({
            "name":        (ro.findtext("reportingOwnerId/rptOwnerName") or "").strip(),
            "cik":         (ro.findtext("reportingOwnerId/rptOwnerCik") or "").strip(),
            "is_director": (rel.findtext("isDirector") or "0").strip() in ("1", "true") if rel is not None else False,
            "is_officer":  (rel.findtext("isOfficer") or "0").strip() in ("1", "true") if rel is not None else False,
            "is_ten_pct":  (rel.findtext("isTenPercentOwner") or "0").strip() in ("1", "true") if rel is not None else False,
            "title":       (rel.findtext("officerTitle") or "").strip() if rel is not None else "",
        })

    txs = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code     = _val(tx, "transactionCoding/transactionCode") or (tx.findtext("transactionCoding/transactionCode") or "").strip()
        acq_disp = _val(tx, "transactionAmounts/transactionAcquiredDisposedCode")
        shares   = _num(_val(tx, "transactionAmounts/transactionShares"))
        price    = _num(_val(tx, "transactionAmounts/transactionPricePerShare"))
        txs.append({
            "date":      _val(tx, "transactionDate"),
            "code":      code,
            "acquired":  acq_disp == "A",
            "shares":    shares,
            "price":     price,
            "value_usd": round(shares * price, 2),
            "security":  _val(tx, "securityTitle"),
        })

    return {"issuer": issuer, "owners": owners, "transactions": txs}


# ── Security-title filter ─────────────────────────────────────────────────────

# A Form 4's non-derivative table can report preferred stock, warrants, units,
# notes or rights alongside ordinary shares. Only the security the signal is
# actually about counts; anything else is a different instrument at a different
# price (Bank of America's "Preferred Stock, Series DD" was scored as a common
# -stock insider buy before this filter existed).
_NON_COMMON_MARKERS = (
    "PREFERRED", "WARRANT", "NOTE", "DEBENTURE", "UNIT", "RIGHT", "BOND",
    "CONVERTIBLE", "TRUST PREF", "DEPOSITARY SHARE", "SUBORDINATED",
)
_COMMON_MARKERS = ("COMMON", "ORDINARY", "ADS", "AMERICAN DEPOSITARY", "SHARES OF BENEFICIAL")


def is_common_stock(security_title: str) -> bool:
    """True when the reported security is the ordinary tradable share class."""
    t = (security_title or "").upper()
    if not t:
        return True                      # untitled rows: assume the common line
    if any(m in t for m in _NON_COMMON_MARKERS):
        return False
    return any(m in t for m in _COMMON_MARKERS) or "STOCK" in t


# ── Aggregation & scoring ─────────────────────────────────────────────────────

def summarize_form4s(parsed_filings: list[dict], since: str) -> dict:
    """
    parsed_filings: [{filing_date, accession, owners, transactions}]
    Aggregates open-market buys (code P, acquired) and sells (code S, disposed)
    with transaction dates strictly after `since`.
    """
    buys, sells = [], []
    skipped_securities: dict[str, int] = {}
    for f in parsed_filings:
        owner_names = [o["name"] for o in f["owners"]] or ["(unknown)"]
        officer = any(o["is_officer"] for o in f["owners"])
        director = any(o["is_director"] for o in f["owners"])
        ten_pct = any(o["is_ten_pct"] for o in f["owners"])
        title = next((o["title"] for o in f["owners"] if o["title"]), "")
        for tx in f["transactions"]:
            if tx["date"] and tx["date"] <= since:
                continue
            if not is_common_stock(tx.get("security", "")):
                key = tx.get("security") or "(untitled)"
                skipped_securities[key] = skipped_securities.get(key, 0) + 1
                continue
            row = {
                "insider":     "; ".join(owner_names),
                "role":        title or ("Director" if director else "Officer" if officer else "10% owner" if ten_pct else "Insider"),
                "is_officer":  officer,
                "is_director": director,
                "date":        tx["date"] or f["filing_date"],
                "filing_date": f["filing_date"],
                "accession":   f["accession"],
                "shares":      tx["shares"],
                "price":       tx["price"],
                "value_usd":   tx["value_usd"],
                "security":    tx.get("security", ""),
            }
            if tx["code"] == "P" and tx["acquired"]:
                buys.append(row)
            elif tx["code"] == "S" and not tx["acquired"]:
                sells.append(row)

    buys.sort(key=lambda r: (r["date"], r["accession"], r["insider"]))
    sells.sort(key=lambda r: (r["date"], r["accession"], r["insider"]))

    sig_buys = [b for b in buys if b["value_usd"] >= INSIDER_MIN_PURCHASE_USD]
    buy_value  = round(sum(b["value_usd"] for b in buys), 2)
    sell_value = round(sum(s["value_usd"] for s in sells), 2)

    if not buys and not sells:
        stance = "NO_ACTIVITY"
    elif buy_value > sell_value:
        stance = "NET_BUYING"
    elif sell_value > buy_value:
        stance = "NET_SELLING"
    else:
        stance = "BALANCED"

    return {
        "net_stance":          stance,
        "skipped_securities":  dict(sorted(skipped_securities.items())),
        "buy_count":            len(buys),
        "significant_buy_count": len(sig_buys),
        "distinct_buyers":      sorted({b["insider"] for b in sig_buys}),
        "officer_or_director_buyers": sorted({b["insider"] for b in sig_buys if b["is_officer"] or b["is_director"]}),
        "buy_value_usd":        buy_value,
        "sell_count":           len(sells),
        "sell_value_usd":       sell_value,
        "net_value_usd":        round(buy_value - sell_value, 2),
        "buys":                 buys[-15:],
        "sells":                sells[-10:],
    }


def insider_score(summary: dict) -> tuple[float, list[str]]:
    """
    Pure, deterministic 0-100 transform of the Form 4 summary.

      55 pts  net open-market buy value  (linear to INSIDER_VALUE_FULL_USD)
      30 pts  distinct insiders buying   (linear to INSIDER_CLUSTER_FULL_COUNT)
      15 pts  officer/director involvement (any = full)
      net sellers: capped at 30 then −10 (buying is contradicted by larger sales)
    No Form 4 data at all -> 0 with an explicit "no insider buying" reason.
    """
    reasons: list[str] = []
    if not summary or (summary.get("buy_count", 0) == 0 and summary.get("sell_count", 0) == 0):
        return 0.0, ["No Form 4 open-market insider transactions since quarter-end"]

    net = summary.get("net_value_usd", 0.0)
    buy_val = summary.get("buy_value_usd", 0.0)
    n_buyers = len(summary.get("distinct_buyers", []))
    exec_involved = bool(summary.get("officer_or_director_buyers"))

    value_pts = 55.0 * min(1.0, max(0.0, buy_val) / INSIDER_VALUE_FULL_USD)
    cluster_pts = 30.0 * min(1.0, n_buyers / INSIDER_CLUSTER_FULL_COUNT)
    exec_pts = 15.0 if exec_involved else 0.0
    score = value_pts + cluster_pts + exec_pts

    if net < 0:
        # Net selling contradicts the 13F buy signal: cap the credit hard.
        score = min(score, 30.0) - 10.0
        reasons.append(f"Insiders were net sellers (${abs(net):,.0f} net sold) since quarter-end")
    if buy_val > 0:
        reasons.append(f"${buy_val:,.0f} of open-market insider purchases since quarter-end")
    if n_buyers:
        reasons.append(f"{n_buyers} distinct insider{'s' if n_buyers != 1 else ''} bought"
                       + (" (officer/director involved)" if exec_involved else ""))
    if buy_val == 0 and summary.get("sell_count", 0) > 0:
        reasons.append("Only insider sales on record since quarter-end (no purchases)")

    return round(max(0.0, min(100.0, score)), 1), reasons


# ── Orchestration with cache ──────────────────────────────────────────────────

def fetch_insider_activity(ticker: str, since: str, until: str | None = None) -> dict:
    """
    Full look-up for one ticker. `since` = last 13F quarter-end (report_date).
    Cached per (ticker, since, until) on disk.
    """
    until = until or date.today().isoformat()
    safe_t = re.sub(r"[^A-Z0-9]", "_", ticker.upper())
    # v2: issuer verification + common-stock-only filter. The v1 snapshots hold
    # other issuers' transactions, so they must not be reused.
    cache_path = INSIDER_CACHE_DIR / f"{safe_t}_{since}_{until}_v2.json"
    if cache_path.exists():
        cached = json.load(open(cache_path))
        # Always re-derive the score from the cached raw summary so a change to
        # the transform applies uniformly to every ticker in the run.
        if cached.get("summary") and not cached.get("error"):
            cached["score"], cached["reasons"] = insider_score(cached["summary"])
        return cached

    result = {
        "ticker": ticker, "since": since, "until": until,
        "cik": None, "form4_count": 0, "summary": {}, "score": 0.0,
        "reasons": ["No Form 4 open-market insider transactions since quarter-end"],
        "error": None,
    }

    cik = ticker_to_cik(ticker)
    if not cik:
        result["error"] = "ticker_not_in_sec_map"
        result["reasons"] = ["Insider data unavailable: ticker not found in SEC issuer map"]
        _save(cache_path, result)
        return result
    result["cik"] = cik

    filings = list_form4_filings(cik, since, until)
    parsed, foreign = [], {}
    for f in filings:
        xml_text = fetch_form4_xml(cik, f)
        if not xml_text:
            continue
        p = parse_form4(xml_text)
        # An issuer's EDGAR feed also carries Form 4s the COMPANY ITSELF filed as
        # an insider (10% owner) of a DIFFERENT issuer. Uber's sale of Aurora
        # Innovation shares and Berkshire's sale of DaVita shares were being
        # scored as insider selling in UBER and BRK/B. Keep only filings whose
        # <issuer> is the company we are actually scoring.
        issuer_cik = (p.get("issuer") or {}).get("cik", "")
        if issuer_cik and issuer_cik != cik:
            label = (p["issuer"].get("symbol") or p["issuer"].get("name") or issuer_cik)
            foreign[label] = foreign.get(label, 0) + 1
            continue
        parsed.append({**f, **p})

    result["form4_count"]        = len(parsed)
    result["foreign_issuer_skipped"] = dict(sorted(foreign.items()))
    summary = summarize_form4s(parsed, since)
    score, reasons = insider_score(summary)
    result.update({"summary": summary, "score": score, "reasons": reasons})
    _save(cache_path, result)
    return result


def _save(path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    tmp.replace(path)


def fetch_for_candidates(tickers: list[str], since_by_ticker: dict[str, str]) -> dict[str, dict]:
    """Batch helper used by signal_engine.py."""
    out: dict[str, dict] = {}
    for t in tickers:
        since = since_by_ticker.get(t) or since_by_ticker.get("__default__", "")
        if not since:
            continue
        print(f"  🔎 Form 4 look-up {t} (since {since})...", end=" ", flush=True)
        r = fetch_insider_activity(t, since)
        s = r.get("summary", {})
        print(f"{r['form4_count']} filings, buys=${s.get('buy_value_usd', 0):,.0f}, "
              f"sells=${s.get('sell_value_usd', 0):,.0f}, score={r['score']}")
        out[t] = r
    return out
