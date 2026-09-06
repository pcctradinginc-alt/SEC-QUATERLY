"""
scoring.py
13F Alpha Score engine (spec Sections 1-17).

Component-based, additive Alpha Score (Section 15):
  25% Active Weight (proxied by portfolio-weight percentile - no benchmark
       index wired up yet, see Tier-3 gap in README)
  20% Position Change (share-count delta magnitude, percentile)
  15% Manager Quality (dynamic 0-1 score, see manager_quality.py)
  15% Smart-Money Consensus (quality-weighted multi-fund buying)
  10% Multi-Quarter Accumulation (recency-weighted, see multi_quarter.py)
  10% Freshness (exp(-k * turnover * filing_delay), see manager_quality.py)
   5% Abnormal Institutional Ownership (Tier-3 gap - always 0, kept explicit)
  -  Crowding Penalty (proxy: hotel-ticker list + oversized same-run cluster)
  -  Price-action staleness penalty (stock already ran hard since filing)

Sell-side signals (REDUCE/EXIT, Section 14) are scored separately and never
mixed into the buy-side Top 20 that feeds Claude Round 1.
"""

import json
import math
import re
from collections import defaultdict
from datetime import date

from config import (
    run_date,
    ALPHA_WEIGHTS, CLUSTER_MIN_FUNDS, CROWDING_HOTEL_PENALTY,
    CROWDING_HOTEL_TICKERS, CROWDING_LABEL_BANDS,
    CROWDING_PENALTY_PER_FUND_OVER_MAX, DATA_DIR, DOUBLE_DOWN_MIN_DELTA,
    EARLY_SMART_MONEY_MAX_BUILD_QUARTERS, EARLY_SMART_MONEY_MAX_FUNDS,
    EARLY_SMART_MONEY_MIN_AVG_QUALITY, EARLY_SMART_MONEY_MIN_FUNDS,
    FRESHNESS_K, FRESHNESS_QUARTER_DAYS, HIGH_CONVICTION_MIN_PCT,
    MIN_PORTFOLIO_WEIGHT_PCT, NEW_POSITION_BANDS, PRICE_ACTION_DOWNGRADE_PCT,
    PRICE_ACTION_STALE_PENALTY, PRICE_ACTION_WARN_PCT,
    PRICE_ACTION_WARN_PENALTY, RELATIVE_OUTSIZED_VS_MEDIAN,
)
import manager_quality as manager_quality_mod
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
    today_str = run_date()

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
            days_since    = (date.fromisoformat(run_date()) - filing_date).days

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


def enrich_with_price_action(scored: list[dict]) -> list[dict]:
    """
    Checks current price vs price at filing date for each ticker.
    If the stock has already run >25% since the 13F filing, the
    thesis may have played out - a penalty is applied to the Alpha Score
    later (see compute_alpha_score) and a STALE flag is added.
    """
    filing_dates: dict[str, str] = {}
    for entry in scored:
        t = entry["ticker"]
        if t and t not in filing_dates:
            filing_dates[t] = entry.get("filing_date", "")

    # Only real, tradable symbols go to the price feed. An unmapped CUSIP
    # ("82452JAD1") is not a ticker; sending it produced a hundred failed
    # downloads per run and no price action either way.
    tickers = [t for t in filing_dates if t and len(t) <= 6 and t[:1].isalpha()]
    skipped = len(filing_dates) - len(tickers)
    if skipped:
        print(f"  ⏭️  {skipped} unmapped CUSIP keys skipped for price action (no tradable ticker)")
    if not tickers:
        return scored

    print(f"  📈 Checking price action for {len(tickers)} tickers since filing date...")
    changes = fetch_price_changes(tickers, filing_dates)

    warned = staled = 0
    for entry in scored:
        t    = entry["ticker"]
        perf = changes.get(t, {})
        pct  = perf.get("pct_change")

        entry["post_filing_perf"] = perf
        entry["price_action_penalty"] = 0.0

        if pct is None:
            continue

        if pct >= PRICE_ACTION_DOWNGRADE_PCT:
            entry["price_action_penalty"] = PRICE_ACTION_STALE_PENALTY
            if "PRICE_ACTION_STALE" not in entry["flags"]:
                entry["flags"].append("PRICE_ACTION_STALE")
            staled += 1
        elif pct >= PRICE_ACTION_WARN_PCT:
            entry["price_action_penalty"] = PRICE_ACTION_WARN_PENALTY
            if "PRICE_ACTION_WARNING" not in entry["flags"]:
                entry["flags"].append("PRICE_ACTION_WARNING")
            warned += 1

    if staled or warned:
        print(f"  ⚠️  Price action: {staled} positions stale (>{PRICE_ACTION_DOWNGRADE_PCT}%), "
              f"{warned} warnings (>{PRICE_ACTION_WARN_PCT}%)")
    return scored


