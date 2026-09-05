"""
analyze_claude_round1.py
Claude API – Round 1: Identify Top 5 investment candidates.

Uses claude-haiku (CLAUDE_MODEL_R1) – sufficient for screening and
~20× cheaper than Sonnet. Sonnet is reserved for Round 2 (precise
option selection where nuance matters more).

Improvements:
  - CLAUDE_MODEL_R1 (Haiku) instead of Sonnet
  - Multi-quarter build signals included in prompt
  - Price-action staleness flags surfaced to Claude
  - Exponential backoff retry
  - Robust JSON extraction
"""

import json
import os
import time
from datetime import date

import anthropic

from config import (
    CLAUDE_MAX_TOKENS_R1, CLAUDE_MODEL_R1, CLAUDE_RETRY_COUNT,
    CLAUDE_RETRY_DELAY, DATA_DIR,
)


def load_scores(today_str: str) -> dict:
    path = DATA_DIR / f"{today_str}_scores.json"
    if not path.exists():
        raise FileNotFoundError(f"Scores not found: {path}")
    with open(path) as f:
        return json.load(f)


def normalize_ticker(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "/")


def build_prompt(scores: dict) -> str:
    today_str  = scores["date"]
    top20      = scores["top20"]
    clusters   = scores["clusters"]
    mq_signals = scores.get("mq_signals", {})
    sell_signals = scores.get("sell_signals", [])

    positions_text = []
    for i, agg in enumerate(top20, 1):
        filer_summary = "; ".join(
            f"{f['filer']} (quality={f.get('manager_quality_score', '?')}, {f['delta_type']}, "
            f"Δ{f['delta_pct'] if f['delta_pct'] is not None else 'N/A'}%, "
            f"port_wt={f['port_weight_pct']}%, tier={f.get('position_tier','?')}, "
            f"weight_vs_median={f.get('weight_vs_median', '?')}x)"
            for f in agg["filers"]
        )

        # Price-action context (stored as post_filing_perf dict by scoring.py)
        perf      = agg.get("post_filing_perf") or {}
        price_chg = perf.get("pct_change")
        days_since = perf.get("days_since_filing")
        price_note = ""
        if price_chg is not None:
            days_str = f" ({days_since}d)" if days_since else ""
            if "PRICE_ACTION_STALE" in agg.get("flags", []):
                price_note = f" ⚠️ ALREADY +{price_chg:.0f}% SINCE FILING{days_str} – thesis may be priced in"
            elif "PRICE_ACTION_WARNING" in agg.get("flags", []):
                price_note = f" ⚡ +{price_chg:.0f}% since filing{days_str}"
            else:
                sign = "+" if price_chg >= 0 else ""
                price_note = f" ({sign}{price_chg:.0f}% since filing{days_str})"

        # Multi-quarter context
        mq_note = ""
        mq = mq_signals.get(agg["ticker"])
        if mq and mq["build_quarters"] >= 2:
            mq_note = (
                f"\n   Multi-Quarter: {mq['build_quarters']} quarters of building"
                f" | avg delta {mq['avg_delta_pct']}%"
                f" | accumulation_score(recency-weighted)={mq.get('accumulation_score')}"
                f" | flags: {', '.join(mq['flags']) or 'none'}"
            )

        comp = agg.get("best_components", {})
        comp_note = (
            f"\n   Alpha Score components: active_weight={comp.get('active_weight')} "
            f"position_change={comp.get('position_change')} manager_quality={comp.get('manager_quality')} "
            f"consensus={comp.get('consensus')} accumulation={comp.get('accumulation')} "
            f"freshness={comp.get('freshness')} abnormal_ownership=N/A(no data source yet)"
        )

        positions_text.append(
            f"{i}. {agg['ticker']} ({agg['name']}){price_note}\n"
            f"   13F Alpha Score: {agg['alpha_score']}/100 | Filers: {agg['filer_count']} | "
            f"Crowding: {agg.get('crowding_label','?')} | "
            f"Early Smart Money: {'YES' if agg.get('early_smart_money') else 'no'}\n"
            f"   Flags: {', '.join(agg['flags']) or 'none'}\n"
            f"   Cluster: {'YES – ' + str(agg['cluster_count']) + ' funds' if agg['cluster_count'] >= 2 else 'no'}\n"
            f"   Manager activity: {filer_summary}{mq_note}{comp_note}\n"
        )

    cluster_text = ""
    if clusters:
        cluster_text = "\nCLUSTER SIGNALS (2+ tracked funds buying same ticker):\n"
        for ticker, filers in clusters.items():
            cluster_text += f"  {ticker}: {', '.join(filers)}\n"

    sell_text = ""
    if sell_signals:
        sell_text = "\nNOTABLE EXITS / REDUCTIONS (Section 14 – negative signals, for risk context only):\n"
        for s in sell_signals[:15]:
            if s["type"] == "EXIT":
                sell_text += (
                    f"  {s['ticker']}: {s['filer']} (quality={s['manager_quality_score']}) EXITED "
                    f"a former rank #{s['prior_rank']} position"
                    f"{' (was TOP-5!)' if s.get('was_top5_position') else ''}"
                    f"{' — PARALLEL SELLING with ' + ', '.join(s['parallel_sellers']) if s.get('parallel_selling') else ''}\n"
                )
            else:
                sell_text += (
                    f"  {s['ticker']}: {s['filer']} (quality={s['manager_quality_score']}) REDUCED "
                    f"{s['delta_pct']}%"
                    f"{' — PARALLEL SELLING with ' + ', '.join(s['parallel_sellers']) if s.get('parallel_selling') else ''}\n"
                )

    return f"""You are an expert quantitative analyst specializing in 13F filing analysis and institutional investor tracking.

ANALYSIS DATE: {today_str}
DATA SOURCE: SEC 13F filings (latest available, up to 45-day lag)

IMPORTANT DISCLAIMER: 13F data reflects only US long equity positions >$200K.
Portfolio weights use long-only AUM (cash/shorts/bonds excluded → weights are systematically overstated).
Stock splits have been adjusted. Never conclude "the manager is bullish on X" from a single filing –
say "the manager increased its reported long position in X" instead (13F shows no shorts, cash, or
most derivatives). Treat this as an idea generator, not a buy signal.

DATA GAPS YOU MUST BE HONEST ABOUT (do not fabricate numbers for these):
- No benchmark index data is wired up yet, so "Active Weight" vs. S&P 500/Russell is NOT available.
  Use weight_vs_median (position size relative to that manager's OWN typical position) as the closest proxy instead.
- No market-wide institutional ownership data is available (only these 13 tracked funds are observed).
  Do not state or imply broad institutional ownership trends – say so explicitly when the format below asks for it.
- Abnormal Ownership is not computed (no data source) – state "not available" rather than guessing.

IMPORTANT – PRICE ACTION: Positions marked ⚠️ ALREADY +X% SINCE FILING have potentially
already played out. Strong preference for fresh ideas that have NOT run significantly yet.

TOP 20 BY 13F ALPHA SCORE (0-100, components shown are pre-computed – do not recompute them, just interpret):
{''.join(positions_text)}
{cluster_text}
{sell_text}

YOUR TASK:
Analyze the above data and identify the TOP 5 stocks with the strongest institutional conviction signals
that have NOT already fully played out in price.

Consider in priority order:
1. EARLY_SMART_MONEY_ACCUMULATION flag (2-6 high-quality managers building early, still low/moderate crowding) – the single most preferred setup
2. Multi-quarter building (3+ quarters of consistent accumulation = strongest signal)
3. Cluster signals (multiple quality-weighted funds buying simultaneously) net of Crowding label
4. Fresh entries that haven't run >15% since the filing date
5. 13F Alpha Score magnitude and its component breakdown
6. Quality of the buying funds (dynamic manager_quality score, not just fund name recognition)

Explicitly DOWNWEIGHT stocks with PRICE_ACTION_STALE flag or crowding HIGH/EXTREME – the thesis is
likely priced in or the trade is already very crowded. Do NOT recommend a stock solely because it's a
large market-value position, or because "many funds hold it" without a change in behavior (Section 17).

For each of your 5 picks, fill in the full structured output (Section 20 format). "signal" must be one of
VERY_STRONG_BUY, STRONG, MODERATE, WEAK, NEGATIVE, judged against how many independent signals line up.
"fazit" must directly answer: is this an unusually strong institutional signal, or just a routine portfolio
change? Be willing to answer "routine" if that's the honest read – do not inflate weak setups.

Use the submit_top5_analysis tool to return your selections."""


