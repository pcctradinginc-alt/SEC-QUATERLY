"""
signal_engine.py
Deterministic 0-100 SIGNAL SCORE and Top-10 ranking.

Answers: "Which 10 stocks currently show the strongest combination of
high-conviction institutional accumulation and confirming SEC insider
activity?"

Eight factors, each a pure 0-100 transform with FIXED anchors (not
universe-relative percentiles), combined with the weights in
config.SIGNAL_WEIGHTS (sum = 100):

    activity         NEW / ADD strength of the best buyer (+ breadth)
    conviction       position size / rank inside the buyer's own book
    manager_quality  dynamic quality of the buying managers
    accumulation     multi-quarter build (current + historical quarters)
    consensus        quality-weighted number of independent buyers
    insider          Form 4 open-market buying since the 13F quarter-end
    freshness        filing delay × turnover decay, and age of the filing
    crowding         inverse crowding (LOW = 100 ... EXTREME = 0)

A capped price-action penalty is subtracted for names that already ran
hard since the filing. Ties are broken deterministically (insider, then
consensus, then ticker A→Z). The same input JSON always yields the same
ranking; a SHA-256 fingerprint of the inputs is stored alongside the
output so this can be verified.

Every Top-10 entry carries a rule-based `why` block (top drivers + reasons
per factor) so the report explains itself even when no LLM narrative is
available.
"""

import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import date

from config import (
    run_date,
    CONFLUENCE_MAX_BONUS, CONFLUENCE_MIN_13F_SCORE, CONFLUENCE_MIN_INSIDER,
    CROWDING_FACTOR_BY_LABEL, DATA_DIR, PRICE_ACTION_DOWNGRADE_PCT,
    PRICE_ACTION_WARN_PCT, SIGNAL_CANDIDATE_POOL, SIGNAL_PRICE_PENALTY_CAP,
    SIGNAL_WEIGHTS, TOP_N,
)

