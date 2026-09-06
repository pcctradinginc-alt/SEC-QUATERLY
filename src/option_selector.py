"""
option_selector.py
Deterministic selection of ONE liquid Call per Top-10 stock.

A contract is eligible only if it passes every predefined filter in
config.py (expiry window, delta window, max spread, min volume, min open
interest, max IV). Among eligible contracts the pick is the one with the
lowest selection cost:

    cost = 1.0 × |delta − OPTION_DELTA_TARGET| / 0.20
         + 0.5 × spread_pct / OPTION_MAX_SPREAD_PCT
         + 0.3 × |dte − mid_window| / half_window
         − 0.2 × min(1, log10(1 + volume + open_interest) / 4)

Ties break on symbol (A→Z). If nothing is eligible the result is the
literal string NO_SUITABLE_OPTION_FOUND, and the reason each candidate was
rejected is recorded for the report.

Pure functions only – the network work lives in options_lookup.py.
"""

import math
from datetime import date

from config import (
    NO_SUITABLE_OPTION, OPTION_DELTA_MAX, OPTION_DELTA_MIN, OPTION_DELTA_TARGET,
    OPTION_MAX_DAYS, OPTION_MAX_IV, OPTION_MAX_SPREAD_PCT, OPTION_MIN_DAYS,
    OPTION_MIN_OPEN_INT, OPTION_MIN_VOLUME, OPTION_OI_WAIVES_VOLUME,
)

FILTERS_DESCRIPTION = {
    "expiry_days":   f"{OPTION_MIN_DAYS}–{OPTION_MAX_DAYS} days to expiration",
    "delta":         f"{OPTION_DELTA_MIN:.2f}–{OPTION_DELTA_MAX:.2f} call delta (target {OPTION_DELTA_TARGET:.2f})",
    "spread":        f"bid-ask spread ≤ {OPTION_MAX_SPREAD_PCT:.0f}% of mid",
    "volume":        (f"volume ≥ {OPTION_MIN_VOLUME} "
                      f"(waived when open interest ≥ {OPTION_OI_WAIVES_VOLUME:,})"),
    "open_interest": f"open interest ≥ {OPTION_MIN_OPEN_INT}",
    "iv":            f"implied volatility known and ≤ {OPTION_MAX_IV:.0%}",
}


def _dte(expiration: str, today: date) -> int | None:
    try:
        return (date.fromisoformat(expiration) - today).days
    except (TypeError, ValueError):
        return None


def check_contract(opt: dict, today: date) -> list[str]:
    """Returns the list of failed filter names (empty = eligible)."""
    fails = []
    if (opt.get("option_type") or "").lower() != "call":
        fails.append("not_a_call")

    dte = _dte(opt.get("expiration_date", ""), today)
    if dte is None or not (OPTION_MIN_DAYS <= dte <= OPTION_MAX_DAYS):
        fails.append("expiry_days")

    delta = opt.get("delta")
    try:
        delta = abs(float(delta)) if delta is not None else None
    except (TypeError, ValueError):
        delta = None
    if delta is None or not (OPTION_DELTA_MIN <= delta <= OPTION_DELTA_MAX):
        fails.append("delta")

    bid, ask = float(opt.get("bid") or 0), float(opt.get("ask") or 0)
    mid = (bid + ask) / 2
    if mid <= 0 or bid <= 0:
        fails.append("spread")
    else:
        if (ask - bid) / mid * 100.0 > OPTION_MAX_SPREAD_PCT:
            fails.append("spread")

    # Open interest is the session-independent liquidity measure and is always
    # enforced. Daily volume is a counter that resets each morning, so a chain
    # pulled early in the session under-reports it for every strike (the
    # scheduled run fires 30 minutes after the US open). A contract carrying
    # deep open interest is liquid regardless of that partial-session count,
    # so its volume floor is waived - deterministically, from the data alone.
    oi = int(opt.get("open_interest") or 0)
    if oi < OPTION_MIN_OPEN_INT:
        fails.append("open_interest")
    if int(opt.get("volume") or 0) < OPTION_MIN_VOLUME and oi < OPTION_OI_WAIVES_VOLUME:
        fails.append("volume")

    # Every filter must be satisfied, so an unknown IV is a failure rather than
    # a pass: we cannot claim a contract is not over-priced when we do not know
    # what it costs in volatility terms.
    iv = opt.get("implied_volatility")
    try:
        iv = float(iv) if iv is not None else None
    except (TypeError, ValueError):
        iv = None
    if iv is None:
        fails.append("iv_missing")
    elif iv > OPTION_MAX_IV:
        fails.append("iv")
    return fails


