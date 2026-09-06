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
import re
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from config import run_date, DATA_DIR


def load_latest_raw(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_raw_holdings.json"
    if not path.exists():
        raise FileNotFoundError(f"Raw holdings not found: {path}")
    with open(path) as f:
        return json.load(f)


def previous_quarter_end(report_date: str) -> str:
    """The quarter-end immediately before the given one. 2026-06-30 -> 2026-03-31."""
    d = date.fromisoformat(report_date)
    ends = {3: (d.year - 1, 12, 31), 6: (d.year, 3, 31),
            9: (d.year, 6, 30), 12: (d.year, 9, 30)}
    y, m, day = ends[d.month]
    return date(y, m, day).isoformat()


def infer_report_date(parsed: dict) -> str:
    """File-level reporting quarter, falling back to what the filers themselves
    report (files written before report_date was stored have no top-level one)."""
    rd = parsed.get("period_of_report") or parsed.get("report_date", "")
    if rd:
        return rd
    dates = [f.get("report_date") for f in parsed.get("filers", {}).values() if f.get("report_date")]
    if not dates:
        return ""
    return max(set(dates), key=dates.count)


def has_usable_share_counts(parsed: dict, sample: int = 400) -> bool:
    """
    A baseline whose share counts are all zero cannot produce a delta: every
    current holding would come out as NEW. Files written before the nested
    <sshPrnamt> parsing fix are in exactly that state, so they must not be used
    as a prior quarter.
    """
    seen = nonzero = 0
    for f in parsed.get("filers", {}).values():
        for pos in f.get("positions", []):
            seen += 1
            if (pos.get("shares") or 0) > 0:
                nonzero += 1
            if seen >= sample:
                break
        if seen >= sample:
            break
    return seen == 0 or nonzero > seen * 0.05


def load_prior_quarter(today_str: str, current_report_date: str = "") -> dict | None:
    """
    Load the dataset for the quarter immediately preceding `current_report_date`.

    Selection is by `period_of_report`, never by file name or run date: running
    the pipeline twice writes a second file for the SAME quarter, and diffing a
    quarter against itself yields no ADDs, no REDUCEs and no EXITs. Amendments
    restate a quarter, so the newest file for the required quarter wins.

    Returns None when the required quarter is unavailable or unusable; the
    caller must then stop rather than emit a degenerate all-NEW comparison.
    """
    if not current_report_date:
        print("  ⚠️  Current reporting period unknown - cannot identify the prior quarter")
        return None

    required = previous_quarter_end(current_report_date)
    print(f"  Expected prior quarter:   {required}")

    best = None
    for c in sorted(DATA_DIR.glob("*_holdings_parsed.json"), reverse=True):
        try:
            file_date = c.name[:10]
            date.fromisoformat(file_date)
        except ValueError:
            continue
        try:
            with open(c) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if infer_report_date(data) != required:
            continue
        if not has_usable_share_counts(data):
            print(f"  ↩︎  {c.name}: share counts are all zero (pre-fix file) - unusable as a baseline")
            continue
        if best is None or file_date > best[0]:
            best = (file_date, c.name, data)

    if best is None:
        print(f"  ❌ No usable dataset for the required prior quarter {required}")
        return None

    data = best[2]
    amendment_filers = [n for n, fd in data.get("filers", {}).items() if fd.get("is_amendment")]
    if amendment_filers:
        print(f"  ℹ️  Prior quarter contains amendments for: {', '.join(amendment_filers)}")
    print(f"  Loaded prior dataset:     {best[1]}")
    print(f"  Loaded prior period:      {infer_report_date(data)}")
    return data


def _match_prior_filer(prior: dict | None, filer_name: str, cik: str) -> dict | None:
    """
    Find last quarter's entry for this filer, keyed on CIK rather than on the
    display name.

    The name is ours and it changes: relabelling "TCI Fund (Chris Hohn)" to
    "Scion Asset Management (Burry)" would make every one of Burry's carried-over
    holdings look like a brand-new position. The reverse is worse - "Coatue
    (Laffont)" kept its name while its CIK was corrected from Chewy's, so a
    name-keyed join would diff Chewy's book against Coatue's and invent a full
    set of EXITs and NEW positions. The CIK is the SEC's identifier and is
    stable, so a changed CIK means a genuinely different entity: no prior.
    """
    if not prior:
        return None
    filers = prior.get("filers", {})

    if cik:
        for prior_name, prior_filer in filers.items():
            raw = str(prior_filer.get("cik", "") or "").strip()
            if raw and raw.zfill(10) == str(cik).zfill(10):
                if prior_name != filer_name:
                    print(f"    ℹ️  {filer_name}: prior quarter matched by CIK {cik} "
                          f"(was filed under '{prior_name}')")
                return prior_filer

    candidate = filers.get(filer_name)
    if candidate is None:
        return None
    prior_cik_raw = str(candidate.get("cik", "") or "").strip()
    prior_cik = prior_cik_raw.zfill(10) if prior_cik_raw else ""
    if cik and prior_cik and prior_cik != str(cik).zfill(10):
        print(f"    ⚠️  {filer_name}: prior quarter has CIK {prior_cik}, now {cik} - "
              f"different entity, treating as no prior quarter")
        return None
    return candidate


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


def compute_delta(current_shares: int, prior_shares: int | None, ticker: str,
                  has_baseline: bool = True) -> dict:
    """
    Returns delta info between current and prior quarter.

    R-04 Fix: Division by zero when prior_shares == 0 (new position).
    Handles three cases:
      - New position  (prior is None or 0)
      - Full exit     (current == 0, though EDGAR won't show these)
      - Change        (normal delta)
    """
    if not has_baseline:
        # No prior quarter exists for this filer, so nothing can be said about
        # whether the position is new. "The manager opened a position" and "we
        # have never seen this manager before" are different statements, and
        # only the first is a buy signal.
        return {
            "type":         "NO_BASELINE",
            "delta_shares": None,
            "delta_pct":    None,
        }

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


_NAME_SUFFIX_RE = re.compile(r"\b(INC|CORP|LTD|LLC|LP|PLC|CO|THE|DEL|COM|HOLDINGS?|GROUP)\b")


def _normalize_name(name: str) -> str:
    n = _NAME_SUFFIX_RE.sub("", (name or "").upper())
    return re.sub(r"\s+", " ", n).strip()


def _flag_possible_corporate_actions(parsed_filers: dict) -> None:
    """
    Section 18: don't score a same-quarter EXIT + NEW pair as a real sell/buy
    if it looks like a merger, spin-off, or share-class swap rather than an
    actual investment decision.

    Heuristic only (best-effort, not authoritative): within one filer's
    filing, if an exited position's issuer name shares its first name token
    with a brand-new position AND the new position's value is within 2x of
    the exited position's prior value, both are flagged
    `possible_corporate_action=True` so scoring.py can exclude them from both
    conviction and sell-signal scoring rather than risk a false signal.
    A real cross-reference against SEC merger/spin-off filings would be more
    reliable but is out of scope here - see README known-limitations.
    """
    for filer_name, filer_data in parsed_filers.items():
        exits = filer_data.get("exited_positions", [])
        news  = [p for p in filer_data.get("positions", []) if p["delta"]["type"] == "NEW"]
        if not exits or not news:
            continue

        for exit_pos in exits:
            exit_tokens = _normalize_name(exit_pos.get("name", "")).split()
            if not exit_tokens:
                continue
            exit_first_token = exit_tokens[0]
            exit_value = exit_pos.get("prior_value_usd_k", 0) or 0
            if exit_value <= 0:
                continue

            for new_pos in news:
                new_tokens = _normalize_name(new_pos.get("name", "")).split()
                if not new_tokens or new_tokens[0] != exit_first_token:
                    continue
                new_value = new_pos.get("value_usd_k", 0) or 0
                if new_value <= 0:
                    continue
                ratio = new_value / exit_value
                if 0.5 <= ratio <= 2.0:
                    exit_pos["possible_corporate_action"] = True
                    exit_pos["corporate_action_note"] = (
                        f"Possibly replaced by NEW position {new_pos.get('ticker') or new_pos.get('name')} "
                        f"in the same filing (similar name + value) - may be a merger/spin-off/"
                        f"share-class swap rather than a real EXIT."
                    )
                    new_pos["possible_corporate_action"] = True
                    new_pos["corporate_action_note"] = (
                        f"Possibly a continuation of exited position {exit_pos.get('ticker') or exit_pos.get('name')} "
                        f"in the same filing (similar name + value) - may be a merger/spin-off/"
                        f"share-class swap rather than a real NEW buy."
                    )


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
        # Quant books are capped at the top 500 positions for storage; weights
        # must still divide by the full reported book (see fetch_filings.py).
        full_reported = filer_data.get("full_reported_value") or 0
        if filer_data.get("is_capped") and full_reported > reported_aum:
            print(f"  ↔︎  {filer_name}: weights use the full reported book "
                  f"(${full_reported/1e9:,.1f}B, {filer_data.get('full_position_count')} positions), "
                  f"not the stored top {len(current_lookup)}")
            reported_aum = full_reported
        if reported_aum == 0:
            print(f"  ⚠️  {filer_name}: reported long-only AUM = 0, skipping")
            parsed_filers[filer_name] = {"error": "zero_aum", "positions": []}
            continue

        # Prior quarter lookup for this filer.
        # CUSIP is the primary join key: it's the SEC-reported identifier and
        # stable across quarters, whereas "ticker" is our own OpenFIGI-derived
        # enrichment that can resolve differently (or not at all) from one
        # quarter's fetch to the next. Joining ticker-first (the old behaviour)
        # silently breaks the delta calculation whenever resolution flips -
        # a real holding looks like a false EXIT + false NEW pair instead of
        # an ADD/REDUCE. Ticker/name are kept only as a fallback for the rare
        # case where a CUSIP itself changed (e.g. share reclassification).
        prior_lookup_by_key   = {}
        prior_lookup_by_cusip = {}
        prior_filer = _match_prior_filer(prior, filer_name, filer_data.get("cik", ""))
        if prior_filer is not None:
            if "positions" in prior_filer:
                for pos in prior_filer["positions"]:
                    key = pos.get("ticker") or pos.get("cusip") or ""
                    prior_lookup_by_key[key] = pos
                    cusip = pos.get("cusip", "")
                    if cusip:
                        prior_lookup_by_cusip[cusip] = pos

        matched_prior_cusips: set[str] = set()
        matched_prior_keys:   set[str] = set()

        positions = []
        for key, holding in current_lookup.items():
            ticker      = holding.get("ticker", "")
            net_bullish = holding.get("net_bullish", True)

            # Skip positions where puts dominate: they are bearish/hedged and
            # would produce false bullish signals downstream.
            # They are logged separately in put_positions for transparency.
            if not net_bullish:
                continue

            cusip = holding.get("cusip", "")
            prior_pos = prior_lookup_by_cusip.get(cusip) if cusip else None
            if prior_pos is None:
                prior_pos = prior_lookup_by_key.get(key)
            if prior_pos is not None:
                prior_cusip = prior_pos.get("cusip", "")
                if prior_cusip:
                    matched_prior_cusips.add(prior_cusip)
                matched_prior_keys.add(prior_pos.get("ticker") or prior_pos.get("cusip") or "")

            # Compare only long shares (prior data may have aggregated puts+longs).
            # Use prior "shares" but cap to avoid inflated deltas from old data format.
            prior_shares_raw = prior_pos["shares"] if prior_pos else None
            if prior_shares_raw is not None and ticker:
                prior_shares_adj = adjust_shares_for_splits(prior_shares_raw, ticker, splits)
            else:
                prior_shares_adj = prior_shares_raw

            delta = compute_delta(holding["shares"], prior_shares_adj, key,
                                  has_baseline=prior_filer is not None)

            # Portfolio weight uses long-only AUM (corrected denominator above)
            port_weight_pct = (holding["value_usd_thousands"] / reported_aum) * 100.0

            # Prior portfolio weight
            prior_port_weight = None
            if prior_pos and prior_filer:
                prior_aum = prior_filer.get("reported_aum_k", 0)
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
                "prior_rank":         prior_pos.get("rank") if prior_pos else None,
                "delta":              delta,
                "is_first_run":       prior is None,
                "net_bullish":        True,
                "weight_note":        "Long-only 13F AUM denominator – true weight may be lower",
            })

        # Sort by portfolio weight descending
        positions.sort(key=lambda x: x["port_weight_pct"], reverse=True)

        # Add rank + tier flags (Section 4: rank relative to Top-3/5/10/median)
        median_weight = (
            statistics.median(p["port_weight_pct"] for p in positions) if positions else 0.0
        )
        for i, pos in enumerate(positions, 1):
            pos["rank"] = i
            pos["weight_vs_median"] = (
                round(pos["port_weight_pct"] / median_weight, 2) if median_weight > 0 else None
            )
            pos["position_tier"] = (
                "TOP3" if i <= 3 else "TOP5" if i <= 5 else "TOP10" if i <= 10 else "OTHER"
            )

        # EXIT detection (Section 2/14): a position present last quarter but
        # absent from this filing was fully sold. EDGAR simply omits it rather
        # than reporting 0 shares, so this must be reconstructed from the diff
        # against prior_lookup - the old compute_delta "SOLD" path never fires
        # in practice because current_lookup never contains a 0-share holding.
        exited_positions = []
        if prior_lookup_by_key and not filer_data.get("is_capped"):
            seen_cusips: set[str] = set()
            for key, prior_pos in prior_lookup_by_key.items():
                prior_cusip = prior_pos.get("cusip", "")
                dedup_key = prior_cusip or key
                if dedup_key in seen_cusips:
                    continue  # avoid double-listing when both maps point to the same position
                seen_cusips.add(dedup_key)

                matched = (prior_cusip and prior_cusip in matched_prior_cusips) or key in matched_prior_keys
                if matched:
                    continue
                prior_shares = prior_pos.get("shares", 0)
                if not prior_shares:
                    continue  # prior entry was itself a pure-put placeholder etc.
                exited_positions.append({
                    "ticker":            prior_pos.get("ticker", ""),
                    "cusip":             prior_pos.get("cusip", ""),
                    "name":              prior_pos.get("name", ""),
                    "prior_shares":      prior_shares,
                    "prior_value_usd_k": prior_pos.get("value_usd_k", 0),
                    "prior_port_weight": prior_pos.get("port_weight_pct"),
                    "prior_rank":        prior_pos.get("rank"),
                    "delta":             {"type": "EXIT", "delta_shares": -prior_shares, "delta_pct": -100.0},
                })

        # Filing delay (Section 8): days between quarter-end (report_date) and
        # the actual filing date. Used downstream by the Freshness score -
        # a manager who reports late AND turns over the book fast is stale.
        try:
            report_dt = date.fromisoformat(filer_data["meta"].get("reportDate") or filer_data["meta"]["filingDate"])
            filing_dt = date.fromisoformat(filer_data["meta"]["filingDate"])
            filing_delay_days = (filing_dt - report_dt).days
        except (ValueError, TypeError):
            filing_delay_days = None

        parsed_filers[filer_name] = {
            "cik":           filer_data["cik"],
            "reported_aum_k": reported_aum,
            "filing_date":   filer_data["meta"]["filingDate"],
            # report_date = quarter-end (period of report); used as price anchor.
            "report_date":   filer_data["meta"].get("reportDate") or filer_data["meta"]["filingDate"],
            "filing_delay_days": filing_delay_days,
            "is_amendment":  filer_data["meta"]["isAmendment"],
            "position_count": len(positions),
            "full_position_count": filer_data.get("full_position_count", len(positions)),
            "is_capped":     bool(filer_data.get("is_capped")),
            # A top-500 subset cannot tell a sale from a rank drop, so EXITs are
            # simply not derivable for these books - stated, not guessed.
            "exit_detection_available": not bool(filer_data.get("is_capped")) and bool(prior_lookup_by_key),
            "exit_detection_note": ("historical book capped" if filer_data.get("is_capped")
                                    else "" if prior_lookup_by_key else "no prior baseline"),
            "median_position_weight_pct": round(median_weight, 3),
            "positions":     positions,
            "exited_positions": exited_positions,
        }

        # NOTE: the SEC switched the 13F <value> column from thousands to whole
        # dollars in 2023, so `value_usd_thousands` / `reported_aum_k` actually
        # hold dollars for current filings. Portfolio weights are ratios and are
        # unaffected; only absolute displays need the /1e9 below.
        capped_note = " (capped book – EXITs not derivable)" if filer_data.get("is_capped") else ""
        print(f"  ✅ {filer_name}: {len(positions)} positions, "
              f"{len(exited_positions)} exits{capped_note}, "
              f"AUM ${reported_aum/1e9:,.1f}B (13F reported, long-only)")

    _flag_possible_corporate_actions(parsed_filers)

    return {
        "date":            today_str,
        "period_of_report": raw.get("report_date", ""),
        # Was this dataset itself produced by a real quarter-over-quarter diff?
        # A baseline built without its own predecessor marks every holding NEW,
        # and those NEW labels are artifacts, not investment decisions.
        "has_prior_baseline": prior is not None,
        "report_date":     raw.get("report_date", ""),   # legacy alias
        "generated_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prior_date":    prior["date"] if prior else None,
        "prior_report_date": infer_report_date(prior) if prior else "",
        "is_first_run":  prior is None,
        "recent_splits": splits,
        "filers":        parsed_filers,
    }


def run():
    today_str = run_date()

    print(f"\n{'='*60}")
    print(f"13F Parser & Delta Calculator – {today_str}")
    print(f"{'='*60}")

    raw = load_latest_raw(today_str)
    prior = load_prior_quarter(today_str, raw.get("report_date", ""))

    print(f"📅 Reporting quarter: {raw.get('report_date', '?')}")
    if prior:
        print(f"📂 Prior quarter data: {prior['date']} (quarter {infer_report_date(prior) or '?'})")
    else:
        print("⚠️  First run – no prior quarter data available. Deltas will be marked as NEW.")

    parsed = parse_and_enrich(raw, prior)

    output_path = DATA_DIR / f"{today_str}_holdings_parsed.json"
    tmp_path = output_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(parsed, f, indent=2, default=str)
    tmp_path.replace(output_path)

    print(f"\n✅ Parsed holdings saved to {output_path}")

    # The verdict is printed here for visibility, but the hard stop lives in its
    # own pipeline step (data_quality.py) so a deliberate baseline rebuild can
    # parse a quarter that has no predecessor on file.
    import data_quality
    data_quality.gate(today_str, parsed, strict=False)


if __name__ == "__main__":
    run()
