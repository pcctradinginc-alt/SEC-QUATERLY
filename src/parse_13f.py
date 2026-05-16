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
    """
    today = date.fromisoformat(today_str)
    candidates = sorted(DATA_DIR.glob("*_holdings_parsed.json"), reverse=True)

    for c in candidates:
        try:
            d = date.fromisoformat(c.name[:10])
            if d < today:
                with open(c) as f:
                    return json.load(f)
        except (ValueError, json.JSONDecodeError):
            continue

    return None  # First run


def build_position_lookup(filer_data: dict) -> dict:
    """
    Build a dict: {ticker_or_cusip → holding_dict} for one filer.
    Ticker is preferred; CUSIP fallback for unmapped positions.
    This resolves R-11 (ADR vs. ordinary share CUSIP mismatch).
    """
    lookup = {}
    for h in filer_data.get("holdings", []):
        key = h.get("ticker") or h.get("cusip") or h.get("nameOfIssuer", "UNKNOWN")
        # Aggregate if the same ticker appears twice (e.g., put and call entries)
        if key in lookup:
            lookup[key]["shares"]              += h["shares"]
            lookup[key]["value_usd_thousands"] += h["value_usd_thousands"]
        else:
            lookup[key] = {**h}
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

        # Reported AUM = sum of all 13F long positions (NOT true AUM – see R-03)
        reported_aum = filer_data["total_value"]  # in USD thousands
        if reported_aum == 0:
            print(f"  ⚠️  {filer_name}: reported AUM = 0, skipping")
            parsed_filers[filer_name] = {"error": "zero_aum", "positions": []}
            continue

        current_lookup = build_position_lookup(filer_data)

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
            ticker   = holding.get("ticker", "")
            prior_pos = prior_lookup.get(key)

            # Split adjustment on prior shares
            prior_shares_raw = prior_pos["shares"] if prior_pos else None
            if prior_shares_raw is not None and ticker:
                prior_shares_adj = adjust_shares_for_splits(prior_shares_raw, ticker, splits)
            else:
                prior_shares_adj = prior_shares_raw

            delta = compute_delta(holding["shares"], prior_shares_adj, key)

            # Portfolio weight (R-03: only long-only AUM denominator)
            port_weight_pct = (holding["value_usd_thousands"] / reported_aum) * 100.0

            # Prior portfolio weight
            prior_port_weight = None
            if prior_pos and prior:
                prior_aum = prior["filers"].get(filer_name, {}).get("reported_aum_k", 0)
                if prior_aum > 0:
                    prior_port_weight = (prior_pos.get("value_usd_thousands", 0) / prior_aum) * 100.0

            positions.append({
                "ticker":             ticker,
                "cusip":              holding.get("cusip", ""),
                "name":               holding.get("nameOfIssuer", ""),
                "value_usd_k":        holding["value_usd_thousands"],
                "shares":             holding["shares"],
                "putCall":            holding.get("putCall"),
                "port_weight_pct":    round(port_weight_pct, 3),
                "prior_port_weight":  round(prior_port_weight, 3) if prior_port_weight else None,
                "delta":              delta,
                "is_first_run":       prior is None,
                # R-03 disclaimer embedded per position
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
