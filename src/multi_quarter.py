"""
multi_quarter.py
Analyzes multi-quarter position building patterns.

A "Silent Build" over 3–4 quarters is a much stronger signal than
a single large buy in one quarter, which can be momentum-chasing or
window-dressing.
"""

import json
from datetime import date
from pathlib import Path

from config import DATA_DIR, MULTI_QUARTER_BUILD_MIN, MULTI_QUARTER_BONUS, MULTI_QUARTER_MAX


def load_historical_parsed(today_str: str) -> list[dict]:
    """
    Loads up to MULTI_QUARTER_MAX previous *_holdings_parsed.json files,
    most recent first, excluding today.
    """
    today = date.fromisoformat(today_str)
    candidates = sorted(DATA_DIR.glob("*_holdings_parsed.json"), reverse=True)

    results = []
    for c in candidates:
        try:
            d = date.fromisoformat(c.name[:10])
            if d < today:
                with open(c) as f:
                    results.append(json.load(f))
                if len(results) >= MULTI_QUARTER_MAX:
                    break
        except (ValueError, json.JSONDecodeError):
            continue

    return results  # most recent first


def build_multi_quarter_signals(today_str: str) -> dict[str, dict]:
    """
    For every ticker seen across historical quarters, computes:
      - How many quarters had net buying activity
      - Average delta pct per building quarter
      - Slope of portfolio weight (positive = growing conviction)
      - Silent build flag (small but consistent adds)

    Returns {ticker: signal_dict} only for tickers with 2+ build quarters.
    """
    history = load_historical_parsed(today_str)

    if len(history) < 2:
        print(f"  ⚠️  Multi-quarter: only {len(history)} historical quarter(s) available. Signals will be sparse.")
        if not history:
            return {}

    # Aggregate per ticker, per quarter
    ticker_quarters: dict[str, list[dict]] = {}

    for quarter_data in history:
        quarter_str = quarter_data.get("date", "")
        for filer_name, filer_data in quarter_data.get("filers", {}).items():
            for pos in filer_data.get("positions", []):
                ticker = pos.get("ticker") or pos.get("cusip", "")
                if not ticker:
                    continue

                if ticker not in ticker_quarters:
                    ticker_quarters[ticker] = []

                ticker_quarters[ticker].append({
                    "quarter":     quarter_str,
                    "filer":       filer_name,
                    "delta_type":  pos["delta"]["type"],
                    "delta_pct":   pos["delta"].get("delta_pct"),
                    "port_weight": pos["port_weight_pct"],
                })

    signals = {}

    for ticker, entries in ticker_quarters.items():
        quarters_seen = sorted(set(e["quarter"] for e in entries), reverse=True)

        build_quarters = 0
        delta_pcts: list[float] = []
        weights_by_quarter: list[float] = []

        for q in quarters_seen:
            q_entries = [e for e in entries if e["quarter"] == q]
            has_buy    = any(e["delta_type"] in ("NEW", "ADDED") for e in q_entries)
            has_reduce = any(e["delta_type"] in ("REDUCED", "SOLD") for e in q_entries)

            if has_buy and not has_reduce:
                build_quarters += 1
                for e in q_entries:
                    if e["delta_pct"] is not None:
                        delta_pcts.append(abs(e["delta_pct"]))

            avg_w = sum(e["port_weight"] for e in q_entries) / len(q_entries)
            weights_by_quarter.append(avg_w)

        if build_quarters < 2:
            continue

        avg_delta = sum(delta_pcts) / len(delta_pcts) if delta_pcts else 0.0

        # Weight slope: positive means portfolio weight growing over time
        # weights_by_quarter[0] = most recent, [-1] = oldest
        slope = 0.0
        if len(weights_by_quarter) >= 2:
            slope = (weights_by_quarter[0] - weights_by_quarter[-1]) / len(weights_by_quarter)

        # Silent Build: 3+ quarters of small, consistent adds (5–25% delta)
        silent_build = (
            build_quarters >= 3
            and len(delta_pcts) >= 3
            and all(5.0 <= d <= 25.0 for d in delta_pcts[-3:])
        )

        # Build score: more quarters + higher avg delta + positive slope
        build_score = (build_quarters / 4.0) * max(avg_delta, 1.0) * (1.0 + max(slope, 0.0))

        flags = []
        if build_quarters >= MULTI_QUARTER_BUILD_MIN:
            flags.append("MULTI_QUARTER_BUILD")
        if build_quarters >= 5:
            flags.append("STRONG_BUILD")
        if silent_build:
            flags.append("SILENT_ACCUMULATION")

        signals[ticker] = {
            "build_quarters":  build_quarters,
            "total_quarters":  len(quarters_seen),
            "avg_delta_pct":   round(avg_delta, 1),
            "weight_slope":    round(slope, 3),
            "build_score":     round(build_score, 2),
            "silent_build":    silent_build,
            "flags":           flags,
        }

    return signals


def get_multiplier(ticker: str, signals: dict[str, dict]) -> float:
    """Returns a score multiplier based on multi-quarter conviction signal."""
    if ticker not in signals:
        return 1.0
    bq = signals[ticker]["build_quarters"]
    if bq >= 5:
        return MULTI_QUARTER_BONUS * 1.3
    if bq >= 3:
        return MULTI_QUARTER_BONUS
    if bq >= 2:
        return 1.2
    return 1.0