# ── Freshness (Section 8) ──────────────────────────────────────────────────────

def compute_freshness_scores(parsed: dict, manager_quality: dict[str, dict]) -> dict[str, dict]:
    """
    Per-filer Freshness = exp(-FRESHNESS_K * turnover_fraction * filing_delay_fraction).

    High turnover + long filing delay = steep discount, because the reported
    positions have likely already changed by the time anyone can read the filing.
    A patient, low-turnover manager's filing stays informative for much longer.
    """
    freshness: dict[str, dict] = {}

    for filer_name, filer_data in parsed["filers"].items():
        if "positions" not in filer_data:
            continue

        delay_days = filer_data.get("filing_delay_days")
        if delay_days is None:
            delay_days = 45  # SEC's typical max lag, conservative fallback

        mq = manager_quality.get(filer_name, {})
        turnover_pct = mq.get("avg_turnover_pct")
        if turnover_pct is None:
            # No turnover history yet - fall back to the concentration signal
            # as a rough proxy (fewer positions tends to mean lower turnover).
            turnover_pct = (1.0 - mq.get("concentration_score", 0.5)) * 50.0

        turnover_fraction = max(0.0, min(1.0, turnover_pct / 100.0))
        delay_fraction    = max(0.0, delay_days) / FRESHNESS_QUARTER_DAYS

        score = math.exp(-FRESHNESS_K * turnover_fraction * delay_fraction)

        freshness[filer_name] = {
            "freshness_score":    round(score, 3),
            "filing_delay_days":  delay_days,
            "turnover_pct_used":  round(turnover_pct, 1),
        }

    return freshness


# ── Position size classification (Section 3) ──────────────────────────────────

def classify_position_strength(port_pct: float) -> str:
    label = NEW_POSITION_BANDS[0][1]
    for lower, lbl in NEW_POSITION_BANDS:
        if port_pct >= lower:
            label = lbl
    return label


# ── Core scoring: buy-side universe ────────────────────────────────────────────

def apply_flags(entry: dict) -> list[str]:
    flags = []
    tier   = entry.get("position_tier")
    is_new = entry["delta_type"] == "NEW"

    if is_new:
        flags.append("NEW_POSITION")
        flags.append(f"SIZE_{classify_position_strength(entry['port_weight_pct'])}")
        if entry["port_weight_pct"] >= HIGH_CONVICTION_MIN_PCT:
            flags.append("HIGH_CONVICTION")
        if tier == "TOP10":
            flags.append("TOP10_ENTRY")

    if tier == "TOP3":
        flags.append("TOP3_POSITION")
    elif tier == "TOP5":
        flags.append("TOP5_POSITION")

    wvm = entry.get("weight_vs_median")
    if wvm is not None and wvm >= RELATIVE_OUTSIZED_VS_MEDIAN:
        flags.append("OUTSIZED_VS_MANAGER_TYPICAL")

    if entry["delta_type"] == "ADDED" and entry["delta_pct"] is not None \
            and entry["delta_pct"] >= DOUBLE_DOWN_MIN_DELTA:
        flags.append("AGGRESSIVE_ADD")

    put_val  = entry.get("put_value_usd_k", 0)
    long_val = entry.get("value_usd_k", 0)
    if put_val > 0 and long_val > 0 and put_val > long_val * 0.5:
        flags.append("PUT_HEDGE_PRESENT")

    return flags


