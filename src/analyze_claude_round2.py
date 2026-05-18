"""
analyze_claude_round2.py
Claude API – Round 2: Select the best specific options from real Tradier data.

Uses claude-sonnet (CLAUDE_MODEL_R2) – precision matters here.
Claude receives the top 5 theses + real option chains and picks the
single best contract per stock, with explicit reasoning on strike,
expiry, IV, and liquidity.

Improvements:
  - CLAUDE_MODEL_R2 (Sonnet) explicitly
  - Structured JSON output enforced
  - Retry with exponential backoff
  - Validates that report is non-empty before saving
"""

import json
import os
import re  # used both for response parsing and company-name normalisation
import time
from datetime import date

import anthropic

from config import (
    CLAUDE_MAX_TOKENS, CLAUDE_MODEL_R2, CLAUDE_RETRY_COUNT,
    CLAUDE_RETRY_DELAY, DATA_DIR,
)


def load_round1(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_claude_round1.json"
    if not path.exists():
        raise FileNotFoundError(f"Claude Round 1 results not found: {path}")
    with open(path) as f:
        return json.load(f)


def load_options(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_options.json"
    if not path.exists():
        raise FileNotFoundError(f"Options data not found: {path}")
    with open(path) as f:
        return json.load(f)


def format_options_for_prompt(ticker: str, opt_data: dict) -> str:
    """Formats option candidates concisely for the prompt, including live stock price."""
    options       = opt_data.get("options", [])
    current_price = opt_data.get("current_price")
    change_pct    = opt_data.get("change_pct")
    direction     = opt_data.get("direction", "BULLISH")

    price_str = f"${current_price}" if current_price else "n/a"
    change_str = f"{change_pct:+.2f}%" if change_pct is not None else "n/a"

    iv_m = opt_data.get("iv_metrics", {})
    iv_line = ""
    if iv_m:
        iv_line = (f" | IV Rank: {iv_m.get('iv_rank','?')} "
                   f"(HV30={iv_m.get('hv_30d_pct','?')}%, "
                   f"IV/HV={iv_m.get('iv_hv_ratio','?')}) → {iv_m.get('verdict','')}")

    if not options:
        return f"\n{ticker} — Current: {price_str} ({change_str}) | No options data available\n"

    lines = [f"\n{ticker} — Current price: {price_str} ({change_str} today) | Direction: {direction}{iv_line}"]
    lines.append(f"{'Symbol':<22}{'Strike':<10}{'vs Spot':<10}{'Expiry':<14}{'Mid':<8}"
                 f"{'Sprd%':<8}{'Vol':<8}{'OI':<8}{'Delta':<8}{'IV':<8}")
    lines.append("─" * 104)

    for opt in options[:8]:
        strike     = opt.get("strike")
        delta_str  = f"{opt['delta']:.2f}" if opt.get("delta") else "n/a"
        iv_str     = f"{float(opt['implied_volatility']):.1%}" if opt.get("implied_volatility") else "n/a"
        mid_str    = f"${opt['mid']}" if opt.get("mid") else "n/a"
        spread_str = f"{opt['spread_pct']}%" if opt.get("spread_pct") is not None else "n/a"

        # Show how far the strike is from current price
        if strike and current_price:
            vs_spot = f"{((float(strike) / float(current_price)) - 1) * 100:+.1f}%"
        else:
            vs_spot = "n/a"

        lines.append(
            f"{str(opt.get('symbol', '')):<22}"
            f"{str(strike or ''):<10}"
            f"{vs_spot:<10}"
            f"{str(opt.get('expiration_date', '')):<14}"
            f"{mid_str:<8}"
            f"{spread_str:<8}"
            f"{str(opt.get('volume', '')):<8}"
            f"{str(opt.get('open_interest', '')):<8}"
            f"{delta_str:<8}"
            f"{iv_str:<8}"
        )
        if not opt.get("greeks_available"):
            lines.append("  ⚠️  Greeks unavailable (pre-market snapshot)")

    return "\n".join(lines)


def build_round2_prompt(r1: dict, options_data: dict) -> str:
    today_str = r1.get("analysis_date", date.today().isoformat())

    # Build thesis summary
    theses = []
    for stock in r1.get("top5", []):
        theses.append(
            f"#{stock['rank']} {stock['ticker']} – {stock['company_name']}\n"
            f"   Thesis: {stock['thesis']}\n"
            f"   Key buyers: {', '.join(stock.get('key_buyers', []))}\n"
            f"   Risk: {stock.get('risk_factors', '')}"
        )

    # Build options tables
    option_tables = []
    for ticker, opt_data in options_data.get("options", {}).items():
        option_tables.append(format_options_for_prompt(ticker, opt_data))

    return f"""You are an expert options strategist with deep knowledge of institutional investor behavior.

ANALYSIS DATE: {today_str}
TASK: For each of the 5 stocks below, select the SINGLE BEST option based on the provided real-market data.

INVESTMENT THESES (from 13F conviction analysis):
{''.join(theses)}

REAL OPTIONS DATA FROM TRADIER:
{''.join(option_tables)}

OPTION SELECTION CRITERIA:
1. Strike selection: Use the "vs Spot" column to assess moneyness. For BULLISH plays, prefer
   slightly OTM (+2% to +8% above current price, Delta 0.35-0.50) for leverage with reasonable
   probability of profit. ATM (Delta 0.45-0.55) for higher conviction plays.
2. Expiry: 3-6 months allows time for institutional thesis to play out (13F data is already
   ~45 days old, so add that to your horizon).
3. IV Rank (shown above each options table): CRITICAL filter.
   - IV Rank ≥ 70 (EXPENSIVE): Do NOT buy naked calls. Instead recommend a Bull Call Spread
     (buy lower strike, sell higher strike same expiry) to neutralise the IV premium.
   - IV Rank 40–69 (MODERATE): Naked call acceptable, note the elevated premium.
   - IV Rank < 40 (CHEAP): Naked call preferred; IV expansion adds to profit.
   The IV/HV ratio tells you how much you're paying over realised volatility.
4. Liquidity: Prefer options with volume >200, OI >500, and spread% <10%. Spread% is shown
   in the table — wide spreads (>12%) hurt entry/exit significantly.
5. Current price context: The live stock price is shown above each options table. Use it to
   judge whether the thesis has already played out (stock already up a lot since 13F filing)
   or still has room to run.

Use the submit_options_recommendations tool to return your selections.
For each stock: if IV Rank ≥ 70 set strategy="BULL_CALL_SPREAD" and populate short_leg_symbol/short_strike;
otherwise set strategy="LONG_CALL" and omit the short leg fields.
Always populate iv_rank_note explaining your strategy choice."""


_ROUND2_TOOL = {
    "name": "submit_options_recommendations",
    "description": "Submit the recommended option trades for the top-5 stocks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "options_recommendations": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["rank","stock_ticker","company_name","option_symbol",
                                 "option_type","strike","expiration","entry_price_mid",
                                 "max_risk_per_contract","investment_thesis_link",
                                 "option_rationale","profit_target","stop_loss","key_risk"],
                    "properties": {
                        "rank":                    {"type": "integer"},
                        "stock_ticker":            {"type": "string"},
                        "company_name":            {"type": "string"},
                        "strategy":                {"type": "string",
                                                    "description": "LONG_CALL or BULL_CALL_SPREAD"},
                        "option_symbol":           {"type": "string"},
                        "short_leg_symbol":        {"type": "string",
                                                    "description": "Sell leg for spreads; omit for naked calls"},
                        "option_type":             {"type": "string"},
                        "strike":                  {"type": "number"},
                        "short_strike":            {"type": "number",
                                                    "description": "Short leg strike for spreads"},
                        "expiration":              {"type": "string"},
                        "entry_price_mid":         {"type": "number"},
                        "max_risk_per_contract":   {"type": "number"},
                        "investment_thesis_link":  {"type": "string"},
                        "option_rationale":        {"type": "string"},
                        "iv_rank_note":            {"type": "string",
                                                    "description": "Note on IV rank and why strategy was chosen"},
                        "profit_target":           {"type": "string"},
                        "stop_loss":               {"type": "string"},
                        "key_risk":                {"type": "string"},
                        "greeks_note":             {"type": "string"},
                    },
                },
            },
            "portfolio_note": {"type": "string"},
            "disclaimer":     {"type": "string"},
        },
        "required": ["options_recommendations"],
    },
}


