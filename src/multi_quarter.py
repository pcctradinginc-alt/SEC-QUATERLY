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
from buy_signal import eligible_common_buy, material_buy


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


def build_multi_quarter_signals(today_str: str, current_parsed: dict | None = None) -> dict[str, dict]:
    """Only reward *consecutive*, material buys of the SAME security and CIK.

    Counting any positive quarter for any tracked manager gave EWY/LSCC
    spurious 75-100 accumulation points even when their per-ticker aggregate
    exhibited net reductions. No cross-manager stitching, nonconsecutive
    quarters, different CUSIPs or minor rebalance trades are accepted.

    build_quarters is the count of PRIOR consecutive qualifying quarters.
    signal_engine.factor_accumulation then adds the current qualifying
    quarter exactly once (legacy interface preserved).
    """
    from parse_13f import infer_report_date, previous_quarter_end

    if current_parsed is None:
        with (DATA_DIR / f"{today_str}_holdings_parsed.json").open() as fh:
            current_parsed = json.load(fh)

    current_quarter = infer_report_date(current_parsed)
    if not current_quarter:
        raise ValueError("Current reporting quarter is missing; cannot score accumulation")

    historical = load_historical_parsed(today_str)
    by_quarter = {infer_report_date(p): p for p in historical if infer_report_date(p)}
    current_filers = current_parsed.get("filers", {})
    result: dict[str, dict] = {}

    for filer_name, filer in current_filers.items():
        cik = str(filer.get("cik", "") or "").zfill(10)
        if not cik.strip("0"):
            # Never blend unidentified filers into a multi-quarter chain.
            continue
        for pos in filer.get("positions", []):
            if not eligible_common_buy(pos):
                continue
            ticker = pos.get("ticker") or pos.get("cusip")
            cusip = pos.get("cusip") or ""
            if not ticker:
                continue

            # A NEW position cannot already be an unbroken accumulation of
            # the same security unless the data are internally contradictory.
            prior_streak = 0
            consecutive = [pos]
            period = current_quarter
            if pos["delta"]["type"] == "ADDED":
                for _ in range(MULTI_QUARTER_MAX - 1):
                    period = previous_quarter_end(period)
                    old = by_quarter.get(period)
                    if old is None:
                        break
                    prior_filer = next((
                        f for f in old.get("filers", {}).values()
                        if str(f.get("cik", "") or "").zfill(10) == cik
                    ), None)
                    if prior_filer is None:
                        break
                    old_pos = next((
                        p for p in prior_filer.get("positions", [])
                        if (cusip and p.get("cusip") == cusip) or
                        (not cusip and p.get("ticker") == ticker)
                    ), None)
                    if old_pos is None or not eligible_common_buy(old_pos):
                        break
                    prior_streak += 1
                    consecutive.append(old_pos)
                    if old_pos["delta"]["type"] == "NEW":
                        break

            if prior_streak == 0:
                continue

            full_count = prior_streak + 1
            deltas = [p["delta"].get("delta_pct") or 0.0 for p in consecutive]
            weights = [p.get("port_weight_pct") or 0.0 for p in consecutive]
            silent = (
                full_count >= 3
                and all(p["delta"]["type"] == "ADDED" for p in consecutive[:3])
                and all(5.0 <= d <= 25.0 for d in deltas[:3])
            )
            recency = (0.5, 0.3, 0.2)
            weighted_delta = sum(
                recency[i] * deltas[i] for i in range(min(3, len(deltas)))
            )
            slope = ((weights[0] - weights[-1]) / (full_count - 1)) if full_count > 1 else 0.0

            flags = []
            if full_count >= MULTI_QUARTER_BUILD_MIN:
                flags.append("MULTI_QUARTER_BUILD")
            if full_count >= 5:
                flags.append("STRONG_BUILD")
            if silent:
                flags.append("SILENT_ACCUMULATION")

            info = {
                "build_quarters": prior_streak,
                "total_quarters": full_count,
                "consecutive_quarters": full_count,
                "manager_cik": cik,
                "manager_name": filer_name,
                "security_cusip": cusip,
                "avg_delta_pct": round(sum(deltas) / len(deltas), 1),
                "weight_slope": round(slope, 3),
                "build_score": round(full_count / 4.0 * max(weighted_delta, 0.0), 2),
                "accumulation_score": round(weighted_delta, 2),
                "silent_build": silent,
                "flags": flags,
            }
            previous = result.get(ticker)
            if previous is None or (full_count, weighted_delta, cik) > (
                previous["consecutive_quarters"],
                previous["accumulation_score"],
                previous["manager_cik"],
            ):
                result[ticker] = info

    return result
