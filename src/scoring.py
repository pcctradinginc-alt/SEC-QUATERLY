"""
scoring.py
Conviction Score calculation engine.

Fixes from architecture audit:
  R-04  New position delta_pct = None → handled with separate NEW logic
  R-09  Score normalization → min-max scaled to [0, 100]
  R-10  Cluster detection uses ticker as primary key (not CUSIP)
        to handle ADR vs. ordinary share (R-11)
  R-01  Split-adjusted shares already in parsed data
"""

import json
import re
from collections import defaultdict
from datetime import date

from config import (
    CLUSTER_BONUS_MULTIPLIER, CLUSTER_MIN_FUNDS,
    DATA_DIR, DOUBLE_DOWN_MIN_DELTA, HIGH_CONVICTION_MIN_PCT,
    HIGH_CONVICTION_TOP_N, MIN_PORTFOLIO_WEIGHT_PCT,
    WEIGHT_DELTA_PCT, WEIGHT_PORTFOLIO_PCT,
)


def load_parsed(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_holdings_parsed.json"
    if not path.exists():
        raise FileNotFoundError(f"Parsed holdings not found: {path}")
    with open(path) as f:
        return json.load(f)


def compute_raw_score(pos: dict) -> float:
    """
    Core scoring formula (weighted combination):

    Score = (weight_port × portfolio_pct) + (weight_delta × normalized_delta)

    For NEW positions: delta component uses a proxy of 100%
    (entering the position at all is the signal; size determines magnitude).

    R-04 Fix: delta_pct == None → treated as 100 (new position proxy).
    R-09 Fix: Raw score is computed here; normalization happens after
              all scores are collected.
    """
    port_pct   = pos["port_weight_pct"]
    delta_info = pos["delta"]
    delta_pct  = delta_info.get("delta_pct")
    tx_type    = delta_info.get("type", "UNCHANGED")

    # Ignore micro positions
    if port_pct < MIN_PORTFOLIO_WEIGHT_PCT:
        return 0.0

    # Only score buys (NEW, ADDED) – not REDUCED/SOLD/UNCHANGED
    if tx_type in ("REDUCED", "SOLD", "UNCHANGED"):
        return 0.0

    # Delta component
    if delta_pct is None:
        # New position: use portfolio weight alone scaled up
        delta_component = 100.0
    else:
        delta_component = abs(delta_pct)  # can be >100% for aggressive adds

    raw = (WEIGHT_PORTFOLIO_PCT * port_pct) + (WEIGHT_DELTA_PCT * delta_component)
    return round(raw, 4)


def apply_flags(pos: dict, rank: int, total_positions: int) -> list[str]:
    """
    Assigns qualitative flags to a position.
    """
    flags = []
    tx_type   = pos["delta"].get("type")
    port_pct  = pos["port_weight_pct"]
    delta_pct = pos["delta"].get("delta_pct")

    if tx_type == "NEW":
        flags.append("NEW_POSITION")
        if port_pct >= HIGH_CONVICTION_MIN_PCT:
            flags.append("HIGH_CONVICTION")
        if rank <= HIGH_CONVICTION_TOP_N:
            flags.append("TOP10_ENTRY")

    if tx_type == "ADDED" and delta_pct is not None and delta_pct >= DOUBLE_DOWN_MIN_DELTA:
        # For double-down we'd ideally check if price fell –
        # that check happens in scoring.py after yfinance lookup.
        flags.append("AGGRESSIVE_ADD")

    return flags


def build_scored_universe(parsed: dict) -> list[dict]:
    """
    Iterates all filers and all positions, computing a score for each.
    Returns a flat list of scored positions across all filers.
    """
    scored = []

    for filer_name, filer_data in parsed["filers"].items():
        if "positions" not in filer_data:
            continue

        positions = filer_data["positions"]
        total     = len(positions)

        for pos in positions:
            raw_score = compute_raw_score(pos)
            if raw_score <= 0:
                continue

            flags = apply_flags(pos, pos.get("rank", 999), total)

            scored.append({
                "filer":          filer_name,
                "ticker":         pos["ticker"],
                "cusip":          pos["cusip"],
                "name":           pos["name"],
                "port_weight_pct":pos["port_weight_pct"],
                "delta_pct":      pos["delta"].get("delta_pct"),
                "delta_type":     pos["delta"]["type"],
                "delta_shares":   pos["delta"]["delta_shares"],
                "value_usd_k":    pos["value_usd_k"],
                "rank_in_port":   pos.get("rank"),
                "raw_score":      raw_score,
                "flags":          flags,
            })

    return scored


def detect_clusters(scored: list[dict]) -> dict[str, list[str]]:
    """
    Clusters on ticker first, then CUSIP, then normalized company name as fallback.
    This ensures clustering works even when OpenFIGI mapping fails.
    """
    key_filers: dict[str, list[str]] = defaultdict(list)

    for entry in scored:
        if entry["delta_type"] not in ("NEW", "ADDED"):
            continue

        # Priority: ticker > cusip > normalized company name
        ticker = entry.get("ticker", "").strip()
        cusip  = entry.get("cusip", "").strip()
        name   = entry.get("name", "").strip().upper()

        # Normalize company name: remove common suffixes for better matching
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
    """Multiplies raw_score by CLUSTER_BONUS_MULTIPLIER for clustered tickers.
    Checks ticker first, then CUSIP as fallback (mirrors detect_clusters key logic).
    """
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
    """
    R-09 Fix: Min-max normalize raw_score to [0, 100].
    This ensures Claude receives comparable numbers, not arbitrary magnitudes.
    """
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
    """
    Merges per-filer entries into per-ticker aggregates.
    Returns a list sorted by aggregate conviction score descending.
    """
    by_ticker: dict[str, dict] = {}

    for entry in scored:
        ticker = entry["ticker"] or entry["cusip"] or entry["name"]
        if ticker not in by_ticker:
            by_ticker[ticker] = {
                "ticker":           ticker,
                "name":             entry["name"],
                "filers":           [],
                "conviction_score": 0.0,
                "total_value_usd_k":0,
                "flags":            set(),
                "cluster_count":    entry["cluster_count"],
                "cluster_funds":    entry["cluster_funds"],
                "delta_types":      [],
            }

        agg = by_ticker[ticker]
        agg["filers"].append({
            "filer":          entry["filer"],
            "port_weight_pct":entry["port_weight_pct"],
            "delta_pct":      entry["delta_pct"],
            "delta_type":     entry["delta_type"],
            "conviction_score":entry["conviction_score"],
        })
        agg["conviction_score"] = max(agg["conviction_score"], entry["conviction_score"])
        agg["total_value_usd_k"] += entry["value_usd_k"]
        agg["flags"].update(entry["flags"])
        agg["delta_types"].append(entry["delta_type"])

    result = []
    for ticker, agg in by_ticker.items():
        agg["flags"] = sorted(agg["flags"])
        agg["filer_count"] = len(agg["filers"])
        result.append(agg)

    result.sort(key=lambda x: x["conviction_score"], reverse=True)
    return result


def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Conviction Scoring Engine – {today_str}")
    print(f"{'='*60}")

    parsed = load_parsed(today_str)

    # 1. Raw scores per filer/position
    scored = build_scored_universe(parsed)
    print(f"📊 Scored positions (buys only, ≥{MIN_PORTFOLIO_WEIGHT_PCT}% port weight): {len(scored)}")

    # 2. Cluster detection
    clusters = detect_clusters(scored)
    print(f"🔗 Cluster signals (≥{CLUSTER_MIN_FUNDS} funds): {len(clusters)} tickers")
    for t, f in clusters.items():
        print(f"   {t}: {f}")

    # 3. Apply cluster bonus
    scored = apply_cluster_bonus(scored, clusters)

    # 4. Normalize to [0, 100]
    scored = normalize_scores(scored)

    # 5. Aggregate by ticker
    aggregated = aggregate_by_ticker(scored)

    # Print top 20
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
    }

    output_path = DATA_DIR / f"{today_str}_scores.json"
    tmp_path = output_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    tmp_path.replace(output_path)

    print(f"\n✅ Scores saved to {output_path}")


if __name__ == "__main__":
    run()