def build_scored_universe(
    parsed: dict,
    manager_quality: dict[str, dict],
    freshness_by_filer: dict[str, dict],
) -> list[dict]:
    """
    Iterates all filers and positions, collecting buy-side (NEW/ADDED) rows
    with the raw inputs each Alpha Score component needs. Normalization
    across the universe happens afterward in finalize_alpha_scores().
    """
    scored = []

    for filer_name, filer_data in parsed["filers"].items():
        if "positions" not in filer_data:
            continue

        mq_info         = manager_quality.get(filer_name, {})
        quality_score   = mq_info.get("quality_score", 0.5)
        fresh_info      = freshness_by_filer.get(filer_name, {})
        freshness_score = fresh_info.get("freshness_score", 0.5)
        # report_date = quarter-end (price anchor + Form 4 window start);
        # filing_date_actual = the day the 13F hit EDGAR (freshness age).
        filing_date     = filer_data.get("report_date") or filer_data.get("filing_date", "")
        filing_date_actual = filer_data.get("filing_date", "")

        for pos in filer_data["positions"]:
            tx_type = pos["delta"]["type"]
            if tx_type not in ("NEW", "ADDED"):
                continue
            if pos["port_weight_pct"] < MIN_PORTFOLIO_WEIGHT_PCT:
                continue
            # Net short/hedged via puts - not a real conviction buy (Section 17).
            if not pos.get("net_bullish", True):
                continue
            # Section 18: don't treat a likely merger/spin-off/share-class
            # swap as a genuine new buy decision.
            if pos.get("possible_corporate_action"):
                continue

            delta_pct = pos["delta"].get("delta_pct")
            position_change_raw = 100.0 if delta_pct is None else abs(delta_pct)
            ticker = pos.get("ticker", "") or pos.get("cusip", "")

            entry = {
                "filer":                  filer_name,
                "ticker":                 ticker,
                "cusip":                  pos.get("cusip", ""),
                "name":                   pos.get("name", ""),
                "port_weight_pct":        pos["port_weight_pct"],
                "weight_vs_median":       pos.get("weight_vs_median"),
                "position_tier":          pos.get("position_tier"),
                "delta_pct":              delta_pct,
                "delta_type":             tx_type,
                "delta_shares":           pos["delta"]["delta_shares"],
                "value_usd_k":            pos["value_usd_k"],
                "put_value_usd_k":        pos.get("put_value_usd_k", 0),
                "rank_in_port":           pos.get("rank"),
                "filing_date":            filing_date,          # = report_date (quarter-end), legacy name
                "report_date":            filing_date,
                "filing_date_actual":     filing_date_actual,
                "position_change_raw":    position_change_raw,
                "manager_quality_score":  quality_score,
                "freshness_score":        freshness_score,
                "filing_delay_days":      fresh_info.get("filing_delay_days"),
                "flags":                  [],
            }
            entry["flags"] = apply_flags(entry)
            scored.append(entry)

    return scored


# ── Consensus (Section 9) & buyer counting ────────────────────────────────────

def count_buyers_per_ticker(scored: list[dict]) -> dict[str, list[str]]:
    """All filers buying (NEW/ADDED) each ticker this run - unfiltered by cluster threshold."""
    buyers: dict[str, list[str]] = defaultdict(list)
    for e in scored:
        if e["ticker"]:
            buyers[e["ticker"]].append(e["filer"])
    return dict(buyers)


