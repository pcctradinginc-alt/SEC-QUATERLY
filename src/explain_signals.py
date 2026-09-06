"""
explain_signals.py
Turns the deterministic Top-10 (+ selected Calls) into the final analysis
file, adding a short LLM narrative per stock.

Cost design
-----------
* ONE batched call covers all ten stocks (market context + thesis + risks +
  option note), routed through llm_router (Haiku first, cascade to Sonnet /
  Opus only if the structured output fails validation).
* The prompt contains only pre-computed numbers; the model interprets, it
  never ranks or re-scores. Ranking and scores come from signal_engine.py.
* Output is cached by content hash, so re-running on the same data is free.
* If no API credentials / budget / valid output exist, the rule-based
  explanation from signal_engine.py is used verbatim - the report always ships.
"""

import json
import re
import sys
from datetime import date

from config import run_date, DATA_DIR, FILERS, NO_SUITABLE_OPTION, SIGNAL_WEIGHTS, TOP_N
import llm_router


# ── prompt building ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You write concise, sober commentary for a quantitative 13F + Form 4 signal report.

Ground rules:
- The ranking and every number are pre-computed by a deterministic engine. Do not re-rank, re-score, or invent figures. Only interpret the data you are given.
- 13F data shows long US equity positions with up to a 45-day lag; weights use long-only reported AUM and are overstated for diversified managers. Say "increased its reported long position", never "is bullish".
- Form 4 open-market purchases (code P) since the quarter-end are the only insider confirmation used. If buy value is 0, say plainly that no insider confirmation exists yet.
- Be willing to call a setup routine. Never inflate. No investment advice language ("buy", "should").
- Use ONLY the figures given. Never compute new percentages, sums or aggregates, and never round a figure into a different number: if sales are $451,600,000 write $451.6M, never $471M.
- State the insider picture exactly as the net stance says. If sales are above zero you may not write that there were no sales; if purchases are zero you may not write that insiders bought.
- Trade counts are given. Do not guess how many trades there were.
- Keep each field within its length limit. Plain text, no markdown.

