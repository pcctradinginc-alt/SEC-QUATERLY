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
    INSIDER_CACHE_DIR, INSIDER_CLUSTER_FULL_COUNT, INSIDER_CLUSTER_WINDOW_DAYS,
    INSIDER_DISCRETIONARY_SELL_PENALTY, INSIDER_MAX_FORM4_PER_TICKER,
    INSIDER_MIN_PURCHASE_USD, INSIDER_ROLE_POINTS, INSIDER_STAKE_FULL_PCT,
    INSIDER_VALUE_FULL_USD, SEC_HEADERS, SEC_RATE_LIMIT_SLEEP,
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
    # Document-level Rule 10b5-1 flag: the trade was scheduled under a plan
    # adopted long before, so it carries no information about today's view.
    planned = (root.findtext("aff10b5One") or "").strip() in ("1", "true")

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
            "planned":   planned,
            "shares_after": _num(_val(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
        })

    return {"issuer": issuer, "owners": owners, "transactions": txs, "planned": planned}


# ── Insider roles ─────────────────────────────────────────────────────────────

def classify_role(owners: list[dict]) -> tuple[str, str]:
    """
    Highest-signal role among a filing's reporting owners, as (key, label).
    The CEO and CFO sit closest to the numbers, so their trades weigh most.
    """
    best_key, best_label = "OTHER", "Insider"
    rank = {"CEO": 5, "CFO": 4, "OFFICER": 3, "DIRECTOR": 2, "TEN_PCT": 1, "OTHER": 0}
    for o in owners:
        title = (o.get("title") or "").upper()
        if o.get("is_officer") and ("CHIEF EXECUTIVE" in title or "CEO" in title.split()
                                    or "PRESIDENT AND CEO" in title):
            key, label = "CEO", o.get("title") or "CEO"
        elif o.get("is_officer") and ("CHIEF FINANCIAL" in title or "CFO" in title.split()):
            key, label = "CFO", o.get("title") or "CFO"
        elif o.get("is_officer"):
            key, label = "OFFICER", o.get("title") or "Officer"
        elif o.get("is_director"):
            key, label = "DIRECTOR", "Director"
        elif o.get("is_ten_pct"):
            key, label = "TEN_PCT", "10% owner"
        else:
            key, label = "OTHER", "Insider"
        if rank[key] > rank[best_key]:
            best_key, best_label = key, label
    return best_key, best_label


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

def _transaction_key(owner_ciks: str, tx: dict) -> tuple:
    """
    Identity of a reported transaction, independent of which filing carried it.

    A Form 4/A restates an earlier Form 4 and repeats its transactions. Summing
    both would count one purchase twice - and a single insider buy can move a
    name by up to 25 points here, so a duplicate is not cosmetic.
    """
    return (owner_ciks, tx.get("date", ""), tx.get("code", ""),
            (tx.get("security") or "").upper(), round(tx.get("shares", 0.0), 4),
            round(tx.get("price", 0.0), 6))