def compute_consensus_raw(scored: list[dict]) -> dict[str, float]:
    """
    Section 9: Consensus = Sum(Manager Quality x Conviction x Position Change x Freshness)
    over buying filers, per ticker. Unnormalized - min-max normalized later
    alongside the other Alpha Score components.
    """
    raw: dict[str, float] = defaultdict(float)
    for e in scored:
        conviction      = e["port_weight_pct"] / 100.0
        position_change = min(e["position_change_raw"] / 100.0, 3.0)  # cap extreme deltas
        raw[e["ticker"]] += (
            e["manager_quality_score"] * conviction * position_change * e["freshness_score"]
        )
    return dict(raw)


# ── Crowding (Section 13, Tier-2 proxy) ───────────────────────────────────────

def compute_crowding(ticker: str, buyer_count: int) -> dict:
    """
    NOT real market-wide institutional ownership data (that needs a data
    source beyond the tracked filers in config.FILERS - see README Tier-3
    gap). Approximates crowding from (a) a static "hedge fund hotel" mega-cap
    list and (b) how many of our own tracked funds are already piling into
    the same name.
    """
    penalty = 0.0
    if ticker in CROWDING_HOTEL_TICKERS:
        penalty += CROWDING_HOTEL_PENALTY
    if buyer_count > EARLY_SMART_MONEY_MAX_FUNDS:
        penalty += (buyer_count - EARLY_SMART_MONEY_MAX_FUNDS) * CROWDING_PENALTY_PER_FUND_OVER_MAX

    penalty = min(penalty, 100.0)
    label = next(lbl for max_score, lbl in CROWDING_LABEL_BANDS if penalty <= max_score)
    return {"crowding_penalty": round(penalty, 1), "crowding_label": label}


# ── Alpha Score assembly (Section 15) ─────────────────────────────────────────

def _minmax(values: list[float]) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    return (min(values), max(values))


def _norm(value: float, lo: float, hi: float) -> float:
    """Min-max to [0,100]. When every value in the universe is identical,
    there is no relative signal to extract - return neutral (50), not 0."""
    if hi == lo:
        return 50.0
    return (value - lo) / (hi - lo) * 100.0


def finalize_alpha_scores(
    scored: list[dict],
    mq_signals: dict[str, dict],
) -> tuple[list[dict], dict[str, list[str]]]:
    """
    Normalizes the universe-relative components (active weight, position
    change, consensus, accumulation) and assembles the weighted Alpha Score
    per Section 15, minus crowding and price-action penalties.

    Returns (scored, all_buyers_per_ticker).
    """
    if not scored:
        return scored, {}

    aw_lo, aw_hi = _minmax([e["port_weight_pct"] for e in scored])
    pc_lo, pc_hi = _minmax([e["position_change_raw"] for e in scored])

    all_buyers = count_buyers_per_ticker(scored)
    clusters   = {t: f for t, f in all_buyers.items() if len(f) >= CLUSTER_MIN_FUNDS}

    consensus_raw       = compute_consensus_raw(scored)
    cons_lo, cons_hi     = _minmax(list(consensus_raw.values()))

    tickers_in_play      = {e["ticker"] for e in scored if e["ticker"]}
    accumulation_raw     = {
        t: mq_signals.get(t, {}).get("accumulation_score", 0.0) for t in tickers_in_play
    }
    acc_lo, acc_hi       = _minmax(list(accumulation_raw.values()))

    for e in scored:
        t = e["ticker"]

        active_weight_component   = _norm(e["port_weight_pct"], aw_lo, aw_hi)
        position_change_component = _norm(e["position_change_raw"], pc_lo, pc_hi)
        consensus_component       = _norm(consensus_raw.get(t, 0.0), cons_lo, cons_hi)
        accumulation_component    = _norm(accumulation_raw.get(t, 0.0), acc_lo, acc_hi)
        manager_quality_component = e["manager_quality_score"] * 100.0
        freshness_component       = e["freshness_score"] * 100.0
        abnormal_ownership_component = 0.0  # Tier-3 gap - no data source yet

        components = {
            "active_weight":      round(active_weight_component, 1),
            "position_change":    round(position_change_component, 1),
            "manager_quality":    round(manager_quality_component, 1),
            "consensus":          round(consensus_component, 1),
            "accumulation":       round(accumulation_component, 1),
            "freshness":          round(freshness_component, 1),
            "abnormal_ownership": round(abnormal_ownership_component, 1),
        }

        buyer_count = len(all_buyers.get(t, []))
        crowd = compute_crowding(t, buyer_count)

        weighted = sum(ALPHA_WEIGHTS[k] * v for k, v in components.items())
        price_penalty = e.get("price_action_penalty", 0.0)
        alpha_score = max(0.0, min(100.0, weighted - crowd["crowding_penalty"] - price_penalty))

        e["components"]        = components
        e["crowding_penalty"]  = crowd["crowding_penalty"]
        e["crowding_label"]    = crowd["crowding_label"]
        e["alpha_score"]       = round(alpha_score, 1)
        e["conviction_score"]  = e["alpha_score"]  # alias for backward-compat callers

        if t in clusters:
            e["cluster_funds"] = clusters[t]
            e["cluster_count"] = len(clusters[t])
            if "CLUSTER" not in e["flags"]:
                e["flags"].append("CLUSTER")
        else:
            e["cluster_funds"] = []
            e["cluster_count"] = buyer_count

    return scored, all_buyers


