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

from config import DATA_DIR, MULTI_QUARTER_BUILD_MIN, MULTI_QUARTER_MAX


def _had_real_comparison(parsed: dict, sample: int = 400) -> bool:
    """True when this dataset was itself diffed against its own prior quarter."""
    if parsed.get("has_prior_baseline") is not None:
        return bool(parsed["has_prior_baseline"])
    seen = non_new = 0                       # older files carry no flag: infer it
    for f in parsed.get("filers", {}).values():
        if f.get("exited_positions"):
            return True
        for pos in f.get("positions", []):
            seen += 1
            if pos["delta"]["type"] != "NEW":
                non_new += 1
            if seen >= sample:
                break
        if seen >= sample:
            break
    return seen > 0 and non_new > seen * 0.05


def load_historical_parsed(today_str: str) -> list[dict]:
    """
    Up to MULTI_QUARTER_MAX previous quarters, newest quarter first.

    Selection is per REPORTING QUARTER, not per file. Running the pipeline
    twice writes two files for the same quarter, and counting both would report
    two quarters of accumulation where only one exists. Two kinds of dataset are
    skipped outright: those whose share counts are all zero, and those that were
    never diffed against a prior quarter of their own - the latter mark every
    holding NEW, which would fabricate accumulation across the whole universe.
    """
    from parse_13f import has_usable_share_counts, infer_report_date

    today = date.fromisoformat(today_str)
    by_quarter: dict[str, tuple[str, dict]] = {}

    for c in sorted(DATA_DIR.glob("*_holdings_parsed.json"), reverse=True):
        try:
            d = date.fromisoformat(c.name[:10])
        except ValueError:
            continue
        if d >= today:
            continue
        try:
            with open(c) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not has_usable_share_counts(data):
            continue
        # A quarter that had no baseline of its own labels everything NEW.
        # Counting it as a build quarter would manufacture accumulation for the
        # entire universe, so such datasets are loaded for nothing here.
        if not _had_real_comparison(data):
            continue
        quarter = infer_report_date(data) or c.name[:10]
        # Newest file wins for a given quarter (a re-run supersedes the earlier one)
        if quarter not in by_quarter or c.name[:10] > by_quarter[quarter][0]:
            by_quarter[quarter] = (c.name[:10], data)

    ordered = [data for _, (_, data) in sorted(by_quarter.items(), reverse=True)]
    return ordered[:MULTI_QUARTER_MAX]


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
        # The reporting quarter, not the run date: a Q2-2026 build must not be
        # labelled "2026-09-06" internally.
        from parse_13f import infer_report_date
        quarter_str = infer_report_date(quarter_data) or quarter_data.get("date", "")
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
        # (legacy composite metric, kept for logging/debugging only - the
        # Alpha Score's accumulation component uses accumulation_score below)
        build_score = (build_quarters / 4.0) * max(avg_delta, 1.0) * (1.0 + max(slope, 0.0))

        # Section 6: Accumulation Score with explicit recency weighting
        # (0.5 x current quarter's delta + 0.3 x prior + 0.2 x the one before).
        # Recent quarters count more heavily than older ones - a stock bought
        # aggressively 3 quarters ago and left untouched since is a weaker
        # signal than one still being actively built right now.
        RECENCY_WEIGHTS = (0.5, 0.3, 0.2)
        quarter_avg_deltas = []  # aligned with quarters_seen (most recent first)
        for q in quarters_seen[:len(RECENCY_WEIGHTS)]:
            q_entries = [e for e in entries if e["quarter"] == q]
            q_deltas = [e["delta_pct"] for e in q_entries if e["delta_pct"] is not None]
            quarter_avg_deltas.append(sum(q_deltas) / len(q_deltas) if q_deltas else 0.0)

        accumulation_score = sum(
            w * quarter_avg_deltas[i]
            for i, w in enumerate(RECENCY_WEIGHTS)
            if i < len(quarter_avg_deltas)
        )

        flags = []
        if build_quarters >= MULTI_QUARTER_BUILD_MIN:
            flags.append("MULTI_QUARTER_BUILD")
        if build_quarters >= 5:
            flags.append("STRONG_BUILD")
        if silent_build:
            flags.append("SILENT_ACCUMULATION")

        signals[ticker] = {
            "build_quarters":     build_quarters,
            "total_quarters":     len(quarters_seen),
            "avg_delta_pct":      round(avg_delta, 1),
            "weight_slope":       round(slope, 3),
            "build_score":        round(build_score, 2),
            "accumulation_score": round(accumulation_score, 2),
            "silent_build":       silent_build,
            "flags":              flags,
        }

    return signals