FACTOR_LABELS = {
    "activity":        "NEW / ADD activity",
    "conviction":      "Portfolio conviction",
    "manager_quality": "Manager quality",
    "accumulation":    "Multi-quarter accumulation",
    "consensus":       "Smart-money consensus",
    "insider":         "Insider buying (Form 4)",
    "freshness":       "Filing freshness",
    "crowding":        "Institutional crowding",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def is_tradable_ticker(t: str) -> bool:
    """
    True only for a resolved, tradable US symbol.

    An unmapped CUSIP ("G4474Y214", "68634K106") is an internal identifier, not
    a ticker: it cannot be priced, cannot be looked up on an options chain and
    must never appear in a public ranking. Such holdings stay in the book for
    AUM and weight accounting, but they do not enter the candidate universe.
    """
    if not t or len(t) > 6:
        return False
    return t[0].isalpha()


# Legacy alias
_is_option_eligible_ticker = is_tradable_ticker


_ISSUER_SUFFIX_RE = re.compile(
    r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|LLC|LP|PLC|NV|SA|AG|"
    r"HOLDINGS?|GROUP|TRUST|CLASS|CL|SER|SERIES|COM|NEW|DEL|THE)\b")


def issuer_key(row: dict) -> str:
    """
    Grouping key for share-class detection: the normalised issuer name, falling
    back to the ticker root.

    A matching name is NECESSARY but NOT SUFFICIENT to treat two rows as the
    same instrument - every iShares ETF reports the issuer name "ISHARES INC",
    so name-only matching merges South Korea (EWY) with Brazil (EWZ). Callers
    must also pass same_share_class_family() on the tickers.
    """
    name = _ISSUER_SUFFIX_RE.sub("", (row.get("name") or "").upper())
    name = re.sub(r"[^A-Z0-9 ]", " ", name)
    name = " ".join(name.split())
    # After the class words are stripped a bare share-class letter can remain
    # ("ALPHABET A" vs "ALPHABET C"); drop it so both classes collapse.
    name = re.sub(r" [ABCK]$", "", name)
    if name:
        return name
    return (row.get("ticker") or "").split("/")[0].upper()


def _ticker_root(t: str) -> str:
    return (t or "").split("/")[0].upper()


def same_share_class_family(a: str, b: str) -> bool:
    """
    Are two tickers share classes of the SAME instrument?

    True for BRK/A vs BRK/B (same root before the class separator),
    GOOG vs GOOGL, UA vs UAA, FOX vs FOXA, CORZ vs CORZW (one is a strict
    prefix of the other) and LLYVA vs LLYVK (equal length, differing only in a
    trailing class letter, sharing at least four characters).

    False for EWY vs EWZ and IWM vs INDA - different funds that merely share a
    legal issuer name.
    """
    ra, rb = _ticker_root(a), _ticker_root(b)
    if not ra or not rb:
        return False
    if "/" in (a or "") or "/" in (b or ""):
        return ra == rb
    if ra == rb:
        return True
    lo, hi = sorted((ra, rb), key=len)
    if hi.startswith(lo):
        return True
    return len(ra) == len(rb) and len(ra) >= 5 and ra[:-1] == rb[:-1]


def _grade(score: float) -> str:
    if score >= 75:
        return "VERY_STRONG"
    if score >= 60:
        return "STRONG"
    if score >= 45:
        return "MODERATE"
    return "WEAK"


# ── factor transforms (pure functions) ────────────────────────────────────────

def factor_activity(filers: list[dict]) -> tuple[float, list[str]]:
    best, reasons = 0.0, []
    for f in filers:
        w = f.get("port_weight_pct") or 0.0
        if f.get("delta_type") == "NEW":
            base = 40.0 if w < 1 else 60.0 if w < 3 else 80.0 if w < 5 else 90.0 if w < 10 else 100.0
        else:
            d = f.get("delta_pct") or 0.0
            base = 30.0 + min(70.0, max(0.0, d) * 0.7)
        best = max(best, base)
    breadth = min(20.0, 5.0 * (len(filers) - 1))
    score = _clamp(best + breadth)

    news = [f for f in filers if f.get("delta_type") == "NEW"]
    adds = [f for f in filers if f.get("delta_type") != "NEW"]
    if news:
        top = max(news, key=lambda f: (f.get("port_weight_pct") or 0.0, f["filer"]))
        reasons.append(f"{top['filer']} opened a NEW position at {top['port_weight_pct']:.1f}% of its book")
    if adds:
        top = max(adds, key=lambda f: (f.get("delta_pct") or 0.0, f["filer"]))
        reasons.append(f"{top['filer']} added {top.get('delta_pct') or 0:+.0f}% to an existing position")
    if len(filers) > 1:
        reasons.append(f"{len(filers)} tracked managers bought this quarter")
    return round(score, 1), reasons


def factor_conviction(filers: list[dict]) -> tuple[float, list[str]]:
    reasons = []
    max_w = max((f.get("port_weight_pct") or 0.0) for f in filers)
    weight_part = _clamp(max_w / 10.0 * 100.0)          # 10% of a book = 100
    tier_part = 0.0
    for f in filers:
        t = f.get("position_tier")
        tier_part = max(tier_part, {"TOP3": 85.0, "TOP5": 70.0, "TOP10": 55.0}.get(t, 0.0))
    outsized = any((f.get("weight_vs_median") or 0.0) >= 5.0 for f in filers)
    score = _clamp(max(weight_part, tier_part) + (10.0 if outsized else 0.0))

    top = max(filers, key=lambda f: ((f.get("port_weight_pct") or 0.0), f["filer"]))
    reasons.append(f"Largest buyer weight: {top['port_weight_pct']:.1f}% of {top['filer']}'s portfolio"
                   + (f" (rank {top.get('position_tier')})" if top.get("position_tier") in ("TOP3", "TOP5", "TOP10") else ""))
    if outsized:
        reasons.append("Position is ≥5× the manager's own median position size")
    return round(score, 1), reasons


def factor_manager_quality(filers: list[dict]) -> tuple[float, list[str]]:
    qs = [f.get("manager_quality_score") or 0.5 for f in filers]
    score = _clamp((0.6 * max(qs) + 0.4 * sum(qs) / len(qs)) * 100.0)
    best = max(filers, key=lambda f: ((f.get("manager_quality_score") or 0.0), f["filer"]))
    reasons = [f"Highest-quality buyer: {best['filer']} (quality {best.get('manager_quality_score', 0):.2f})"]
    if len(qs) > 1:
        reasons.append(f"Average buyer quality {sum(qs)/len(qs):.2f} across {len(qs)} managers")
    return round(score, 1), reasons


def factor_accumulation(mq: dict, is_buy_now: bool) -> tuple[float, list[str]]:
    hist = (mq or {}).get("build_quarters", 0)
    effective = hist + (1 if is_buy_now else 0)
    base = {0: 0.0, 1: 15.0, 2: 50.0, 3: 75.0}.get(effective, 100.0)
    bonus = 10.0 if (mq or {}).get("silent_build") else 0.0
    score = _clamp(base + bonus)
    if effective >= 2:
        reasons = [f"Built over {effective} consecutive quarters" + (" (silent accumulation)" if bonus else "")]
    else:
        reasons = ["First quarter of buying – no multi-quarter build yet"]
    return round(score, 1), reasons


def factor_consensus(filers: list[dict]) -> tuple[float, list[str]]:
    n = len(filers)
    qs = [f.get("manager_quality_score") or 0.5 for f in filers]
    avg_q = sum(qs) / n
    base = {1: 30.0, 2: 60.0, 3: 80.0, 4: 95.0}.get(n, 100.0)
    score = _clamp(base * (0.5 + 0.5 * avg_q))
    names = ", ".join(sorted(f["filer"] for f in filers))
    reasons = [f"{n} independent buyer{'s' if n != 1 else ''}: {names}"] if n <= 4 else \
              [f"{n} independent buyers incl. {', '.join(sorted(f['filer'] for f in filers)[:3])} …"]
    return round(score, 1), reasons


def factor_freshness(filers: list[dict], today: date) -> tuple[float, list[str]]:
    fs = [f.get("freshness_score") or 0.5 for f in filers]
    avg_fs = sum(fs) / len(fs)
    ages = []
    for f in filers:
        fd = f.get("filing_date")
        if fd:
            try:
                ages.append((today - date.fromisoformat(fd)).days)
            except ValueError:
                pass
    age = min(ages) if ages else 60
    age_part = _clamp(100.0 - age * 100.0 / 120.0)     # 120 days after filing = 0
    score = _clamp(0.7 * avg_fs * 100.0 + 0.3 * age_part)
    reasons = [f"Filing is {age} days old; low-turnover buyers keep it informative" if avg_fs >= 0.7
               else f"Filing is {age} days old; high-turnover buyers make it staler"]
    return round(score, 1), reasons


def factor_crowding(label: str, penalty: float | None) -> tuple[float, list[str]]:
    """Inverse of scoring.py's 0-100 crowding penalty (hotel list + oversized cluster);
    falls back to the coarse label when the penalty is missing."""
    if penalty is not None:
        score = _clamp(100.0 - float(penalty))
    else:
        score = CROWDING_FACTOR_BY_LABEL.get(label or "LOW", 60.0)
    lbl = "LOW" if score > 75 else "MODERATE" if score > 50 else "HIGH" if score > 25 else "EXTREME"
    reasons = [f"Crowding {lbl} – {'room for further institutional sponsorship' if score > 50 else 'trade is already popular'}"]
    return round(score, 1), reasons


def confluence_bonus(score_13f: float, insider: float) -> tuple[float, str | None]:
    """
    Explicit interaction term: a weighted sum alone rates "excellent 13F, no
    insider" the same as "average 13F, excellent insider". The engine is looking
    for the two firing TOGETHER, so co-occurrence earns a capped bonus that
    scales with whichever side is weaker.
    """
    if score_13f < CONFLUENCE_MIN_13F_SCORE or insider < CONFLUENCE_MIN_INSIDER:
        return 0.0, None
    reach_13f = (score_13f - CONFLUENCE_MIN_13F_SCORE) / max(1.0, 100.0 - CONFLUENCE_MIN_13F_SCORE)
    reach_ins = (insider - CONFLUENCE_MIN_INSIDER) / max(1.0, 100.0 - CONFLUENCE_MIN_INSIDER)
    bonus = round(CONFLUENCE_MAX_BONUS * min(1.0, 0.5 + 0.5 * min(reach_13f, reach_ins)), 1)
    return bonus, (f"Institutional accumulation and insider buying confirm each other "
                   f"(13F {score_13f:.0f}, insider {insider:.0f}) → +{bonus:.0f} confluence")


def classify_signal(row: dict, bonus: float) -> tuple[str, str]:
    """Deterministic signal class + human label for the report badge."""
    f = row["factors"]
    ins = (row.get("insider") or {}).get("summary") or {}
    early = "EARLY_SMART_MONEY_ACCUMULATION" in row.get("flags", [])
    confirmed = bonus > 0 and (ins.get("buy_value_usd", 0) or 0) > 0

    if early and confirmed:
        return "EARLY_SMART_MONEY_WITH_INSIDER_CONFIRMATION", "Early smart money + insider confirmation"
    if confirmed:
        return "ACCUMULATION_WITH_INSIDER_CONFIRMATION", "Accumulation + insider confirmation"
    if early:
        return "EARLY_SMART_MONEY_ACCUMULATION", "Early smart money accumulation"
    if f["accumulation"] >= 50:
        return "MULTI_QUARTER_ACCUMULATION", "Multi-quarter accumulation"
    if f["consensus"] >= 80:
        return "SMART_MONEY_CONSENSUS", "Smart-money consensus"
    if f["conviction"] >= 90 and f["activity"] >= 90:
        return "LARGE_NEW_POSITION", "Large new position"
    return "SINGLE_MANAGER_CONVICTION", "Single-manager conviction"


def price_penalty(perf: dict) -> tuple[float, str | None]:
    pct = (perf or {}).get("pct_change")
    if pct is None:
        return 0.0, None
    if pct >= PRICE_ACTION_DOWNGRADE_PCT:
        return SIGNAL_PRICE_PENALTY_CAP, f"Already +{pct:.0f}% since the filing quarter-end – part of the thesis may be priced in"
    if pct >= PRICE_ACTION_WARN_PCT:
        return round(SIGNAL_PRICE_PENALTY_CAP / 2, 1), f"Up +{pct:.0f}% since quarter-end – entry is less fresh"
    return 0.0, None


# ── assembly ──────────────────────────────────────────────────────────────────

def _filer_rows(agg: dict, scored_flat: list[dict]) -> list[dict]:
    """Per-filer rows for a ticker, enriched with report/filing dates from scored_flat."""
    dates_by_filer: dict[str, tuple] = {}
    for e in scored_flat:
        if e.get("ticker") == agg["ticker"]:
            dates_by_filer[e["filer"]] = (
                e.get("report_date") or e.get("filing_date"),
                e.get("filing_date_actual") or e.get("filing_date"),
            )
    rows = []
    for f in agg.get("filers", []):
        rd, fd = dates_by_filer.get(f["filer"], (f.get("report_date"), f.get("filing_date_actual")))
        rows.append({**f, "report_date": rd, "filing_date": fd})
    rows.sort(key=lambda r: r["filer"])
    return rows


def score_ticker(agg: dict, scored_flat: list[dict], mq_signals: dict,
                 insider: dict | None, today: date) -> dict:
    filers = _filer_rows(agg, scored_flat)
    factors: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}

    factors["activity"],        reasons["activity"]        = factor_activity(filers)
    factors["conviction"],      reasons["conviction"]      = factor_conviction(filers)
    factors["manager_quality"], reasons["manager_quality"] = factor_manager_quality(filers)
    factors["accumulation"],    reasons["accumulation"]    = factor_accumulation(mq_signals.get(agg["ticker"], {}), True)
    factors["consensus"],       reasons["consensus"]       = factor_consensus(filers)
    factors["insider"] = float((insider or {}).get("score", 0.0))
    reasons["insider"] = list((insider or {}).get("reasons") or ["Insider data not evaluated for this name"])
    factors["freshness"],       reasons["freshness"]       = factor_freshness(filers, today)
    factors["crowding"],        reasons["crowding"]        = factor_crowding(agg.get("crowding_label"), agg.get("crowding_penalty"))

    contributions = {k: round(SIGNAL_WEIGHTS[k] * factors[k] / 100.0, 2) for k in SIGNAL_WEIGHTS}
    raw = sum(contributions.values())

    # The 13F side and the insider side are computed as SEPARATE signals first
    # (spec: never blend the two before each stands on its own), then combined.
    weight_13f = sum(w for k, w in SIGNAL_WEIGHTS.items() if k != "insider")
    score_13f  = round(sum(v for k, v in contributions.items() if k != "insider")
                       / weight_13f * 100.0, 1)
    insider_sc = factors["insider"]
    bonus, bonus_reason = confluence_bonus(score_13f, insider_sc)

    penalty, penalty_reason = price_penalty(agg.get("post_filing_perf"))
    score = round(_clamp(raw + bonus - penalty), 1)

    top_drivers = sorted(contributions.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    why_bullets = []
    for k, _ in top_drivers:
        why_bullets.extend(reasons[k][:1])
    if factors["insider"] > 0 and "insider" not in [k for k, _ in top_drivers]:
        why_bullets.append(reasons["insider"][0])
    if bonus_reason:
        why_bullets.append(bonus_reason)
    if penalty_reason:
        why_bullets.append(penalty_reason)

    row = {
        "ticker":            agg["ticker"],
        "name":              agg.get("name", ""),
        "signal_score":      score,
        "score_13f":         score_13f,
        "insider_score":     insider_sc,
        "confluence_bonus":  bonus,
        "grade":             _grade(score),
        "factors":           factors,
        "contributions":     contributions,
        "price_penalty":     penalty,
        "reasons":           reasons,
        "why": {
            "top_drivers": [{"factor": k, "label": FACTOR_LABELS[k], "points": v} for k, v in top_drivers],
            "bullets":     why_bullets,
            "summary":     _summary_sentence(agg, factors, top_drivers, insider),
        },
        "filers":            filers,
        "filer_count":       len(filers),
        "flags":             sorted(agg.get("flags", [])),
        "crowding_label":    ("LOW" if factors["crowding"] > 75 else "MODERATE" if factors["crowding"] > 50
                              else "HIGH" if factors["crowding"] > 25 else "EXTREME"),
        "post_filing_perf":  agg.get("post_filing_perf", {}),
        "mq_signal":         mq_signals.get(agg["ticker"], {}),
        "insider":           insider or {},
        "alpha_score_legacy": agg.get("alpha_score"),
        "tradable_ticker_validated": is_tradable_ticker(agg["ticker"]),
        "option_eligible":   is_tradable_ticker(agg["ticker"]),
    }
    row["signal_class"], row["signal_label"] = classify_signal(row, bonus)
    return row


def _summary_sentence(agg: dict, factors: dict, top_drivers: list, insider: dict | None) -> str:
    labels = [FACTOR_LABELS[k].lower() for k, _ in top_drivers]
    ins = insider or {}
    buy_val = (ins.get("summary") or {}).get("buy_value_usd", 0) or 0
    insider_txt = (f", confirmed by ${buy_val:,.0f} of insider open-market buying since quarter-end"
                   if buy_val > 0 else ", without confirming insider purchases so far")
    return (f"{agg['ticker']} ranks on {labels[0]}, {labels[1]} and {labels[2]}"
            f"{insider_txt}.")


def compute_signals(scores: dict, insider_by_ticker: dict[str, dict], today: date) -> dict:
    """
    Pure function: (scores.json content, insider look-ups, date) -> ranking dict.
    No I/O, no randomness, order-independent.
    """
    aggregated  = scores.get("aggregated", [])
    scored_flat = scores.get("scored_flat", [])
    mq_signals  = scores.get("mq_signals", {})

    rows = [
        score_ticker(agg, scored_flat, mq_signals, insider_by_ticker.get(agg["ticker"]), today)
        for agg in aggregated
        if agg.get("ticker") and agg.get("filers")
    ]
    rows.sort(key=lambda r: (-r["signal_score"], -r["factors"]["insider"], -r["factors"]["consensus"], r["ticker"]))
    for i, r in enumerate(rows, 1):
        r["overall_rank"] = i          # position among every scored name (incl. CUSIP-only rows)

    eligible = [r for r in rows if r["tradable_ticker_validated"]]

    # One instrument occupies one slot: keep the highest-scoring share class and
    # record the ones folded into it. A row is folded only when BOTH the issuer
    # name and the ticker family match, so different funds of one ETF sponsor
    # stay separate.
    deduped: list[dict] = []
    kept_by_name: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:                                   # already score-sorted
        key = issuer_key(r)
        host = next((k for k in kept_by_name[key]
                     if same_share_class_family(k["ticker"], r["ticker"])), None)
        if host is not None:
            host.setdefault("same_issuer_alternates", []).append(
                {"ticker": r["ticker"], "signal_score": r["signal_score"]})
            r["superseded_by"] = host["ticker"]
            continue
        kept_by_name[key].append(r)
        deduped.append(r)

    for i, r in enumerate(deduped, 1):
        r["rank"] = i                  # position among tradeable tickers – this is the reported rank
    for r in rows:
        r.setdefault("rank", None)
    top = deduped[:TOP_N]

    fingerprint_src = json.dumps(
        {"aggregated": aggregated, "mq": mq_signals,
         "insider": {t: (v.get("summary"), v.get("score")) for t, v in sorted(insider_by_ticker.items())},
         "weights": SIGNAL_WEIGHTS, "date": today.isoformat()},
        sort_keys=True, default=str,
    )
    return {
        "date":               today.isoformat(),
        "top10":              top,
        "ranking":            rows,
        "weights":            SIGNAL_WEIGHTS,
        "factor_labels":      FACTOR_LABELS,
        "input_fingerprint":  hashlib.sha256(fingerprint_src.encode()).hexdigest(),
        "ranking_fingerprint": hashlib.sha256(
            json.dumps([(r["ticker"], r["signal_score"]) for r in rows]).encode()).hexdigest(),
        "excluded_no_ticker": [r["ticker"] for r in rows if not r["tradable_ticker_validated"]][:20],
        "excluded_no_ticker_count": sum(1 for r in rows if not r["tradable_ticker_validated"]),
        "excluded_same_issuer": [
            {"ticker": r["ticker"], "superseded_by": r["superseded_by"]}
            for r in rows if r.get("superseded_by")
        ],
    }


# ── candidate pool for the (network-heavy) insider look-up ────────────────────

def candidate_pool(scores: dict, today: date) -> tuple[list[str], dict[str, str]]:
    """
    Pre-ranks every ticker with insider = 0 and returns the top
    SIGNAL_CANDIDATE_POOL tickers plus the Form 4 window start per ticker
    (latest 13F quarter-end among its buyers).
    """
    pre = compute_signals(scores, {}, today)
    pool = [r["ticker"] for r in pre["ranking"] if r["tradable_ticker_validated"]][:SIGNAL_CANDIDATE_POOL]

    since: dict[str, str] = {}
    for r in pre["ranking"]:
        if r["ticker"] not in pool:
            continue
        report_dates = [f["report_date"] for f in r["filers"] if f.get("report_date")]
        since[r["ticker"]] = max(report_dates) if report_dates else ""
    return pool, since


# ── main ──────────────────────────────────────────────────────────────────────

def load_scores(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_scores.json"
    if not path.exists():
        raise FileNotFoundError(f"Scores not found: {path}")
    return json.load(open(path))


def run(today_str: str | None = None, skip_insider: bool = False) -> dict:
    today_str = today_str or run_date()
    today = date.fromisoformat(today_str)
    print(f"\n{'='*60}\nSignal Engine – deterministic Top {TOP_N} – {today_str}\n{'='*60}")

    scores = load_scores(today_str)
    pool, since = candidate_pool(scores, today)
    print(f"🎯 Candidate pool for Form 4 look-up: {len(pool)} tickers")

    insider_by_ticker: dict[str, dict] = {}
    if not skip_insider:
        import insider_activity
        insider_by_ticker = insider_activity.fetch_for_candidates(pool, since)
    else:
        print("  (insider look-up skipped)")

    result = compute_signals(scores, insider_by_ticker, today)

    print(f"\n{'Rank':<5}{'Ticker':<8}{'Score':<8}{'Grade':<13}{'Insider':<9}{'Buyers':<8}Top drivers")
    print("─" * 78)
    for r in result["top10"]:
        drivers = ", ".join(d["factor"] for d in r["why"]["top_drivers"])
        print(f"{r['rank']:<5}{r['ticker']:<8}{r['signal_score']:<8}{r['grade']:<13}"
              f"{r['factors']['insider']:<9}{r['filer_count']:<8}{drivers}")
    print(f"\n🔏 input fingerprint   {result['input_fingerprint'][:16]}…")
    print(f"🔏 ranking fingerprint {result['ranking_fingerprint'][:16]}…")

    out = DATA_DIR / f"{today_str}_signals.json"
    tmp = out.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True, default=str)
    tmp.replace(out)
    print(f"✅ Signals saved to {out}")
    return result


if __name__ == "__main__":
    args = sys.argv[1:]
    run(skip_insider="--skip-insider" in args)