def call_claude_with_retry(prompt: str) -> dict:
    """Calls Claude with tool_use forced – returns the structured dict directly."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    for attempt in range(1, CLAUDE_RETRY_COUNT + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL_R2,
                max_tokens=CLAUDE_MAX_TOKENS,
                system="You are an expert options strategist.",
                tools=[_ROUND2_TOOL],
                tool_choice={"type": "tool", "name": "submit_options_recommendations"},
                messages=[{"role": "user", "content": prompt}],
            )
            for block in response.content:
                if block.type == "tool_use" and block.name == "submit_options_recommendations":
                    return block.input
            raise ValueError("Claude returned no tool_use block")

        except anthropic.RateLimitError:
            wait = CLAUDE_RETRY_DELAY * (2 ** (attempt - 1))
            print(f"  ⏳ Rate limit. Waiting {wait}s (attempt {attempt}/{CLAUDE_RETRY_COUNT})")
            time.sleep(wait)

        except anthropic.APIError as e:
            wait = CLAUDE_RETRY_DELAY * attempt
            print(f"  ⚠️  API error (attempt {attempt}): {e}. Retry in {wait}s...")
            time.sleep(wait)

    raise RuntimeError(f"Claude API failed after {CLAUDE_RETRY_COUNT} attempts")


def validate_result(result: dict) -> bool:
    """
    R-14 Fix: Ensure the result is substantive before saving.
    Returns False if the result appears empty or malformed.
    """
    recs = result.get("options_recommendations", [])
    if not recs:
        print("  ⚠️  VALIDATION FAILED: No recommendations in Claude response")
        return False
    if len(recs) < 3:
        print(f"  ⚠️  VALIDATION WARNING: Only {len(recs)} recommendations (expected 5)")
    for rec in recs:
        if not rec.get("option_symbol") or not rec.get("stock_ticker"):
            print(f"  ⚠️  VALIDATION FAILED: Missing ticker or symbol in recommendation: {rec}")
            return False
    return True


def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Claude Analysis Round 2 – {today_str}")
    print(f"{'='*60}")

    r1           = load_round1(today_str)
    options_data = load_options(today_str)

    prompt = build_round2_prompt(r1, options_data)

    print(f"📤 Sending real options data for {len(r1.get('top5', []))} stocks to Claude...")

    result = call_claude_with_retry(prompt)

    if not validate_result(result):
        raise RuntimeError("Claude Round 2 result failed validation – aborting report")

    print(f"✅ Claude selected {len(result.get('options_recommendations', []))} option trades:")
    for rec in result.get("options_recommendations", []):
        mid = rec.get("entry_price_mid", "?")
        print(f"   #{rec['rank']} {rec['stock_ticker']} → {rec['option_symbol']} "
              f"@ ${mid} | {rec.get('profit_target', '')}")

    # Enrich round1_top5 with post-filing price performance, per-filer detail,
    # and multi-quarter signal from scores.json.
    # When OpenFIGI mapping failed, scores.json keys are CUSIPs rather than
    # ticker symbols. In that case we fall back to normalised company-name matching.
    top5 = r1.get("top5", [])
    try:
        scores_path = DATA_DIR / f"{today_str}_scores.json"
        with open(scores_path) as sf:
            scores = json.load(sf)
        aggregated = scores.get("aggregated", [])

        def _norm(n: str) -> str:
            n = re.sub(
                r'\b(INC\.?|CORP\.?|LTD\.?|LLC|LP|PLC|CO\.?|THE|HOLDING|HOLDINGS|'
                r'INCORPORATED|CORPORATION|LIMITED)\b', '', n.upper())
            return re.sub(r'[^A-Z0-9]', ' ', n).split()[0] if n.strip() else ''

        by_ticker: dict = {}
        by_name:   dict = {}
        for agg in aggregated:
            by_ticker[agg["ticker"]] = agg
            norm = _norm(agg.get("name", ""))
            if norm:
                by_name[norm] = agg

        enriched = 0
        for stock in top5:
            t  = stock.get("ticker", "")
            cn = _norm(stock.get("company_name", ""))
            matched = by_ticker.get(t) or by_name.get(cn)
            if matched:
                enriched += 1
            stock["post_filing_perf"] = matched.get("post_filing_perf", {}) if matched else {}
            stock["filer_details"]    = matched.get("filers",            []) if matched else []
            stock["mq_signal"]        = matched.get("mq_signal",         {}) if matched else {}

        print(f"  ✅ Enriched {enriched}/{len(top5)} stocks with post-filing perf + filer details")
    except Exception as e:
        print(f"  ⚠️  Could not enrich with post-filing perf / filer details: {e}")

    # Merge Round 1 and Round 2 into final analysis file
    final = {
        "date":              today_str,
        "round1_top5":       top5,
        "market_context":    r1.get("market_context", ""),
        "options_recs":      result.get("options_recommendations", []),
        "portfolio_note":    result.get("portfolio_note", ""),
        "disclaimer":        result.get("disclaimer", ""),
    }

    output_path = DATA_DIR / f"{today_str}_final_analysis.json"
    with open(output_path, "w") as f:
        json.dump(final, f, indent=2)

    print(f"💾 Final analysis saved to {output_path}")


if __name__ == "__main__":
    run()