_ROUND1_TOOL = {
    "name": "submit_top5_analysis",
    "description": "Submit the top-5 conviction picks from the 13F analysis, in the full Section-20 structured format.",
    "input_schema": {
        "type": "object",
        "properties": {
            "analysis_date":  {"type": "string"},
            "market_context": {"type": "string", "description": "2-3 sentence market summary"},
            "top5": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "required": [
                        "rank", "ticker", "company_name", "alpha_score", "signal",
                        "manager_activity", "conviction_narrative", "accumulation_narrative",
                        "consensus_narrative", "institutional_ownership_narrative",
                        "crowding_label", "freshness_narrative", "why_interesting", "risks",
                        "fazit", "thesis", "key_buyers", "primary_flag", "risk_factors", "direction",
                    ],
                    "properties": {
                        "rank":               {"type": "integer"},
                        "ticker":             {"type": "string"},
                        "company_name":       {"type": "string"},
                        "alpha_score":        {"type": "number", "description": "Copy the given 13F Alpha Score – do not recompute."},
                        "conviction_score":   {"type": "number", "description": "Alias of alpha_score, kept for backward compatibility."},
                        "signal": {
                            "type": "string",
                            "enum": ["VERY_STRONG_BUY", "STRONG", "MODERATE", "WEAK", "NEGATIVE"],
                        },
                        "manager_activity": {
                            "type": "array",
                            "description": "One row per buying manager (Section 20 table): manager | quality | status | weight before | weight now | shares change | weight_vs_median (Active-Weight proxy).",
                            "items": {
                                "type": "object",
                                "required": ["manager", "quality_score", "status", "weight_now_pct", "shares_change_pct"],
                                "properties": {
                                    "manager":            {"type": "string"},
                                    "quality_score":      {"type": "number"},
                                    "status":             {"type": "string", "description": "NEW / ADD / REDUCE / EXIT"},
                                    "weight_before_pct":  {"type": ["number", "null"]},
                                    "weight_now_pct":      {"type": "number"},
                                    "shares_change_pct":  {"type": ["number", "null"]},
                                    "weight_vs_median":   {"type": ["number", "null"], "description": "Active-Weight proxy: position size vs. this manager's own median position."},
                                },
                            },
                        },
                        "conviction_narrative":   {"type": "string", "description": "Assessment of position size and portfolio importance."},
                        "accumulation_narrative": {"type": "string", "description": "How the position built up over the available quarters."},
                        "consensus_narrative":    {"type": "string", "description": "Which quality managers are buying together, and how strong that consensus is."},
                        "institutional_ownership_narrative": {
                            "type": "string",
                            "description": "State plainly that broad institutional-ownership data is NOT available (only the 13 tracked funds) rather than fabricating a trend.",
                        },
                        "crowding_label": {"type": "string", "enum": ["LOW", "MODERATE", "HIGH", "EXTREME"], "description": "Copy the given crowding_label – do not invent your own."},
                        "freshness_narrative": {"type": "string", "description": "How filing delay + manager turnover affect how stale this signal already is."},
                        "why_interesting": {"type": "array", "items": {"type": "string"}, "maxItems": 5, "description": "Max 3-5 precise bullet points."},
                        "risks":           {"type": "array", "items": {"type": "string"}, "maxItems": 5, "description": "Max 3-5 precise bullet points, incl. possible misinterpretations."},
                        "fazit": {"type": "string", "description": "Sober paragraph answering: unusually strong institutional signal, or routine portfolio change?"},
                        # Backward-compatible fields (used by round 2 + report rendering)
                        "thesis":             {"type": "string"},
                        "key_buyers":         {"type": "array", "items": {"type": "string"}},
                        "cluster_signal":     {"type": "boolean"},
                        "multi_quarter_build":{"type": "boolean"},
                        "primary_flag":       {"type": "string"},
                        "risk_factors":       {"type": "string"},
                        "direction":          {"type": "string", "enum": ["BULLISH", "BEARISH"]},
                    },
                },
            },
            "disclaimer": {"type": "string"},
        },
        "required": ["analysis_date", "market_context", "top5"],
    },
}