def selection_cost(opt: dict, today: date) -> float:
    delta = abs(float(opt["delta"]))
    bid, ask = float(opt["bid"]), float(opt["ask"])
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100.0
    dte = _dte(opt["expiration_date"], today) or OPTION_MIN_DAYS
    mid_window = (OPTION_MIN_DAYS + OPTION_MAX_DAYS) / 2
    half_window = (OPTION_MAX_DAYS - OPTION_MIN_DAYS) / 2
    liq = math.log10(1 + int(opt.get("volume") or 0) + int(opt.get("open_interest") or 0)) / 4
    return (
        1.0 * abs(delta - OPTION_DELTA_TARGET) / 0.20
        + 0.5 * spread_pct / OPTION_MAX_SPREAD_PCT
        + 0.3 * abs(dte - mid_window) / half_window
        - 0.2 * min(1.0, liq)
    )


def select_call(chain: list[dict], today: date, spot: float | None = None) -> dict:
    """
    chain: raw contracts (Tradier shape, greeks nested or flattened).
    Returns {status: "OK"|NO_SUITABLE_OPTION_FOUND, contract, rejections, eligible_count}
    """
    eligible, rejections = [], {}
    for opt in chain:
        o = _flatten(opt)
        fails = check_contract(o, today)
        if fails:
            for f in fails:
                rejections[f] = rejections.get(f, 0) + 1
        else:
            eligible.append(o)

    if not eligible:
        return {
            "status":         NO_SUITABLE_OPTION,
            "contract":       None,
            "eligible_count": 0,
            "candidates_checked": len(chain),
            "rejections":     dict(sorted(rejections.items())),
            "filters":        FILTERS_DESCRIPTION,
        }

    eligible.sort(key=lambda o: (round(selection_cost(o, today), 6), o.get("symbol", "")))
    best = eligible[0]
    bid, ask = float(best["bid"]), float(best["ask"])
    mid = round((bid + ask) / 2, 2)
    dte = _dte(best["expiration_date"], today)
    moneyness = None
    if spot:
        moneyness = round((float(best["strike"]) / float(spot) - 1) * 100.0, 1)

    contract = {
        "symbol":            best.get("symbol"),
        "strike":            float(best["strike"]),
        "expiration":        best["expiration_date"],
        "dte":               dte,
        "bid":               bid,
        "ask":               ask,
        "mid":               mid,
        "spread_pct":        round((ask - bid) / mid * 100.0, 1) if mid else None,
        "volume":            int(best.get("volume") or 0),
        "open_interest":     int(best.get("open_interest") or 0),
        "delta":             round(abs(float(best["delta"])), 3),
        "implied_volatility": (round(float(best["implied_volatility"]), 4)
                               if best.get("implied_volatility") is not None else None),
        "moneyness_pct":     moneyness,
        "max_risk_per_contract": round(mid * 100, 2),
        "breakeven":         round(float(best["strike"]) + mid, 2),
        "selection_cost":    round(selection_cost(best, today), 4),
    }
    runner_up = [
        {"symbol": o.get("symbol"), "delta": round(abs(float(o["delta"])), 3),
         "selection_cost": round(selection_cost(o, today), 4)}
        for o in eligible[1:4]
    ]
    return {
        "status":         "OK",
        "contract":       contract,
        "eligible_count": len(eligible),
        "candidates_checked": len(chain),
        "rejections":     dict(sorted(rejections.items())),
        "runner_up":      runner_up,
        "filters":        FILTERS_DESCRIPTION,
        "rule_rationale": rule_rationale(contract),
    }


def rule_rationale(c: dict) -> str:
    money = ""
    if c.get("moneyness_pct") is not None:
        m = c["moneyness_pct"]
        money = f", strike {abs(m):.1f}% {'above' if m > 0 else 'below'} spot"
    return (f"Delta {c['delta']:.2f} sits closest to the {OPTION_DELTA_TARGET:.2f} target"
            f"{money}; {c['dte']} days to expiry gives the 13F thesis time to play out; "
            f"spread {c['spread_pct']}% with volume {c['volume']:,} and open interest "
            f"{c['open_interest']:,} keeps entry and exit liquid.")


def _flatten(opt: dict) -> dict:
    """Accepts either Tradier's nested greeks or options_lookup's flat shape."""
    g = opt.get("greeks") or {}
    out = dict(opt)
    if "delta" not in out or out.get("delta") is None:
        out["delta"] = g.get("delta")
    if out.get("implied_volatility") is None:
        out["implied_volatility"] = g.get("smv_vol") or g.get("mid_iv")
    return out
