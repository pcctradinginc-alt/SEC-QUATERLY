"""
scoring.py
Conviction Score calculation engine.

Improvements:
  - Multi-quarter position building bonus (multi_quarter.py)
  - Filer quality tier multiplier (FILER_QUALITY in config)
  - Price-action staleness check via yfinance:
      +15% since filing → WARNING flag
      +25% since filing → score halved + STALE flag
  - Score normalization min-max [0, 100]
  - Cluster detection on ticker > CUSIP > normalized name
"""

import json
import re
from collections import defaultdict
from datetime import date, timedelta

from config import (
    CLUSTER_BONUS_MULTIPLIER, CLUSTER_MIN_FUNDS,
    DATA_DIR, DOUBLE_DOWN_MIN_DELTA, FILER_QUALITY,
    HIGH_CONVICTION_MIN_PCT, HIGH_CONVICTION_TOP_N,
    MIN_PORTFOLIO_WEIGHT_PCT, PRICE_ACTION_DOWNGRADE_PCT,
    PRICE_ACTION_WARN_PCT, WEIGHT_DELTA_PCT, WEIGHT_PORTFOLIO_PCT,
)
import multi_quarter


def load_parsed(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_holdings_parsed.json"
    if not path.exists():
        raise FileNotFoundError(f"Parsed holdings not found: {path}")
    with open(path) as f:
        return json.load(f)


# ── Price-action staleness ────────────────────────────────────────────────────

def fetch_price_changes(tickers: list[str], filing_dates: dict[str, str]) -> dict[str, dict]:
    """
    For each ticker, compares the price at its filing date to today's price.

    Returns {ticker: {pct_change, filing_close, current_price, days_since_filing}}
    or {ticker: {"pct_change": None, ...}} on failure.

    Uses a single yfinance batch download for efficiency.
    """
    empty: dict = {"pct_change": None, "filing_close": None,
                   "current_price": None, "days_since_filing": None}

    try:
        import yfinance as yf
    except ImportError:
        return {t: empty.copy() for t in tickers}

    if not tickers:
        return {}

    dates = [d for d in filing_dates.values() if d]
    if not dates:
        return {t: empty.copy() for t in tickers}

    # Download from the oldest filing date so every ticker has data from its filing onward
    oldest    = min(dates)
    today_str = date.today().isoformat()

    # yfinance uses BRK-B format, not BRK/B (Tradier format)
    yf_tickers = [t.replace("/", "-") for t in tickers]
    ticker_map  = {yf: orig for yf, orig in zip(yf_tickers, tickers)}

    try:
        hist = yf.download(
            yf_tickers,
            start=oldest,
            end=today_str,
            auto_adjust=True,
            progress=False,
        )
    except Exception as e:
        print(f"  ⚠️  yfinance batch download failed: {e}")
        return {t: empty.copy() for t in tickers}

    close = hist.get("Close", hist) if hasattr(hist, "get") else hist

    result: dict[str, dict] = {}

    for ticker in tickers:
        filing_date_str = filing_dates.get(ticker)
        if not filing_date_str:
            result[ticker] = empty.copy()
            continue

        yf_t = ticker.replace("/", "-")

        try:
            series = close if len(yf_tickers) == 1 else (
                close[yf_t] if yf_t in close.columns else None
            )

            if series is None or series.empty:
                result[ticker] = empty.copy()
                continue

            filing_date         = date.fromisoformat(filing_date_str)
            series_after_filing = series[series.index.date >= filing_date]

            if series_after_filing.empty:
                result[ticker] = empty.copy()
                continue

            filing_close  = round(float(series_after_filing.iloc[0]), 2)
            current_price = round(float(series_after_filing.iloc[-1]), 2)
            days_since    = (date.today() - filing_date).days

            if filing_close <= 0:
                result[ticker] = empty.copy()
            else:
                pct = ((current_price - filing_close) / filing_close) * 100.0
                result[ticker] = {
                    "pct_change":         round(pct, 1),
                    "filing_close":       filing_close,
                    "current_price":      current_price,
                    "days_since_filing":  days_since,
                }

        except Exception:
            result[ticker] = empty.copy()

    return result


# ── Core scoring ──────────────────────────────────────────────────────────────

def compute_raw_score(pos: dict, filer_name: str) -> float:
    """
    Score = (weight_port × portfolio_pct) + (weight_delta × normalized_delta)
    × filer_quality_multiplier

    NEW positions: delta component uses 100 as proxy.
    REDUCED / SOLD / UNCHANGED: score = 0.
    """
    port_pct   = pos["port_weight_pct"]
    delta_info = pos["delta"]
    delta_pct  = delta_info.get("delta_pct")
    tx_type    = delta_info.get("type", "UNCHANGED")

    if port_pct < MIN_PORTFOLIO_WEIGHT_PCT:
        return 0.0

    if tx_type in ("REDUCED", "SOLD", "UNCHANGED"):
        return 0.0

    # Skip positions where the fund is net short/hedged via puts.
    # A filer with 100 long shares and 10,000 put shares is bearish,
    # not a conviction buy – scoring it bullish would be a false signal.
    if not pos.get("net_bullish", True):
        return 0.0

    delta_component = 100.0 if delta_pct is None else abs(delta_pct)

    raw = (WEIGHT_PORTFOLIO_PCT * port_pct) + (WEIGHT_DELTA_PCT * delta_component)

    # Filer quality multiplier
    quality = FILER_QUALITY.get(filer_name, 1.0)
    raw *= quality

    return round(raw, 4)


def apply_flags(pos: dict, rank: int) -> list[str]:
    flags    = []
    tx_type  = pos["delta"].get("type")
    port_pct = pos["port_weight_pct"]
    delta_pct = pos["delta"].get("delta_pct")

    if tx_type == "NEW":
        flags.append("NEW_POSITION")
        if port_pct >= HIGH_CONVICTION_MIN_PCT:
            flags.append("HIGH_CONVICTION")
        if rank <= HIGH_CONVICTION_TOP_N:
            flags.append("TOP10_ENTRY")

    if tx_type == "ADDED" and delta_pct is not None and delta_pct >= DOUBLE_DOWN_MIN_DELTA:
        flags.append("AGGRESSIVE_ADD")

    # Warn if the long position is accompanied by a significant put hedge
    put_val  = pos.get("put_value_usd_k", 0)
    long_val = pos.get("value_usd_thousands", 0)
    if put_val > 0 and long_val > 0 and put_val > long_val * 0.5:
        flags.append("PUT_HEDGE_PRESENT")

    return flags


def build_scored_universe(parsed: dict, mq_signals: dict[str, dict]) -> list[dict]:
    """
    Iterates all filers and positions, computing a score for each.
    Applies filer quality and multi-quarter multipliers.
    Returns flat list of scored buy-only positions.
    """
    scored = []

    for filer_name, filer_data in parsed["filers"].items():
        if "positions" not in filer_data:
            continue

        positions = filer_data["positions"]
        filing_date = filer_data.get("filing_date", "")

        for pos in positions:
            raw_score = compute_raw_score(pos, filer_name)
            if raw_score <= 0:
                continue

            flags  = apply_flags(pos, pos.get("rank", 999))
            ticker = pos.get("ticker", "") or pos.get("cusip", "")

            # Multi-quarter bonus
            mq_mult = multi_quarter.get_multiplier(ticker, mq_signals)
            raw_score *= mq_mult
            if mq_mult > 1.0:
                mq_sig = mq_signals.get(ticker, {})
                flags.extend([f for f in mq_sig.get("flags", []) if f not in flags])

            scored.append({
                "filer":           filer_name,
                "ticker":          ticker,
                "cusip":           pos.get("cusip", ""),
                "name":            pos.get("name", ""),
                "port_weight_pct": pos["port_weight_pct"],
                "delta_pct":       pos["delta"].get("delta_pct"),
                "delta_type":      pos["delta"]["type"],
                "delta_shares":    pos["delta"]["delta_shares"],
                "value_usd_k":     pos["value_usd_k"],
                "rank_in_port":    pos.get("rank"),
                "filing_date":     filing_date,
                "raw_score":       round(raw_score, 4),
                "mq_multiplier":   round(mq_mult, 2),
                "flags":           flags,
            })

    return scored


# ── Price-action staleness enrichment ─────────────────────────────────────────

def enrich_with_price_action(scored: list[dict]) -> list[dict]:
    """
    Checks current price vs price at filing date for each ticker.
    If the stock has already run >25% since the 13F filing, the
    thesis may have played out – score is halved and STALE flag added.
    """
    # Collect unique ticker → filing_date mapping (use earliest filing date per ticker)
    filing_dates: dict[str, str] = {}
    for entry in scored:
        t = entry["ticker"]
        if t and t not in filing_dates:
            filing_dates[t] = entry.get("filing_date", "")

    tickers = [t for t in filing_dates if t]
    if not tickers:
        return scored

    print(f"  📈 Checking price action for {len(tickers)} tickers since filing date...")
    changes = fetch_price_changes(tickers, filing_dates)

    warned = staled = 0
    for entry in scored:
        t    = entry["ticker"]
        perf = changes.get(t, {})
        pct  = perf.get("pct_change")

        # Store full performance snapshot on each scored entry
        entry["post_filing_perf"] = perf

        if pct is None:
            continue

        if pct >= PRICE_ACTION_DOWNGRADE_PCT:
            entry["raw_score"] *= 0.5
            if "PRICE_ACTION_STALE" not in entry["flags"]:
                entry["flags"].append("PRICE_ACTION_STALE")
            staled += 1
        elif pct >= PRICE_ACTION_WARN_PCT:
            if "PRICE_ACTION_WARNING" not in entry["flags"]:
                entry["flags"].append("PRICE_ACTION_WARNING")
            warned += 1

    if staled or warned:
        print(f"  ⚠️  Price action: {staled} positions stale (>{PRICE_ACTION_DOWNGRADE_PCT}%), "
              f"{warned} warnings (>{PRICE_ACTION_WARN_PCT}%)")
    return scored


# ── Cluster detection ─────────────────────────────────────────────────────────

def detect_clusters(scored: list[dict]) -> dict[str, list[str]]:
    """Clusters on ticker > CUSIP > normalized company name."""
    key_filers: dict[str, list[str]] = defaultdict(list)

    for entry in scored:
        if entry["delta_type"] not in ("NEW", "ADDED"):
            continue

        ticker    = entry.get("ticker", "").strip()
        cusip     = entry.get("cusip", "").strip()
        name      = entry.get("name", "").strip().upper()
        name_norm = re.sub(r"\b(INC|CORP|LTD|LLC|LP|PLC|CO|THE|DEL|COM)\b", "", name)
        name_norm = re.sub(r"\s+", " ", name_norm).strip()

        key = ticker or cusip or name_norm
        if key:
            key_filers[key].append(entry["filer"])

    return {
        key: filers
        for key, filers in key_filers.items()
        if len(filers) >= CLUSTER_MIN_FUNDS
    }


def apply_cluster_bonus(scored: list[dict], clusters: dict[str, list[str]]) -> list[dict]:
    for entry in scored:
        cluster_key = entry["ticker"] or entry.get("cusip", "")
        if cluster_key in clusters:
            entry["cluster_funds"] = clusters[cluster_key]
            entry["cluster_count"] = len(clusters[cluster_key])
            entry["raw_score"]    *= CLUSTER_BONUS_MULTIPLIER
            if "CLUSTER" not in entry["flags"]:
                entry["flags"].append("CLUSTER")
        else:
            entry["cluster_funds"] = []
            entry["cluster_count"] = 0
    return scored


def normalize_scores(scored: list[dict]) -> list[dict]:
    """Min-max normalize raw_score to [0, 100]."""
    if not scored:
        return scored

    scores = [e["raw_score"] for e in scored]
    min_s, max_s = min(scores), max(scores)

    if max_s == min_s:
        for e in scored:
            e["conviction_score"] = 50.0
    else:
        for e in scored:
            e["conviction_score"] = round(
                (e["raw_score"] - min_s) / (max_s - min_s) * 100, 1
            )

    return scored


def aggregate_by_ticker(scored: list[dict]) -> list[dict]:
    """Merges per-filer entries into per-ticker aggregates."""
    by_ticker: dict[str, dict] = {}

    for entry in scored:
        ticker = entry["ticker"] or entry["cusip"] or entry["name"]
        if ticker not in by_ticker:
            by_ticker[ticker] = {
                "ticker":            ticker,
                "name":              entry["name"],
                "filers":            [],
                "conviction_score":  0.0,
                "total_value_usd_k": 0,
                "flags":             set(),
                "cluster_count":     entry["cluster_count"],
                "cluster_funds":     entry["cluster_funds"],
                "delta_types":       [],
                # Full post-filing performance dict (pct_change, filing_close,
                # current_price, days_since_filing) – used in report and Claude prompt
                "post_filing_perf":  entry.get("post_filing_perf", {}),
            }

        agg = by_ticker[ticker]
        agg["filers"].append({
            "filer":           entry["filer"],
            "port_weight_pct": entry["port_weight_pct"],
            "delta_pct":       entry["delta_pct"],
            "delta_type":      entry["delta_type"],
            "conviction_score":entry["conviction_score"],
            "mq_multiplier":   entry.get("mq_multiplier", 1.0),
        })
        agg["conviction_score"] = max(agg["conviction_score"], entry["conviction_score"])
        agg["total_value_usd_k"] += entry["value_usd_k"]
        agg["flags"].update(entry["flags"])
        agg["delta_types"].append(entry["delta_type"])

    result = []
    for ticker, agg in by_ticker.items():
        agg["flags"]       = sorted(agg["flags"])
        agg["filer_count"] = len(agg["filers"])
        result.append(agg)

    result.sort(key=lambda x: x["conviction_score"], reverse=True)
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Conviction Scoring Engine – {today_str}")
    print(f"{'='*60}")

    parsed = load_parsed(today_str)

    # 1. Multi-quarter signals from historical data
    print("\n🔍 Analyzing multi-quarter position building...")
    mq_signals = multi_quarter.build_multi_quarter_signals(today_str)
    print(f"   {len(mq_signals)} tickers with 2+ build quarters")
    for t, s in list(mq_signals.items())[:5]:
        print(f"   {t}: {s['build_quarters']}Q build, flags={s['flags']}")

    # 2. Raw scores per filer/position (includes filer quality + MQ multiplier)
    scored = build_scored_universe(parsed, mq_signals)
    print(f"\n📊 Scored positions (buys only, ≥{MIN_PORTFOLIO_WEIGHT_PCT}% port weight): {len(scored)}")

    # 3. Price-action staleness check
    scored = enrich_with_price_action(scored)

    # 4. Cluster detection
    clusters = detect_clusters(scored)
    print(f"🔗 Cluster signals (≥{CLUSTER_MIN_FUNDS} funds): {len(clusters)} tickers")
    for t, f in clusters.items():
        print(f"   {t}: {f}")

    # 5. Apply cluster bonus
    scored = apply_cluster_bonus(scored, clusters)

    # 6. Normalize to [0, 100]
    scored = normalize_scores(scored)

    # 7. Aggregate by ticker
    aggregated = aggregate_by_ticker(scored)

    print(f"\n{'─'*60}")
    print(f"{'Rank':<5}{'Ticker':<8}{'Score':<8}{'Filers':<8}{'Flags'}")
    print(f"{'─'*60}")
    for i, agg in enumerate(aggregated[:20], 1):
        print(f"{i:<5}{agg['ticker']:<8}{agg['conviction_score']:<8.1f}"
              f"{agg['filer_count']:<8}{', '.join(agg['flags'])}")

    output = {
        "date":        today_str,
        "scored_flat": scored,
        "aggregated":  aggregated,
        "clusters":    clusters,
        "top20":       aggregated[:20],
        "mq_signals":  mq_signals,
    }

    output_path = DATA_DIR / f"{today_str}_scores.json"
    tmp_path    = output_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    tmp_path.replace(output_path)

    print(f"\n✅ Scores saved to {output_path}")


if __name__ == "__main__":
    run()
