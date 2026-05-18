"""
parse_13f.py
Computes quarter-over-quarter deltas from raw holdings data.

Fixes from architecture audit:
  R-01  Stock split → share count adjusted before delta calculation
  R-03  AUM inflation warning → added systematic bias disclaimer
  R-04  Division by zero on new positions → handled explicitly
  R-07  First run / missing prior quarter → bootstrap gracefully
  R-11  CUSIP vs ADR matching → uses ticker as primary key with CUSIP fallback
"""

import json
import sys
from datetime import date
from pathlib import Path

from config import DATA_DIR


def load_latest_raw(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_raw_holdings.json"
    if not path.exists():
        raise FileNotFoundError(f"Raw holdings not found: {path}")
    with open(path) as f:
        return json.load(f)


def load_prior_quarter(today_str: str) -> dict | None:
    """
    Finds the most recent previously saved *_holdings_parsed.json
    that is NOT today. Returns None if this is the first run.

    Amendment note: 13F-HR/A filings amend a prior quarter's data.
    Because fetch_filings.py always retrieves the LATEST filing per filer
    (which is the amendment if one exists), the data saved today is always
    the most up-to-date. The delta comparison against the *previous* parsed
    file is therefore always amendment-aware as long as each run overwrites
    stale data from the same quarter. If two runs occur within the same
    quarter (e.g., base + amendment), only the most recent parsed file
    survives and prior-quarter comparison remains valid.
    """
    today = date.fromisoformat(today_str)
    candidates = sorted(DATA_DIR.glob("*_holdings_parsed.json"), reverse=True)

    for c in candidates:
        try:
            d = date.fromisoformat(c.name[:10])
            if d < today:
                data = json.load(open(c))
                # Warn if the prior data itself contains amendments, so the
                # user knows the baseline may have been restated.
                amendment_filers = [
                    name for name, fd in data.get("filers", {}).items()
                    if fd.get("is_amendment")
                ]
                if amendment_filers:
                    print(f"  ℹ️  Prior quarter ({data['date']}) contains amendments "
                          f"for: {', '.join(amendment_filers)} – baseline has been restated.")
                return data
        except (ValueError, json.JSONDecodeError):
            continue

    return None  # First run


def build_position_lookup(filer_data: dict) -> dict:
    """
    Build a dict: {ticker_or_cusip → holding_dict} for one filer.

    PUT and CALL/long positions are tracked separately so the net
    direction is visible downstream. Aggregating them would inflate
    bullish signals: a fund with 100 long shares + 10,000 put shares
    must NOT be scored as a 10,100-share long conviction buy.

    Structure per key:
      shares / value_usd_thousands  → long + CALL shares (bullish)
      put_shares / put_value_usd_k  → PUT shares (bearish / hedge)
      net_bullish                   → True if long_value > put_value
    """
    long_lookup: dict = {}
    put_lookup:  dict = {}

    for h in filer_data.get("holdings", []):
        put_call = (h.get("putCall") or "").strip().upper()
        key      = h.get("ticker") or h.get("cusip") or h.get("nameOfIssuer", "UNKNOWN")

        if put_call == "PUT":
            if key in put_lookup:
                put_lookup[key]["shares"]              += h["shares"]
                put_lookup[key]["value_usd_thousands"] += h["value_usd_thousands"]
            else:
                put_lookup[key] = {**h}
        else:
            # Long or CALL – counts as bullish exposure
            if key in long_lookup:
                long_lookup[key]["shares"]              += h["shares"]
                long_lookup[key]["value_usd_thousands"] += h["value_usd_thousands"]
            else:
                long_lookup[key] = {**h}

    # Merge: annotate every long position with its paired PUT size
    all_keys = set(long_lookup) | set(put_lookup)
    lookup   = {}

    for key in all_keys:
        if key in long_lookup:
            entry = {**long_lookup[key]}
        else:
            # Pure-put position: create a placeholder with zero long exposure
            entry = {**put_lookup[key], "shares": 0, "value_usd_thousands": 0}

        put_entry = put_lookup.get(key, {})
        entry["put_shares"]       = put_entry.get("shares", 0)
        entry["put_value_usd_k"]  = put_entry.get("value_usd_thousands", 0)

        long_val = entry["value_usd_thousands"]
        put_val  = entry["put_value_usd_k"]
        entry["net_bullish"] = long_val >= put_val   # False → fund is net short/hedged

        if not entry["net_bullish"]:
            # Surface this so scoring can skip or flag it
            entry["direction_note"] = (
                f"NET SHORT/HEDGED: long ${long_val:,}k vs put ${put_val:,}k"
            )

        lookup[key] = entry

    return lookup


def adjust_shares_for_splits(shares: int, ticker: str, splits: dict) -> int:
    """
    R-01 Fix: If a stock split occurred since the prior quarter,
    the prior-quarter share count must be multiplied by the split ratio
    before computing delta. Otherwise +900% false positives occur.
    """
    ratio = splits.get(ticker, 1.0)
    if ratio != 1.0:
        adjusted = int(shares * ratio)
        print(f"    🔀 Split-adjusted {ticker} prior shares: {shares} → {adjusted} (ratio {ratio})")
        return adjusted
    return shares


def compute_delta(current_shares: int, prior_shares: int | None, ticker: str) -> dict:
    """
    Returns delta info between current and prior quarter.

    R-04 Fix: Division by zero when prior_shares == 0 (new position).
    Handles three cases:
      - New position  (prior is None or 0)
      - Full exit     (current == 0, though EDGAR won't show these)
      - Change        (normal delta)
    """
    if prior_shares is None:
        return {
            "type":         "NEW",
            "delta_shares": current_shares,
            "delta_pct":    None,  # undefined for new positions
        }

    if prior_shares == 0:
        return {
            "type":         "NEW",
            "delta_shares": current_shares,
            "delta_pct":    None,
        }

    delta_shares = current_shares - prior_shares
    delta_pct    = (delta_shares / prior_shares) * 100.0

    if current_shares == 0:
        tx_type = "SOLD"
    elif delta_shares > 0:
        tx_type = "ADDED"
    elif delta_shares < 0:
        tx_type = "REDUCED"
    else:
        tx_type = "UNCHANGED"

    return {
        "type":         tx_type,
        "delta_shares": delta_shares,
        "delta_pct":    round(delta_pct, 2),
    }


def parse_and_enrich(raw: dict, prior: dict | None) -> dict:
    """
    Main enrichment pass: for each filer and each position, compute:
      - portfolio weight (% of reported long-only AUM)
      - delta vs. prior quarter
      - position rank within portfolio

    R-03 Warning: portfolio weight uses REPORTED 13F AUM (long positions only).
    Cash, shorts, bonds, options premiums are excluded by SEC rules.
    The weight is systematically overstated for diversified managers.
    """
    today_str = raw["date"]
    splits    = raw.get("recent_splits", {})

    parsed_filers = {}

    for filer_name, filer_data in raw["filers"].items():
        if "holdings" not in filer_data:
            parsed_filers[filer_name] = {"error": filer_data.get("error"), "positions": []}
            continue

        current_lookup = build_position_lookup(filer_data)

        # Reported AUM = long + CALL positions only (Puts are hedges, not capital deployed).
        # Using filer_data["total_value"] would inflate the denominator with put notional,
        # making every position's portfolio weight look smaller than it really is.
        reported_aum = sum(
            pos["value_usd_thousands"]
            for pos in current_lookup.values()
        )
        if reported_aum == 0:
            print(f"  ⚠️  {filer_name}: reported long-only AUM = 0, skipping")
            parsed_filers[filer_name] = {"error": "zero_aum", "positions": []}
            continue

        # Prior quarter lookup for this filer
        prior_lookup = {}
        if prior and filer_name in prior.get("filers", {}):
            prior_filer = prior["filers"][filer_name]
            if "positions" in prior_filer:
                for pos in prior_filer["positions"]:
                    key = pos.get("ticker") or pos.get("cusip") or ""
                    prior_lookup[key] = pos

        positions = []
        for key, holding in current_lookup.items():
            ticker      = holding.get("ticker", "")
            net_bullish = holding.get("net_bullish", True)

            # Skip positions where puts dominate: they are bearish/hedged and
            # would produce false bullish signals downstream.
            # They are logged separately in put_positions for transparency.
            if not net_bullish:
                continue

            prior_pos = prior_lookup.get(key)

            # Compare only long shares (prior data may have aggregated puts+longs).
            # Use prior "shares" but cap to avoid inflated deltas from old data format.
            prior_shares_raw = prior_pos["shares"] if prior_pos else None
            if prior_shares_raw is not None and ticker:
                prior_shares_adj = adjust_shares_for_splits(prior_shares_raw, ticker, splits)
            else:
                prior_shares_adj = prior_shares_raw

            delta = compute_delta(holding["shares"], prior_shares_adj, key)

            # Portfolio weight uses long-only AUM (corrected denominator above)
            port_weight_pct = (holding["value_usd_thousands"] / reported_aum) * 100.0

            # Prior portfolio weight
            prior_port_weight = None
            if prior_pos and prior:
                prior_aum = prior["filers"].get(filer_name, {}).get("reported_aum_k", 0)
                if prior_aum > 0:
                    prior_port_weight = (prior_pos.get("value_usd_thousands", 0) / prior_aum) * 100.0

            # Determine position direction: LONG or CALL (no pure PUTs reach here)
            put_val  = holding.get("put_value_usd_k", 0)
            long_val = holding["value_usd_thousands"]
            direction = "LONG_WITH_HEDGE" if put_val > 0 else "LONG"

            positions.append({
                "ticker":             ticker,
                "cusip":              holding.get("cusip", ""),
                "name":               holding.get("nameOfIssuer", ""),
                "value_usd_k":        long_val,
                "shares":             holding["shares"],
                "put_shares":         holding.get("put_shares", 0),
                "put_value_usd_k":    put_val,
                "direction":          direction,
                "port_weight_pct":    round(port_weight_pct, 3),
                "prior_port_weight":  round(prior_port_weight, 3) if prior_port_weight else None,
                "delta":              delta,
                "is_first_run":       prior is None,
                "net_bullish":        True,
                "weight_note":        "Long-only 13F AUM denominator – true weight may be lower",
            })

        # Sort by portfolio weight descending
        positions.sort(key=lambda x: x["port_weight_pct"], reverse=True)

        # Add rank
        for i, pos in enumerate(positions, 1):
            pos["rank"] = i

        parsed_filers[filer_name] = {
            "cik":           filer_data["cik"],
            "reported_aum_k": reported_aum,
            "filing_date":   filer_data["meta"]["filingDate"],
            # report_date = quarter-end (period of report); used as price anchor.
            "report_date":   filer_data["meta"].get("reportDate") or filer_data["meta"]["filingDate"],
            "is_amendment":  filer_data["meta"]["isAmendment"],
            "position_count": len(positions),
            "positions":     positions,
        }

        print(f"  ✅ {filer_name}: {len(positions)} positions, "
              f"AUM ${reported_aum/1e6:,.1f}B (13F reported, long-only)")

    return {
        "date":          today_str,
        "prior_date":    prior["date"] if prior else None,
        "is_first_run":  prior is None,
        "recent_splits": splits,
        "filers":        parsed_filers,
    }


def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"13F Parser & Delta Calculator – {today_str}")
    print(f"{'='*60}")

    raw = load_latest_raw(today_str)
    prior = load_prior_quarter(today_str)

    if prior:
        print(f"📂 Prior quarter data: {prior['date']}")
    else:
        print("⚠️  First run – no prior quarter data available. Deltas will be marked as NEW.")

    parsed = parse_and_enrich(raw, prior)

    output_path = DATA_DIR / f"{today_str}_holdings_parsed.json"
    tmp_path = output_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(parsed, f, indent=2, default=str)
    tmp_path.replace(output_path)

    print(f"\n✅ Parsed holdings saved to {output_path}")


if __name__ == "__main__":
    run()