# ── Early Smart Money Accumulation (Section 10) ───────────────────────────────

def flag_early_smart_money(
    aggregated: list[dict],
    all_buyers: dict[str, list[str]],
    manager_quality: dict[str, dict],
    mq_signals: dict[str, dict],
) -> list[dict]:
    """
    Section 10: 2-6 high-quality managers buying/building the same name,
    still early (few build quarters visible), not yet broadly crowded.
    This is flagged as the single most-preferred setup in the spec.
    """
    for agg in aggregated:
        ticker  = agg["ticker"]
        buyers  = all_buyers.get(ticker, [])
        count   = len(buyers)
        agg["early_smart_money"] = False

        if not (EARLY_SMART_MONEY_MIN_FUNDS <= count <= EARLY_SMART_MONEY_MAX_FUNDS):
            continue

        qualities    = [manager_quality.get(f, {}).get("quality_score", 0.5) for f in buyers]
        avg_quality  = sum(qualities) / len(qualities) if qualities else 0.0
        build_quarters = mq_signals.get(ticker, {}).get("build_quarters", 1)

        if avg_quality >= EARLY_SMART_MONEY_MIN_AVG_QUALITY \
                and build_quarters <= EARLY_SMART_MONEY_MAX_BUILD_QUARTERS \
                and agg.get("crowding_label") in ("LOW", "MODERATE"):
            agg["early_smart_money"] = True
            agg["flags"] = sorted(set(agg["flags"]) | {"EARLY_SMART_MONEY_ACCUMULATION"})

    return aggregated


# ── Aggregation ────────────────────────────────────────────────────────────────

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
                "alpha_score":       0.0,
                "total_value_usd_k": 0,
                "flags":             set(),
                "cluster_count":     entry["cluster_count"],
                "cluster_funds":     entry["cluster_funds"],
                "crowding_penalty":  entry["crowding_penalty"],
                "crowding_label":    entry["crowding_label"],
                "delta_types":       [],
                "post_filing_perf":  entry.get("post_filing_perf", {}),
                "mq_signal":         entry.get("mq_signal", {}),
                "best_components":   entry["components"],
            }

        agg = by_ticker[ticker]
        agg["filers"].append({
            "filer":                  entry["filer"],
            "port_weight_pct":        entry["port_weight_pct"],
            "delta_pct":              entry["delta_pct"],
            "delta_type":             entry["delta_type"],
            "alpha_score":            entry["alpha_score"],
            "manager_quality_score":  entry["manager_quality_score"],
            "freshness_score":        entry["freshness_score"],
            "weight_vs_median":       entry.get("weight_vs_median"),
            "position_tier":          entry.get("position_tier"),
            "report_date":            entry.get("report_date"),
            "filing_date_actual":     entry.get("filing_date_actual"),
        })
        if entry["alpha_score"] >= agg["alpha_score"]:
            agg["alpha_score"]     = entry["alpha_score"]
            agg["best_components"] = entry["components"]
        agg["total_value_usd_k"] += entry["value_usd_k"]
        agg["flags"].update(entry["flags"])
        agg["delta_types"].append(entry["delta_type"])

    result = []
    for ticker, agg in by_ticker.items():
        agg["flags"]            = sorted(agg["flags"])
        agg["filer_count"]      = len(agg["filers"])
        agg["conviction_score"] = agg["alpha_score"]  # backward-compat alias
        result.append(agg)

    result.sort(key=lambda x: x["alpha_score"], reverse=True)
    return result