def call_claude_with_retry(prompt: str) -> dict:
    """Calls Claude with tool_use forced – returns the structured dict directly."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    for attempt in range(1, CLAUDE_RETRY_COUNT + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL_R1,
                max_tokens=CLAUDE_MAX_TOKENS_R1,
                system="You are a quantitative analyst specialising in 13F filing analysis.",
                tools=[_ROUND1_TOOL],
                tool_choice={"type": "tool", "name": "submit_top5_analysis"},
                messages=[{"role": "user", "content": prompt}],
            )
            for block in response.content:
                if block.type == "tool_use" and block.name == "submit_top5_analysis":
                    return block.input
            raise ValueError("Claude returned no tool_use block")

        except anthropic.RateLimitError:
            wait = CLAUDE_RETRY_DELAY * (2 ** (attempt - 1))
            print(f"  ⏳ Rate limit. Waiting {wait}s (attempt {attempt}/{CLAUDE_RETRY_COUNT})")
            time.sleep(wait)

        except anthropic.APIError as e:
            wait = CLAUDE_RETRY_DELAY * attempt
            print(f"  ⚠️  API error (attempt {attempt}): {e}. Retrying in {wait}s...")
            time.sleep(wait)

    raise RuntimeError(f"Claude API failed after {CLAUDE_RETRY_COUNT} attempts")


def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Claude Round 1 (Haiku) – {today_str}")
    print(f"{'='*60}")
    print(f"  Model: {CLAUDE_MODEL_R1}")

    scores = load_scores(today_str)
    prompt = build_prompt(scores)

    print(f"📤 Sending top {len(scores['top20'])} scored positions to Claude Haiku...")

    result = call_claude_with_retry(prompt)

    for stock in result.get("top5", []):
        stock["ticker"] = normalize_ticker(stock.get("ticker", ""))
        # Backward-compat safety net: keep conviction_score in sync with
        # alpha_score even if the model only filled one of the two.
        if stock.get("conviction_score") is None:
            stock["conviction_score"] = stock.get("alpha_score")
        elif stock.get("alpha_score") is None:
            stock["alpha_score"] = stock.get("conviction_score")

    print(f"✅ Claude identified top 5:")
    for stock in result.get("top5", []):
        print(f"   {stock['rank']}. {stock['ticker']} – {stock['thesis'][:60]}...")

    output_path = DATA_DIR / f"{today_str}_claude_round1.json"
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"💾 Saved to {output_path}")


if __name__ == "__main__":
    run()
