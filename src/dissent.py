"""
dissent.py
Institutional Dissent Penalty.

Managers do not always agree. A stock can show three high-quality funds
accumulating while a fourth walks out of what was its largest position, and
until now the engine reported the buying and left the selling as a footnote.

This is deliberately NOT a ninth scoring factor. Buying and selling are not
symmetric - a purchase is a fresh decision, a sale is often a rebalance, a
redemption or a tax event - and a sell factor with its own weight would also
double-count what Activity and Consensus already measure. It is a capped
penalty subtracted after the bull score, exactly like the price-action penalty:

    base score + confluence − price-action penalty − dissent penalty

Counting sellers is not enough. A full exit from a former top-3 position by a
patient manager says something; a 22 % trim of a 0.4 % position by a
200-name quant book says almost nothing. So each sale is weighed by how severe
it was, how much conviction it had, and who made it - and several independent
sellers count for more than one, with diminishing returns.
"""

from config import (
    DISSENT_BANDS, DISSENT_CONVICTION_FLOOR, DISSENT_MAX_PENALTY,
    DISSENT_SEVERITY_BANDS, economic_group,
)

# Sales we cannot interpret are not dissent. Each of these is a measurement
# problem, not a decision by a manager.
EXCLUDED_REASONS = {
    "possible_corporate_action": "merger, spin-off or share-class swap",
    "no_baseline":               "filer has no prior quarter on file",
    "capped_book":               "top-500 book: a rank drop is not a sale",
}


def sell_severity(signal: dict) -> float:
    """EXIT is total; a reduction scales with how much was cut."""
    if signal.get("type") == "EXIT":
        return 1.0
    pct = abs(signal.get("delta_pct") or 0.0)
    severity = 0.0
    for threshold, value in DISSENT_SEVERITY_BANDS:      # ascending
        if pct >= threshold:
            severity = value
    return severity


def prior_conviction(signal: dict) -> float:
    """How much the position mattered to that manager BEFORE the sale."""
    rank = signal.get("prior_rank")
    if rank:
        if rank <= 3:
            return 1.00
        if rank <= 5:
            return 0.90
        if rank <= 10:
            return 0.75
    weight = signal.get("prior_port_weight") or signal.get("port_weight_pct") or 0.0
    return max(DISSENT_CONVICTION_FLOOR, min(1.0, weight / 10.0))


def event_strength(signal: dict) -> float:
    """
    Strength of one sale, 0-1.

    Deliberately not multiplicative down to zero: a genuine exit stays
    meaningful even when the manager is only average, so quality and conviction
    scale the event between 0.6x and 1.0x rather than between 0 and 1.
    """
    severity = sell_severity(signal)
    if severity <= 0:
        return 0.0
    quality = max(0.0, min(1.0, signal.get("manager_quality_score") or 0.0))
    return severity * (0.60 + 0.40 * prior_conviction(signal)) * (0.60 + 0.40 * quality)


def usable_sell_signals(signals: list[dict], buyers: list[dict] | None = None) -> list[dict]:
    """
    One event per economic decision-maker, keeping the strongest, after dropping
    everything that cannot be read as a decision.

    A manager that appears on both sides of the same issuer is netted to the buy
    side: that pattern is a share-class swap, not a change of mind.
    """
    buyer_groups = {economic_group(b["filer"]) for b in (buyers or [])}
    best: dict[str, dict] = {}

    for s in signals:
        if s.get("possible_corporate_action") or s.get("excluded"):
            continue
        if s.get("exit_detection_available") is False:
            continue
        if sell_severity(s) <= 0:
            continue
        group = economic_group(s.get("filer", ""))
        if group in buyer_groups:
            continue
        strength = event_strength(s)
        if group not in best or strength > event_strength(best[group]):
            best[group] = s

    return sorted(best.values(), key=lambda s: (-event_strength(s), s.get("filer", "")))


def _product(values) -> float:
    out = 1.0
    for v in values:
        out *= v
    return out