# ── Sell-side signals (Section 14) ────────────────────────────────────────────

def build_sell_signals(parsed: dict, manager_quality: dict[str, dict]) -> list[dict]:
    """
    Section 14: REDUCE/EXIT are negative signals in their own right, not just
    discarded rows. Not merged into the buy-side Top 20 / Claude prompt -
    surfaced separately (report: "Notable Exits & Reductions").

    Small reductions are deliberately NOT overweighted (spec explicit ask):
    only cuts of REDUCE_SIGNAL_MIN_DELTA_PCT or worse are included.
    """
    REDUCE_SIGNAL_MIN_DELTA_PCT = -20.0
    signals: list[dict] = []

    for filer_name, filer_data in parsed["filers"].items():
        if "positions" not in filer_data:
            continue
        quality = manager_quality.get(filer_name, {}).get("quality_score", 0.5)

        for pos in filer_data["positions"]:
            if pos["delta"]["type"] != "REDUCED" or pos.get("possible_corporate_action"):
                continue
            delta_pct = pos["delta"].get("delta_pct")
            if delta_pct is None or delta_pct > REDUCE_SIGNAL_MIN_DELTA_PCT:
                continue
            signals.append({
                "filer":                 filer_name,
                "ticker":                pos.get("ticker", "") or pos.get("cusip", ""),
                "name":                  pos.get("name", ""),
                "type":                  "REDUCE",
                "manager_quality_score": quality,
                "current_rank":          pos.get("rank"),
                "port_weight_pct":       pos["port_weight_pct"],
                "delta_pct":             delta_pct,
                "severity_score":        round(quality * abs(delta_pct) / 100.0, 3),
            })

        for exit_pos in filer_data.get("exited_positions", []):
            if exit_pos.get("possible_corporate_action"):
                continue
            prior_rank = exit_pos.get("prior_rank") or 999
            was_top5   = prior_rank <= 5
            signals.append({
                "filer":                 filer_name,
                "ticker":                exit_pos.get("ticker", "") or exit_pos.get("cusip", ""),
                "name":                  exit_pos.get("name", ""),
                "type":                  "EXIT",
                "manager_quality_score": quality,
                "prior_rank":            prior_rank,
                "prior_port_weight":     exit_pos.get("prior_port_weight"),
                "was_top5_position":     was_top5,
                "severity_score":        round(quality * (2.0 if was_top5 else 1.0), 3),
            })

    # Parallel selling: 2+ funds exiting/reducing the same name is a much
    # stronger negative signal than one fund trimming alone (Section 14).
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for s in signals:
        by_ticker[s["ticker"]].append(s)

    for ticker, group in by_ticker.items():
        parallel = len(group) >= 2
        for s in group:
            s["parallel_selling"] = parallel
            s["parallel_sellers"] = [g["filer"] for g in group if g["filer"] != s["filer"]] if parallel else []

    signals.sort(key=lambda s: s["severity_score"], reverse=True)
    return signals


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    today_str = run_date()

    print(f"\n{'='*60}")
    print(f"13F Alpha Score Engine – {today_str}")
    print(f"{'='*60}")

    parsed = load_parsed(today_str)

    # 1. Dynamic Manager Quality (Section 7)
    print("\n🧮 Computing dynamic manager quality scores...")
    manager_quality = manager_quality_mod.build_manager_quality_scores(today_str, parsed)
    for name, mq in manager_quality.items():
        print(f"   {name}: quality={mq['quality_score']} "
              f"(concentration={mq['concentration_score']}, turnover={mq['avg_turnover_pct']}, "
              f"bootstrapped={mq['is_bootstrapped']})")

    # 2. Freshness per filer (Section 8)
    freshness_by_filer = compute_freshness_scores(parsed, manager_quality)

    # 3. Multi-quarter accumulation signals (Section 6)
    print("\n🔍 Analyzing multi-quarter position building...")
    mq_signals = multi_quarter.build_multi_quarter_signals(today_str)
    print(f"   {len(mq_signals)} tickers with 2+ build quarters")

    # 4. Buy-side universe
    scored = build_scored_universe(parsed, manager_quality, freshness_by_filer)
    print(f"\n📊 Scored buy-side positions (≥{MIN_PORTFOLIO_WEIGHT_PCT}% port weight): {len(scored)}")

    # 5. Price-action staleness check
    scored = enrich_with_price_action(scored)

    # 6. Assemble Alpha Score (components, consensus, crowding)
    scored, all_buyers = finalize_alpha_scores(scored, mq_signals)

    # 7. Aggregate by ticker
    aggregated = aggregate_by_ticker(scored)

    # 8. Early Smart Money Accumulation (Section 10)
    aggregated = flag_early_smart_money(aggregated, all_buyers, manager_quality, mq_signals)

    clusters = {t: f for t, f in all_buyers.items() if len(f) >= CLUSTER_MIN_FUNDS}
    print(f"🔗 Cluster signals (≥{CLUSTER_MIN_FUNDS} funds): {len(clusters)} tickers")
    early_count = sum(1 for a in aggregated if a.get("early_smart_money"))
    print(f"🌱 Early Smart Money Accumulation: {early_count} tickers")

    # 9. Sell-side signals (Section 14) - separate from the buy universe
    sell_signals = build_sell_signals(parsed, manager_quality)
    print(f"📉 Sell-side signals (REDUCE ≥20% / EXIT): {len(sell_signals)}")

    print(f"\n{'─'*60}")
    print(f"{'Rank':<5}{'Ticker':<8}{'Score':<8}{'Filers':<8}{'Crowd':<10}{'Flags'}")
    print(f"{'─'*60}")
    # Unresolved CUSIPs stay in the book for AUM and weights, but they are not
    # tradable securities and must not be presented as ranked candidates.
    def _tradable(t: str) -> bool:
        return bool(t) and len(t) <= 6 and t[:1].isalpha()

    shown = [a for a in aggregated if _tradable(a["ticker"])][:20]
    hidden = sum(1 for a in aggregated if not _tradable(a["ticker"]))
    for i, agg in enumerate(shown, 1):
        print(f"{i:<5}{agg['ticker']:<8}{agg['alpha_score']:<8.1f}"
              f"{agg['filer_count']:<8}{agg['crowding_label']:<10}{', '.join(agg['flags'])}")
    if hidden:
        print(f"\n  ({hidden} holdings without a resolved tradable ticker are excluded "
              f"from the candidate ranking)")

    output = {
        "date":             today_str,
        "scored_flat":      scored,
        "aggregated":       aggregated,
        "clusters":         clusters,
        "top20":            aggregated[:20],
        "top40":            aggregated[:40],
        "mq_signals":       mq_signals,
        "manager_quality":  manager_quality,
        "sell_signals":     sell_signals,
    }

    output_path = DATA_DIR / f"{today_str}_scores.json"
    tmp_path    = output_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    tmp_path.replace(output_path)

    print(f"\n✅ Scores saved to {output_path}")


if __name__ == "__main__":
    run()
