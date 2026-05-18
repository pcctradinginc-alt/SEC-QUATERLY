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


def _build_cached_system_context(r1: dict) -> str:
    """
    Builds the stable context block that is sent (and cached) with EVERY per-ticker
    call in Round 2.  Includes role, all 5 theses, and the full selection criteria.

    This block must be ≥ 1024 tokens (Sonnet minimum) to qualify for caching.
    With 5 theses + criteria it lands at ~1500–2000 tokens.
    """
    today_str = r1.get("analysis_date", date.today().isoformat())

    theses_text = ""
    for stock in r1.get("top5", []):
        theses_text += (
            f"\n#{stock['rank']} {stock['ticker']} – {stock['company_name']}\n"
            f"   Thesis: {stock['thesis']}\n"
            f"   Key buyers: {', '.join(stock.get('key_buyers', []))}\n"
            f"   Risk: {stock.get('risk_factors', '')}\n"
        )

    return f"""You are an expert options strategist with deep knowledge of institutional investor behaviour.

ANALYSIS DATE: {today_str}
DATA SOURCE: SEC 13F filings (up to 45-day lag). These theses were identified by a prior
quantitative screen of 13 institutional investors; they reflect positions as of quarter-end.

═══════════════════════════════════════════════════════
TOP-5 INVESTMENT THESES (from 13F conviction analysis)
═══════════════════════════════════════════════════════
{theses_text}

═══════════════════════════════════════════════════════
OPTION SELECTION CRITERIA  (apply to the ticker you receive)
═══════════════════════════════════════════════════════
1. STRIKE SELECTION
   • Prefer slightly OTM (+2–8% above spot, Delta 0.35–0.50) for leveraged bullish exposure.
   • Use ATM (Delta 0.45–0.55) for higher-conviction names.
   • Use the "vs Spot" column to judge moneyness.

2. EXPIRY
   • 3–6 months. The 13F data is already ~45 days stale; the thesis needs time to play out.
   • Prefer expiries that straddle a known catalyst (earnings, conference season).

3. IV RANK — CRITICAL
   • IV Rank is provided in the options table header.
   • IV Rank ≥ 70 (EXPENSIVE): NEVER recommend a naked long call.
     → Recommend a Bull Call Spread: buy lower strike, sell higher strike, same expiry.
     → This caps profit but drastically reduces the IV premium drag.
   • IV Rank 40–69 (MODERATE): naked call acceptable; note the elevated premium.
   • IV Rank < 40 (CHEAP): naked call preferred; IV expansion will add to profit.
   • Always state the IV rank and your strategy rationale in iv_rank_note.

4. LIQUIDITY
   • Prefer Volume > 200, OI > 500, Spread% < 10%.
   • Spread% > 12% meaningfully hurts entry/exit — penalise these options.

5. PRICE ACTION CONTEXT
   • If the stock has already moved >15% since the 13F filing quarter-end, the thesis
     may be partially priced in. Reflect this in profit_target and key_risk.

TOOL INSTRUCTIONS
• Respond using the submit_option_recommendation tool.
• If IV Rank ≥ 70, set strategy="BULL_CALL_SPREAD" and fill short_leg_symbol/short_strike.
• Otherwise set strategy="LONG_CALL" and leave short leg fields absent.
• Always fill iv_rank_note with the IV rank value and your reasoning."""


_PER_TICKER_TOOL = {
    "name": "submit_option_recommendation",
    "description": "Submit the single best option trade for the stock provided.",
    "input_schema": {
        "type": "object",
        "required": [
            "rank","stock_ticker","company_name","strategy",
            "option_symbol","option_type","strike","expiration",
            "entry_price_mid","max_risk_per_contract",
            "investment_thesis_link","option_rationale",
            "iv_rank_note","profit_target","stop_loss","key_risk",
        ],
        "properties": {
            "rank":                   {"type": "integer"},
            "stock_ticker":           {"type": "string"},
            "company_name":           {"type": "string"},
            "strategy":               {"type": "string",
                                       "enum": ["LONG_CALL", "BULL_CALL_SPREAD"]},
            "option_symbol":          {"type": "string"},
            "short_leg_symbol":       {"type": "string"},
            "option_type":            {"type": "string"},
            "strike":                 {"type": "number"},
            "short_strike":           {"type": "number"},
            "expiration":             {"type": "string"},
            "entry_price_mid":        {"type": "number"},
            "max_risk_per_contract":  {"type": "number"},
            "investment_thesis_link": {"type": "string"},
            "option_rationale":       {"type": "string"},
            "iv_rank_note":           {"type": "string"},
            "profit_target":          {"type": "string"},
            "stop_loss":              {"type": "string"},
            "key_risk":               {"type": "string"},
            "greeks_note":            {"type": "string"},
        },
    },
}


