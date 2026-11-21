"""Shared, conservative definition of institutional accumulation.

13F share-count differences below 5% are not strong enough, on their own,
to count as an actionable institutional purchase. The threshold is a
research hypothesis, NOT a validated alpha parameter.
"""
from config import MIN_MATERIAL_ADD_PCT, MIN_PORTFOLIO_WEIGHT_PCT


def material_buy(pos: dict) -> bool:
    delta = pos.get("delta") or {}
    kind = delta.get("type")
    if kind == "NEW":
        return True
    if kind != "ADDED":
        return False
    pct = delta.get("delta_pct")
    return pct is not None and float(pct) >= MIN_MATERIAL_ADD_PCT


def eligible_common_buy(pos: dict) -> bool:
    """Only real, net-long equity positions with material share acquisition."""
    return (
        material_buy(pos)
        and (pos.get("port_weight_pct") or 0.0) >= MIN_PORTFOLIO_WEIGHT_PCT
        and pos.get("net_bullish", True)
        and not pos.get("possible_corporate_action", False)
        and pos.get("exposure_type", "COMMON_LONG") == "COMMON_LONG"
    )
