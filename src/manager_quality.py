"""
manager_quality.py
Dynamic Manager Quality Score (0-1) per filer (Section 7).

Replaces the previously static FILER_QUALITY lookup (config.py) with a
data-driven estimate built from information the pipeline already collects:
  - Concentration: fewer reported positions -> higher-conviction stock-picker
  - Turnover: value-weighted share of the portfolio that changed name-to-name
    quarter over quarter -> lower turnover = more patient/fundamental investor

The spec (Section 7) also calls for historical stock-picking performance and
backtest-calibrated weights. That needs years of quarters this system does
not have yet (it has run for 2 quarters as of this writing) – see backtest.py
for the performance-tracking groundwork. Until MANAGER_QUALITY_MIN_HISTORY_QUARTERS
quarters of history exist for a filer, its score blends toward the old
FILER_QUALITY static prior instead of overfitting to noisy single-quarter data.
"""

from config import (
    FILER_QUALITY, MANAGER_QUALITY_MIN_HISTORY_QUARTERS,
    MQ_CONCENTRATION_CEIL_POSITIONS, MQ_CONCENTRATION_FLOOR_POSITIONS,
    MQ_TURNOVER_FULL_PENALTY_PCT,
)
from multi_quarter import load_historical_parsed


def _static_prior(filer_name: str) -> float:
    """Maps the old hand-assigned FILER_QUALITY (0.8-1.3) onto a 0-1 prior."""
    raw = FILER_QUALITY.get(filer_name, 1.0)
    return max(0.0, min(1.0, (raw - 0.8) / 0.5))


def _concentration_score(position_count: int) -> float:
    """1.0 for concentrated stock-pickers, 0.0 for 200+ position diversified/quant books."""
    if position_count <= MQ_CONCENTRATION_FLOOR_POSITIONS:
        return 1.0
    if position_count >= MQ_CONCENTRATION_CEIL_POSITIONS:
        return 0.0
    span = MQ_CONCENTRATION_CEIL_POSITIONS - MQ_CONCENTRATION_FLOOR_POSITIONS
    return 1.0 - (position_count - MQ_CONCENTRATION_FLOOR_POSITIONS) / span


def _turnover_score(avg_turnover_pct: float) -> float:
    """Lower turnover -> higher score. Clipped [0,1]."""
    if avg_turnover_pct <= 0:
        return 1.0
    if avg_turnover_pct >= MQ_TURNOVER_FULL_PENALTY_PCT:
        return 0.0
    return 1.0 - (avg_turnover_pct / MQ_TURNOVER_FULL_PENALTY_PCT)


def _position_key(pos: dict) -> str:
    # CUSIP-first: stable across quarters even when OpenFIGI ticker resolution
    # flips (see parse_13f.py's prior-quarter join for the same reasoning).
    return pos.get("cusip") or pos.get("ticker") or pos.get("name", "")


def _quarter_turnover_pct(prev_positions: list[dict], curr_positions: list[dict]) -> float | None:
    """
    Two-sided turnover: value-weighted share of the portfolio that either
    left (was in prev, gone from curr) or is brand new (in curr, not in prev),
    relative to prev portfolio value. None if prev portfolio had no reported value.
    """
    prev_by_key = {_position_key(p): p for p in prev_positions}
    curr_by_key = {_position_key(p): p for p in curr_positions}

    prev_total = sum(p.get("value_usd_k", 0) for p in prev_positions)
    if prev_total <= 0:
        return None

    exited_value = sum(
        p.get("value_usd_k", 0)
        for k, p in prev_by_key.items() if k not in curr_by_key
    )
    new_value = sum(
        p.get("value_usd_k", 0)
        for k, p in curr_by_key.items() if k not in prev_by_key
    )

    return (exited_value + new_value) / (2 * prev_total) * 100.0


def compute_manager_quality(history: list[dict]) -> dict[str, dict]:
    """
    history: list of parsed-quarter dicts (parse_13f.py output shape),
             MOST RECENT FIRST (current quarter's parsed dict must be history[0]).

    Returns {filer_name: {quality_score, concentration_score, turnover_score,
                           avg_turnover_pct, position_count, quarters_used,
                           is_bootstrapped}}
    """
    results: dict[str, dict] = {}

    all_filer_names: set[str] = set()
    for q in history:
        all_filer_names.update(q.get("filers", {}).keys())

    for filer_name in all_filer_names:
        quarters_with_data = [
            q for q in history
            if filer_name in q.get("filers", {})
            and "positions" in q["filers"][filer_name]
        ]
        if not quarters_with_data:
            continue

        latest = quarters_with_data[0]["filers"][filer_name]
        position_count = latest.get("position_count", len(latest.get("positions", [])))
        concentration = _concentration_score(position_count)
        prior = _static_prior(filer_name)
        quarters_used = len(quarters_with_data)

        turnovers = []
        for i in range(len(quarters_with_data) - 1):
            curr = quarters_with_data[i]["filers"][filer_name]["positions"]
            prev = quarters_with_data[i + 1]["filers"][filer_name]["positions"]
            t = _quarter_turnover_pct(prev, curr)
            if t is not None:
                turnovers.append(t)

        if quarters_used < MANAGER_QUALITY_MIN_HISTORY_QUARTERS or not turnovers:
            # Not enough history yet - lean on the static prior, but still
            # surface the concentration signal since that needs only 1 quarter.
            quality = 0.7 * prior + 0.3 * concentration
            results[filer_name] = {
                "quality_score":       round(max(0.0, min(1.0, quality)), 3),
                "concentration_score": round(concentration, 3),
                "turnover_score":      None,
                "avg_turnover_pct":    None,
                "position_count":      position_count,
                "quarters_used":       quarters_used,
                "is_bootstrapped":     True,
            }
            continue

        avg_turnover = sum(turnovers) / len(turnovers)
        turnover_score = _turnover_score(avg_turnover)

        # Blend data-driven components with the static prior; the prior's
        # weight shrinks toward 0 as more history accumulates (fully
        # data-driven by MULTI_QUARTER_MAX=8 quarters).
        prior_weight = max(0.0, (8 - quarters_used) / 8.0) * 0.4
        data_score = 0.5 * concentration + 0.5 * turnover_score
        quality = prior_weight * prior + (1 - prior_weight) * data_score

        results[filer_name] = {
            "quality_score":       round(max(0.0, min(1.0, quality)), 3),
            "concentration_score": round(concentration, 3),
            "turnover_score":      round(turnover_score, 3),
            "avg_turnover_pct":    round(avg_turnover, 1),
            "position_count":      position_count,
            "quarters_used":       quarters_used,
            "is_bootstrapped":     False,
        }

    return results


def build_manager_quality_scores(today_str: str, current_parsed: dict) -> dict[str, dict]:
    """Convenience wrapper: current quarter + all available prior history."""
    history = [current_parsed] + load_historical_parsed(today_str)
    return compute_manager_quality(history)