def _call_for_ticker(
    client: anthropic.Anthropic,
    cached_system: str,
    rank: int,
    stock: dict,
    opt_data: dict,
) -> dict | None:
    """
    Makes ONE Claude call for a single ticker with the shared context cached.

    Prompt caching works as follows:
      • Call 1: cache MISS  → Anthropic stores `cached_system` for 5 min
      • Calls 2-5: cache HIT → input tokens for `cached_system` billed at ~10% of normal
    This saves ~80% of input token costs for 4 of the 5 per-run calls.

    NOTE: The cache is model-specific. Because both rounds use CLAUDE_MODEL_R2 (Sonnet)
    for the per-ticker calls, the cache created by call 1 is valid for calls 2-5.
    """
    options_block = format_options_for_prompt(stock["ticker"], opt_data)
    user_text = (
        f"Select the best option trade for {stock['ticker']} (rank #{rank}).\n"
        f"This is the real options data from Tradier:\n{options_block}"
    )

    for attempt in range(1, CLAUDE_RETRY_COUNT + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL_R2,
                max_tokens=1500,   # one recommendation needs far less than 4096
                system=[
                    {
                        "type": "text",
                        "text": cached_system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[_PER_TICKER_TOOL],
                tool_choice={"type": "tool", "name": "submit_option_recommendation"},
                messages=[{"role": "user", "content": user_text}],
            )
            for block in response.content:
                if block.type == "tool_use" and block.name == "submit_option_recommendation":
                    usage = getattr(response, "usage", None)
                    if usage:
                        cache_read  = getattr(usage, "cache_read_input_tokens",  0) or 0
                        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
                        if cache_read:
                            print(f"      💾 Cache HIT  – {cache_read:,} cached tokens")
                        elif cache_write:
                            print(f"      📝 Cache MISS – {cache_write:,} tokens written to cache")
                    return block.input
            raise ValueError("Claude returned no tool_use block")

        except anthropic.RateLimitError:
            wait = CLAUDE_RETRY_DELAY * (2 ** (attempt - 1))
            print(f"  ⏳ Rate limit. Waiting {wait}s (attempt {attempt}/{CLAUDE_RETRY_COUNT})")
            time.sleep(wait)
        except anthropic.APIError as e:
            wait = CLAUDE_RETRY_DELAY * attempt
            print(f"  ⚠️  API error attempt {attempt}: {e}. Retry in {wait}s…")
            time.sleep(wait)

    print(f"  ⚠️  Claude failed for {stock['ticker']} after {CLAUDE_RETRY_COUNT} attempts – skipping")
    return None


def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Claude Analysis Round 2 – {today_str}")
    print(f"{'='*60}")

    r1           = load_round1(today_str)
    options_data = load_options(today_str)
    top5         = r1.get("top5", [])

    cached_system = _build_cached_system_context(r1)
    print(f"📤 Per-ticker calls for {len(top5)} stocks (system context cached after call 1)…")
    est_sys_tokens = len(cached_system) // 4
    print(f"   Cached system block: ~{est_sys_tokens:,} tokens "
          f"({'≥1024 ✅' if est_sys_tokens >= 1024 else '<1024 ⚠️ may not cache'})")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    options_by_ticker = options_data.get("options", {})

    recs: list[dict] = []
    for stock in top5:
        ticker   = stock["ticker"]
        opt_data = options_by_ticker.get(ticker, {})
        print(f"  ▶ {ticker} (#{stock['rank']})…")
        rec = _call_for_ticker(client, cached_system, stock["rank"], stock, opt_data)
        if rec:
            recs.append(rec)
            mid = rec.get("entry_price_mid", "?")
            strat = rec.get("strategy", "LONG_CALL")
            print(f"    ✅ {rec.get('option_symbol','?')} @ ${mid} [{strat}] | "
                  f"{rec.get('profit_target','')[:50]}")

    if len(recs) < 3:
        raise RuntimeError(
            f"Only {len(recs)}/5 option recommendations returned – aborting report"
        )

    # Wrap into the legacy shape that the rest of the pipeline expects
    result = {
        "options_recommendations": recs,
        "portfolio_note": "",
        "disclaimer": (
            "Options involve significant risk including total loss of premium. "
            "Based on delayed 13F data. Not investment advice."
        ),
    }

    # Enrich round1_top5 with post-filing price performance, per-filer detail,
    # and multi-quarter signal from scores.json.
    # When OpenFIGI mapping failed, scores.json keys are CUSIPs rather than
    # ticker symbols. In that case we fall back to normalised company-name matching.
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