Score model (weights out of 100): {json.dumps(SIGNAL_WEIGHTS)}.
Grades: VERY_STRONG ≥75, STRONG ≥60, MODERATE ≥45, WEAK <45."""


def _fmt_stock(s: dict, opt: dict | None) -> str:
    f = s["factors"]
    ins = (s.get("insider") or {}).get("summary") or {}
    buyers = "; ".join(
        f"{r['filer']} ({r['delta_type']}, wt {r['port_weight_pct']:.1f}%"
        + (f", Δ{r['delta_pct']:+.0f}%" if r.get("delta_pct") is not None else "")
        + f", q={r.get('manager_quality_score', 0):.2f})"
        for r in s["filers"]
    )
    perf = s.get("post_filing_perf") or {}
    perf_txt = (f"{perf['pct_change']:+.0f}% since quarter-end" if perf.get("pct_change") is not None else "n/a")
    if ins:
        ins_txt = (
            f"net stance {ins.get('net_stance', '?')}; "
            f"purchases ${ins.get('buy_value_usd', 0):,.0f} in {ins.get('buy_count', 0)} trade(s) "
            f"by {len(ins.get('distinct_buyers', []))} insider(s) "
            f"(officer/director involved: {'yes' if ins.get('officer_or_director_buyers') else 'no'}); "
            f"sales ${ins.get('sell_value_usd', 0):,.0f} in {ins.get('sell_count', 0)} trade(s)"
        )
    else:
        ins_txt = "no Form 4 common-stock activity"
    if opt and opt.get("status") == "OK":
        c = opt["contract"]
        opt_txt = (f"{c['symbol']} strike {c['strike']} exp {c['expiration']} ({c['dte']}d) delta {c['delta']} "
                   f"mid ${c['mid']} spread {c['spread_pct']}% vol {c['volume']} OI {c['open_interest']}"
                   + (f" IV rank {opt['iv_metrics'].get('iv_rank')}" if opt.get("iv_metrics") else ""))
    elif opt:
        opt_txt = f"{NO_SUITABLE_OPTION} (rejections: {opt.get('rejections')})"
    else:
        opt_txt = "not evaluated"
    return (
        f"#{s['rank']} {s['ticker']} ({s['name']}) score {s['signal_score']} grade {s['grade']}\n"
        f"  factors: activity {f['activity']}, conviction {f['conviction']}, manager_quality {f['manager_quality']}, "
        f"accumulation {f['accumulation']}, consensus {f['consensus']}, insider {f['insider']}, "
        f"freshness {f['freshness']}, crowding {f['crowding']}; price penalty {s['price_penalty']}\n"
        f"  buyers: {buyers}\n"
        f"  multi-quarter: {(s.get('mq_signal') or {}).get('build_quarters', 0)} prior build quarters\n"
        f"  price: {perf_txt}; crowding label {s.get('crowding_label')}\n"
        f"  insider: {ins_txt}\n"
        f"  engine reasons: {' | '.join(s['why']['bullets'])}\n"
        f"  call option: {opt_txt}\n"
    )


def build_user_prompt(signals: dict, options: dict | None) -> str:
    opts = (options or {}).get("options", {})
    blocks = [_fmt_stock(s, opts.get(s["ticker"])) for s in signals["top10"]]
    return (
        f"ANALYSIS DATE {signals['date']} – {len(FILERS)} tracked 13F filers.\n"
        f"TOP {len(blocks)} BY DETERMINISTIC SIGNAL SCORE:\n\n" + "\n".join(blocks) +
        "\nWrite the commentary using the submit_commentary tool. Cover every ticker exactly once, in the given order."
    )


TOOL = {
    "name": "submit_commentary",
    "description": "Commentary for the Top-10 signal report.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["market_context", "stocks"],
        "properties": {
            "market_context": {"type": "string", "description": "2 sentences on what this quarter's filings show in aggregate. ≤ 60 words."},
            "stocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ticker", "why_strongest", "insider_read", "risks", "option_note", "verdict"],
                    "properties": {
                        "ticker":       {"type": "string"},
                        "why_strongest": {"type": "string", "description": "2-3 sentences: why this qualifies as one of the strongest signals, citing the given factors. ≤ 80 words."},
                        "insider_read": {"type": "string", "description": "1 sentence on whether Form 4 activity confirms the 13F signal. ≤ 35 words."},
                        "risks":        {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3, "description": "Short bullets incl. possible misreads."},
                        "option_note":  {"type": "string", "description": "1 sentence on how the selected Call expresses the signal, or why none qualified. ≤ 40 words."},
                        "verdict":      {"type": "string", "enum": ["UNUSUALLY_STRONG", "STRONG", "ROUTINE"]},
                    },
                },
            },
        },
    },
}


_NO_SALES_CLAIMS = (
    "no sales", "no insider sales", "no selling", "nor sales", "no offsetting sales",
    "without sales", "no meaningful sales", "minimal sales", "no insider selling",
)
_BOUGHT_CLAIMS = ("insiders bought", "insiders purchased", "insiders added", "insider buying confirmed")


def _money_figures(text: str) -> list[float]:
    """Every $ amount in the text, normalised to dollars ($1.4M -> 1_400_000)."""
    out = []
    for num, suffix in re.findall(r"\$\s*([\d,]+(?:\.\d+)?)\s*([KMB]?)", text or "", re.I):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        out.append(v * {"k": 1e3, "m": 1e6, "b": 1e9}.get(suffix.lower(), 1.0))
    return out


def _insider_facts_ok(text: str, ins: dict) -> tuple[bool, str]:
    """Reject narrative that contradicts the deterministic Form 4 numbers."""
    low = (text or "").lower()
    buy, sell = ins.get("buy_value_usd", 0) or 0, ins.get("sell_value_usd", 0) or 0

    if sell > 0 and any(c in low for c in _NO_SALES_CLAIMS):
        return False, f"claims no insider sales but ${sell:,.0f} was sold"
    if buy == 0 and any(c in low for c in _BOUGHT_CLAIMS):
        return False, "claims insider buying but purchases are zero"

    allowed = [buy, sell, abs(buy - sell), 0.0]
    for v in _money_figures(text):
        if not any(abs(v - a) <= max(a * 0.02, 1.0) for a in allowed):
            return False, f"dollar figure ${v:,.0f} matches no Form 4 total"
    return True, "ok"


def make_validator(expected_tickers: list[str], insider_by_ticker: dict[str, dict] | None = None):
    insider_by_ticker = insider_by_ticker or {}

    def validate(data: dict) -> tuple[bool, str]:
        if not isinstance(data, dict):
            return False, "not an object"
        stocks = data.get("stocks")
        if not isinstance(stocks, list) or len(stocks) != len(expected_tickers):
            return False, f"expected {len(expected_tickers)} stocks, got {len(stocks) if isinstance(stocks, list) else 'none'}"
        got = [str(s.get("ticker", "")).upper().replace(".", "/") for s in stocks]
        if sorted(got) != sorted(t.upper() for t in expected_tickers):
            return False, f"ticker mismatch {got}"
        for s in stocks:
            if len(str(s.get("why_strongest", ""))) < 40:
                return False, f"{s.get('ticker')}: why_strongest too short"
            if len(str(s.get("why_strongest", "")).split()) > 120:
                return False, f"{s.get('ticker')}: why_strongest too long"
            if not isinstance(s.get("risks"), list) or not (2 <= len(s["risks"]) <= 3):
                return False, f"{s.get('ticker')}: risks count"
            if s.get("verdict") not in ("UNUSUALLY_STRONG", "STRONG", "ROUTINE"):
                return False, f"{s.get('ticker')}: verdict"
            low = str(s.get("why_strongest", "")).lower()
            if any(w in low for w in (" should buy", "strong buy", "must buy")):
                return False, f"{s.get('ticker')}: advice language"

            ins = insider_by_ticker.get(str(s.get("ticker", "")).upper())
            if ins is not None:
                for field in ("insider_read", "why_strongest"):
                    ok, why = _insider_facts_ok(str(s.get(field, "")), ins)
                    if not ok:
                        return False, f"{s.get('ticker')}: {field} {why}"
        mc = str(data.get("market_context", ""))
        if len(mc) < 20 or len(mc.split()) > 90:
            return False, "market_context length"
        return True, "ok"
    return validate


# ── rule-based fallback ───────────────────────────────────────────────────────

def fallback_commentary(signals: dict, options: dict | None) -> dict:
    opts = (options or {}).get("options", {})
    stocks = []
    for s in signals["top10"]:
        ins = (s.get("insider") or {}).get("summary") or {}
        buy_val = ins.get("buy_value_usd", 0) or 0
        # option_note left empty: the report already prints the rule rationale
        # (OK) or the per-filter rejection counts (NO_SUITABLE_OPTION_FOUND).
        option_note = ""
        stocks.append({
            "ticker":        s["ticker"],
            "why_strongest": s["why"]["summary"],
            "insider_read":  (f"Form 4 filings show ${buy_val:,.0f} of open-market insider buying since quarter-end."
                              if buy_val > 0 else "No open-market insider purchases confirm the 13F signal yet."),
            "risks":         ["13F data is up to 45 days stale and shows no shorts, hedges or cash.",
                              "Reported weights use long-only AUM and overstate true portfolio exposure."],
            "option_note":   option_note,
            "verdict":       "STRONG" if s["grade"] in ("VERY_STRONG", "STRONG") else "ROUTINE",
        })
    n_buy = sum(1 for s in signals["top10"] if (s["factors"]["insider"] or 0) > 0)
    return {
        "market_context": (f"{len(FILERS)} tracked filers were screened; the ten strongest names are led by "
                           f"{signals['top10'][0]['ticker']} (score {signals['top10'][0]['signal_score']}). "
                           f"{n_buy} of the ten carry confirming insider purchases since quarter-end."),
        "stocks": stocks,
        "_source": "rule_based",
    }


# ── main ──────────────────────────────────────────────────────────────────────

def run(today_str: str | None = None) -> dict:
    today_str = today_str or run_date()
    print(f"\n{'='*60}\nCommentary (cost-routed) – {today_str}\n{'='*60}")

    signals = json.load(open(DATA_DIR / f"{today_str}_signals.json"))
    opt_path = DATA_DIR / f"{today_str}_options.json"
    options = json.load(open(opt_path)) if opt_path.exists() else None
    if options is None:
        print("  ℹ️  no options file – narratives will say 'not evaluated'")

    top = signals["top10"]
    tickers = [s["ticker"] for s in top]
    insider_summaries = {
        s["ticker"].upper(): ((s.get("insider") or {}).get("summary") or {}) for s in top
    }

    commentary, meta = llm_router.route(
        task="explain_signals",
        system=SYSTEM_PROMPT,
        user=build_user_prompt(signals, options),
        tool=TOOL,
        validator=make_validator(tickers, insider_summaries),
        max_tokens=6000,
    )
    if commentary is None:
        print("  ⚠️  LLM commentary unavailable – using rule-based explanations")
        commentary = fallback_commentary(signals, options)
    else:
        commentary["_source"] = meta.get("model")
        print(f"  ✅ commentary from {meta.get('model')}" + (" (cache)" if meta.get("cached") else "")
              + f" – est. ${meta.get('cost_usd', 0):.4f}")

    by_ticker = {c["ticker"].upper().replace(".", "/"): c for c in commentary["stocks"]}
    opts = (options or {}).get("options", {})
    merged = []
    for s in top:
        c = by_ticker.get(s["ticker"].upper(), {})
        o = opts.get(s["ticker"]) or {"status": "NOT_EVALUATED", "contract": None, "rejections": {}}
        merged.append({**s, "commentary": c, "option": o})

    # Sell-side context from scoring.py (informational)
    sell_signals = []
    try:
        sell_signals = json.load(open(DATA_DIR / f"{today_str}_scores.json")).get("sell_signals", [])[:10]
    except (OSError, json.JSONDecodeError):
        pass

    # Quarter actually analysed (13F period-of-report), and how many names the
    # deterministic engine scored before the Top 10 was cut.
    report_dates = [f.get("report_date") for s in top for f in s.get("filers", []) if f.get("report_date")]
    quarter_label = ""
    if report_dates:
        rd = max(report_dates)
        quarter_label = f"Q{(int(rd[5:7]) - 1) // 3 + 1} {rd[:4]} filings"

    final = {
        "date":               today_str,
        "quarter_label":      quarter_label,
        "stocks_scored":      len(signals.get("ranking", [])),
        "top10":              merged,
        "market_context":     commentary.get("market_context", ""),
        "commentary_source":  commentary.get("_source"),
        "llm":                {**llm_router.budget().summary(), "route": meta.get("attempts", [])},
        "weights":            SIGNAL_WEIGHTS,
        "filters":            (options or {}).get("filters", {}),
        "input_fingerprint":  signals.get("input_fingerprint"),
        "ranking_fingerprint": signals.get("ranking_fingerprint"),
        "sell_signals":       sell_signals,
        "filer_count":        len(FILERS),
        "no_option_label":    NO_SUITABLE_OPTION,
        "disclaimer": ("Automated research summary built from public SEC 13F and Form 4 filings and delayed "
                       "option quotes. Not investment advice. Options can expire worthless."),
    }
    out = DATA_DIR / f"{today_str}_final_analysis.json"
    with open(out, "w") as f:
        json.dump(final, f, indent=2, default=str)
    print(f"💾 Final analysis saved to {out}")
    return final


if __name__ == "__main__":
    run()