def classify(penalty: float, seller_count: int) -> str:
    if seller_count == 0 or penalty <= 0:
        return "NO_DISSENT"
    for upper, label in DISSENT_BANDS:                   # ascending
        if penalty <= upper:
            return label
    return "SELL_DOMINANT"


def compute(sell_signals: list[dict], buyers: list[dict] | None = None) -> dict:
    """
    Deterministic 0-DISSENT_MAX_PENALTY penalty plus the detail behind it.

    Order-independent: events are keyed by decision-maker and combined with
    commutative operations only.
    """
    events = usable_sell_signals(sell_signals, buyers)
    if not events:
        return {"penalty": 0.0, "stance": "NO_DISSENT", "seller_count": 0,
                "exit_count": 0, "reduce_count": 0, "dissent_score": 0.0,
                "strongest_seller": None, "strongest_action": None,
                "sellers": [], "reason": "no interpretable selling by a tracked manager"}

    strengths = [event_strength(e) for e in events]
    max_event = max(strengths)

    # Diminishing returns: two independent sellers add to one, ten do not
    # multiply it. Capped at 0.95 each so no single event saturates the mass.
    sell_mass = 1.0 - _product(1.0 - min(0.95, s) for s in strengths)

    # How much of the tracked activity in this name is selling? Used only to
    # modulate the penalty, so the buy side is never scored twice.
    seller_mass = sum(
        (e.get("manager_quality_score") or 0.0) * sell_severity(e) for e in events)
    buyer_mass = sum(
        (b.get("manager_quality_score") or 0.0) * _buy_strength(b) for b in (buyers or []))
    dissent_share = seller_mass / (seller_mass + buyer_mass) if (seller_mass + buyer_mass) else 1.0

    raw = 0.65 * max_event + 0.35 * sell_mass
    breadth_modifier = 0.75 + 0.25 * dissent_share
    penalty = round(min(DISSENT_MAX_PENALTY, DISSENT_MAX_PENALTY * raw * breadth_modifier), 1)

    strongest = events[0]
    exits = [e for e in events if e.get("type") == "EXIT"]

    return {
        "penalty":          penalty,
        "stance":           classify(penalty, len(events)),
        "seller_count":     len(events),
        "exit_count":       len(exits),
        "reduce_count":     len(events) - len(exits),
        "dissent_score":    round(raw * 100, 1),
        "dissent_share":    round(dissent_share, 3),
        "strongest_seller": strongest.get("filer"),
        "strongest_action": strongest.get("type"),
        "prior_weight_pct": strongest.get("prior_port_weight") or strongest.get("port_weight_pct"),
        "prior_rank":       strongest.get("prior_rank"),
        "sellers": [
            {"filer": e.get("filer"), "type": e.get("type"),
             "delta_pct": e.get("delta_pct"), "prior_rank": e.get("prior_rank"),
             "prior_weight_pct": e.get("prior_port_weight") or e.get("port_weight_pct"),
             "manager_quality_score": e.get("manager_quality_score"),
             "strength": round(event_strength(e), 3)}
            for e in events[:5]
        ],
        "reason": describe(events, penalty),
    }


def _buy_strength(buyer: dict) -> float:
    """Buy-side counterweight: a NEW position or a large add weighs more."""
    if buyer.get("delta_type") == "NEW":
        return 1.0
    delta = buyer.get("delta_pct") or 0.0
    return max(0.2, min(1.0, delta / 100.0))


def describe(events: list[dict], penalty: float) -> str:
    e = events[0]
    who = e.get("filer", "a tracked manager")
    if e.get("type") == "EXIT":
        rank = e.get("prior_rank")
        weight = e.get("prior_port_weight") or 0.0
        if rank and rank <= 10:
            where = f" its former #{rank} position"
        elif weight >= 0.05:
            where = f" a {weight:.1f}% position"
        else:
            where = " the position"
        action = f"{who} fully exited{where}"
    else:
        action = f"{who} cut its position by {abs(e.get('delta_pct') or 0):.0f}%"
    others = len(events) - 1
    if others:
        action += f", alongside {others} other independent seller{'s' if others > 1 else ''}"
    return f"{action} — {penalty:.1f} point dissent penalty"