def summarize_form4s(parsed_filings: list[dict], since: str) -> dict:
    """
    parsed_filings: [{filing_date, accession, owners, transactions, form}]
    Aggregates open-market buys (code P, acquired) and sells (code S, disposed)
    with transaction dates strictly after `since`.

    Amendments supersede: filings are walked newest first and a transaction
    already seen from a later filing is not counted again.
    """
    buys, sells = [], []
    seen_transactions: set = set()
    superseded = 0
    # Newest filing first, amendments ahead of the original they restate.
    parsed_filings = sorted(
        parsed_filings,
        key=lambda f: (f.get("filing_date", ""), f.get("form", "") == "4/A"),
        reverse=True,
    )
    skipped_securities: dict[str, int] = {}
    for f in parsed_filings:
        owner_names = [o["name"] for o in f["owners"]] or ["(unknown)"]
        officer = any(o["is_officer"] for o in f["owners"])
        director = any(o["is_director"] for o in f["owners"])
        ten_pct = any(o["is_ten_pct"] for o in f["owners"])
        role_key, role_label = classify_role(f["owners"])
        owner_ciks = "|".join(sorted(o.get("cik", "") for o in f["owners"]))
        for tx in f["transactions"]:
            if tx["date"] and tx["date"] <= since:
                continue
            key = _transaction_key(owner_ciks, tx)
            if key in seen_transactions:
                superseded += 1
                continue
            seen_transactions.add(key)
            if not is_common_stock(tx.get("security", "")):
                key = tx.get("security") or "(untitled)"
                skipped_securities[key] = skipped_securities.get(key, 0) + 1
                continue
            stake_after = tx.get("shares_after") or 0.0
            row = {
                "insider":     "; ".join(owner_names),
                "role":        role_label,
                "role_key":    role_key,
                "planned":     bool(tx.get("planned")),
                "shares_after": stake_after,
                "stake_change_pct": (round(tx["shares"] / (stake_after - tx["shares"]) * 100.0, 1)
                                     if stake_after > tx["shares"] > 0 else None),
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

    # Token-sized purchases are excluded by configuration, so they must not
    # reach the value component either: a hundred $10k trades would otherwise
    # score like a single $1M conviction buy.
    sig_buys = [b for b in buys if b["value_usd"] >= INSIDER_MIN_PURCHASE_USD]
    buy_value      = round(sum(b["value_usd"] for b in sig_buys), 2)   # drives the score
    gross_buy_value = round(sum(b["value_usd"] for b in buys), 2)      # reported, not scored
    sell_value = round(sum(s["value_usd"] for s in sells), 2)

    # Rule 10b5-1 sales were scheduled in advance; only discretionary sales say
    # anything about how insiders see the business today (spec section 3).
    planned_sells       = [x for x in sells if x.get("planned")]
    discretionary_sells = [x for x in sells if not x.get("planned")]
    planned_sell_value       = round(sum(x["value_usd"] for x in planned_sells), 2)
    discretionary_sell_value = round(sum(x["value_usd"] for x in discretionary_sells), 2)

    # Cluster buying: independent insiders buying within a short window.
    cluster_buying, cluster_window_buyers = False, []
    if len(sig_buys) >= 2:
        dated = sorted((b for b in sig_buys if b.get("date")), key=lambda b: b["date"])
        for i, anchor in enumerate(dated):
            try:
                a0 = date.fromisoformat(anchor["date"])
            except ValueError:
                continue
            window = {anchor["insider"]}
            for other in dated[i + 1:]:
                try:
                    if (date.fromisoformat(other["date"]) - a0).days <= INSIDER_CLUSTER_WINDOW_DAYS:
                        window.add(other["insider"])
                except ValueError:
                    continue
            if len(window) > len(cluster_window_buyers):
                cluster_window_buyers = sorted(window)
        cluster_buying = len(cluster_window_buyers) >= 2

    best_stake = max((b.get("stake_change_pct") or 0.0) for b in sig_buys) if sig_buys else 0.0
    roles = {b.get("role_key", "OTHER") for b in sig_buys}

    if not buys and not sells:
        stance = "NO_ACTIVITY"
    elif gross_buy_value > sell_value:
        stance = "NET_BUYING"
    elif sell_value > gross_buy_value:
        stance = "NET_SELLING"
    else:
        stance = "BALANCED"

    return {
        "net_stance":              stance,
        "gross_buy_value_usd":     gross_buy_value,
        "significant_buy_value_usd": buy_value,
        "superseded_transactions": superseded,
        "skipped_securities":      dict(sorted(skipped_securities.items())),
        "planned_sell_value_usd":       planned_sell_value,
        "discretionary_sell_value_usd": discretionary_sell_value,
        "planned_sell_count":           len(planned_sells),
        "cluster_buying":               cluster_buying,
        "cluster_buyers":               cluster_window_buyers,
        "buyer_roles":                  sorted(roles),
        "top_role":                     max(roles, key=lambda r: INSIDER_ROLE_POINTS.get(r, 0.0)) if roles else None,
        "max_stake_change_pct":         best_stake,
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

      40 pts  open-market buy value      (linear to INSIDER_VALUE_FULL_USD)
      25 pts  distinct insiders buying   (linear to INSIDER_CLUSTER_FULL_COUNT,
                                          full credit needs cluster buying)
      25 pts  seniority of the buyers    (CEO/CFO > other officers > directors)
      10 pts  size of the buy against the buyer's own existing stake
      −25 pts discretionary net selling  (Rule 10b5-1 sales are NOT counted:
                                          they were scheduled months earlier)
    """
    reasons: list[str] = []
    if not summary or (summary.get("buy_count", 0) == 0 and summary.get("sell_count", 0) == 0):
        return 0.0, ["No Form 4 open-market insider transactions since quarter-end"]

    buy_val   = summary.get("buy_value_usd", 0.0) or 0.0
    n_buyers  = len(summary.get("distinct_buyers", []))
    top_role  = summary.get("top_role")
    cluster   = bool(summary.get("cluster_buying"))
    stake_pct = summary.get("max_stake_change_pct", 0.0) or 0.0

    value_pts = 40.0 * min(1.0, max(0.0, buy_val) / INSIDER_VALUE_FULL_USD)
    cluster_pts = 25.0 * min(1.0, n_buyers / INSIDER_CLUSTER_FULL_COUNT)
    if n_buyers >= 2 and not cluster:
        cluster_pts *= 0.7          # spread over months, not a coordinated cluster
    role_pts  = INSIDER_ROLE_POINTS.get(top_role or "OTHER", 0.0) if buy_val > 0 else 0.0
    stake_pts = 10.0 * min(1.0, stake_pct / INSIDER_STAKE_FULL_PCT)
    score = value_pts + cluster_pts + role_pts + stake_pts

    discretionary = summary.get("discretionary_sell_value_usd", summary.get("sell_value_usd", 0.0)) or 0.0
    planned       = summary.get("planned_sell_value_usd", 0.0) or 0.0
    if discretionary > buy_val:
        excess = discretionary - buy_val
        penalty = INSIDER_DISCRETIONARY_SELL_PENALTY * min(1.0, excess / INSIDER_VALUE_FULL_USD)
        score -= penalty
        reasons.append(f"Discretionary insider selling of ${discretionary:,.0f} outweighs purchases")

    if buy_val > 0:
        reasons.append(f"${buy_val:,.0f} of open-market insider purchases since quarter-end")
    if n_buyers:
        role_txt = {"CEO": "including the CEO", "CFO": "including the CFO",
                    "OFFICER": "including an officer", "DIRECTOR": "by directors"}.get(top_role, "")
        reasons.append(f"{n_buyers} distinct insider{'s' if n_buyers != 1 else ''} bought {role_txt}".strip()
                       + (" in a coordinated cluster" if cluster else ""))
    if stake_pct >= 10:
        reasons.append(f"Largest buy lifted that insider's own stake by {stake_pct:.0f}%")
    if planned > 0:
        reasons.append(f"${planned:,.0f} of sales ran under Rule 10b5-1 plans and are not scored as a signal")
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
    # v3: adds 10b5-1 planned-sale detection, insider roles and stake changes.
    # v2 added issuer verification + the common-stock filter; v1 snapshots hold
    # other issuers' transactions outright. Older snapshots are never reused.
    cache_path = INSIDER_CACHE_DIR / f"{safe_t}_{since}_{until}_v4.json"
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
    result["amendments"]        = sum(1 for f in parsed if f.get("form") == "4/A")
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
