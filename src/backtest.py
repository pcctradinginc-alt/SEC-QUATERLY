"""
backtest.py
Tracks historical performance of past recommendations.

For each past final_analysis.json, checks what the underlying stock
did 90 and 180 days after the report date. This is the only way to
know if the system generates alpha or just looks good.

Saved to data/holdings/{today}_backtest.json and embedded in the report.
"""

import json
from datetime import date, timedelta
from pathlib import Path

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False

from config import DATA_DIR, run_date


def load_all_analyses() -> list[dict]:
    files = sorted(DATA_DIR.glob("*_final_analysis.json"))
    results = []
    for f in files:
        try:
            with open(f) as fh:
                results.append(json.load(fh))
        except (json.JSONDecodeError, OSError):
            continue
    return results


def _yf_ticker(ticker: str) -> str:
    """Convert Tradier-style tickers to yfinance format. BRK/B → BRK-B."""
    return ticker.replace("/", "-")


_BENCH = "SPY"
_bench_cache: dict[str, dict] = {}


def benchmark_return(signal_date_str: str, days: int) -> float | None:
    """S&P 500 (SPY) return over the same window, so a signal's return can be
    read against the market rather than in isolation."""
    key = f"{signal_date_str}:{days}"
    if key in _bench_cache:
        return _bench_cache[key]["ret"]
    perf = check_stock_performance(_BENCH, signal_date_str)
    val = perf.get(f"return_d{days}_pct")
    _bench_cache[key] = {"ret": val}
    return val


def check_stock_performance(ticker: str, signal_date_str: str) -> dict:
    """
    Downloads price history for ticker starting from signal_date.
    Returns price at signal, +90d, +180d, and % returns.
    """
    if not YF_AVAILABLE:
        return {"error": "yfinance_not_installed"}

    try:
        signal_date = date.fromisoformat(signal_date_str)
        # Don't request data past today
        end_date = min(signal_date + timedelta(days=200), date.today())

        if end_date <= signal_date:
            return {"status_d90": "pending", "status_d180": "pending"}

        hist = yf.Ticker(_yf_ticker(ticker)).history(
            start=signal_date.isoformat(),
            end=end_date.isoformat(),
        )

        if hist.empty:
            return {"error": "no_price_data"}

        price_start = float(hist["Close"].iloc[0])
        result: dict = {
            "signal_date":      signal_date_str,
            "price_at_signal":  round(price_start, 2),
        }

        for days, label in [(90, "d90"), (180, "d180")]:
            target = signal_date + timedelta(days=days)
            if target > date.today():
                result[f"price_{label}"]       = None
                result[f"return_{label}_pct"]  = None
                result[f"status_{label}"]      = "pending"
            else:
                future = hist[hist.index.date >= target]
                if not future.empty:
                    p   = float(future["Close"].iloc[0])
                    ret = ((p - price_start) / price_start) * 100
                    result[f"price_{label}"]       = round(p, 2)
                    result[f"return_{label}_pct"]  = round(ret, 1)
                    result[f"status_{label}"]      = "win" if ret > 0 else "loss"
                else:
                    result[f"price_{label}"]       = None
                    result[f"return_{label}_pct"]  = None
                    result[f"status_{label}"]      = "no_data"

        return result

    except Exception as e:
        return {"error": str(e)}


def run() -> dict:
    today_str = run_date()

    print(f"\n{'='*60}")
    print(f"Backtesting Engine – {today_str}")
    print(f"{'='*60}")

    analyses = load_all_analyses()
    if not analyses:
        print("No past analyses found. Skipping backtest.")
        return {"summary": {}, "results": []}

    rows = []
    for analysis in analyses:
        report_date = analysis.get("date", "")
        for stock in analysis.get("top10") or analysis.get("round1_top5", []):
            ticker = stock.get("ticker", "")
            if not ticker:
                continue
            print(f"  📊 {ticker} @ {report_date}...", end=" ", flush=True)
            perf = check_stock_performance(ticker, report_date)
            ret90 = perf.get("return_d90_pct")
            print(f"{f'+{ret90}%' if ret90 and ret90 > 0 else (f'{ret90}%' if ret90 is not None else perf.get('status_d90', '?'))}")
            b90  = benchmark_return(report_date, 90)
            b180 = benchmark_return(report_date, 180)
            r90  = perf.get("return_d90_pct")
            r180 = perf.get("return_d180_pct")
            rows.append({
                "report_date":      report_date,
                "engine":           "v2_deterministic" if analysis.get("top10") else "v1_llm_top5",
                "benchmark_d90_pct":  b90,
                "benchmark_d180_pct": b180,
                "excess_d90_pct":   round(r90 - b90, 1) if (r90 is not None and b90 is not None) else None,
                "excess_d180_pct":  round(r180 - b180, 1) if (r180 is not None and b180 is not None) else None,
                "ticker":           ticker,
                "company":          stock.get("company_name") or stock.get("name", ""),
                "signal_score":     stock.get("signal_score", stock.get("conviction_score")),
                "primary_flag":     stock.get("grade") or stock.get("primary_flag", ""),
                "insider_score":    (stock.get("factors") or {}).get("insider"),
                **perf,
            })

    # Summary statistics (only on completed, non-error rows)
    done_90  = [r for r in rows if r.get("status_d90")  in ("win", "loss")]
    done_180 = [r for r in rows if r.get("status_d180") in ("win", "loss")]

    def _win_rate(subset, key):
        if not subset:
            return None
        return round(sum(1 for r in subset if r.get(key) == "win") / len(subset) * 100, 1)

    def _avg_ret(subset, key):
        vals = [r[key] for r in subset if r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def _avg(subset, key):
        vals = [r[key] for r in subset if r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def _beat(subset):
        pairs = [(r["return_d90_pct"], r["benchmark_d90_pct"]) for r in subset
                 if r.get("return_d90_pct") is not None and r.get("benchmark_d90_pct") is not None]
        return round(sum(1 for a, b in pairs if a > b) / len(pairs) * 100, 1) if pairs else None

    by_engine = {}
    for eng in sorted({r.get("engine", "unknown") for r in rows}):
        sub = [r for r in done_90 if r.get("engine") == eng]
        if sub:
            by_engine[eng] = {
                "signals":            len(sub),
                "win_rate_90d_pct":   _win_rate(sub, "status_d90"),
                "avg_return_90d_pct": _avg_ret(sub, "return_d90_pct"),
                "avg_benchmark_90d_pct": _avg(sub, "benchmark_d90_pct"),
                "avg_excess_90d_pct": _avg(sub, "excess_d90_pct"),
                "beat_benchmark_pct": _beat(sub),
            }

    summary = {
        "generated_at":       today_str,
        "benchmark":          _BENCH,
        "avg_benchmark_90d_pct": _avg(done_90, "benchmark_d90_pct"),
        "avg_excess_90d_pct":    _avg(done_90, "excess_d90_pct"),
        "beat_benchmark_90d_pct": _beat(done_90),
        "by_engine":          by_engine,
        "total_signals":      len(rows),
        "completed_90d":      len(done_90),
        "completed_180d":     len(done_180),
        "win_rate_90d_pct":   _win_rate(done_90,  "status_d90"),
        "win_rate_180d_pct":  _win_rate(done_180, "status_d180"),
        "avg_return_90d_pct": _avg_ret(done_90,  "return_d90_pct"),
        "avg_return_180d_pct":_avg_ret(done_180, "return_d180_pct"),
    }

    output = {"summary": summary, "results": rows}

    out_path = DATA_DIR / f"{today_str}_backtest.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n{'─'*40}")
    print(f"BACKTEST SUMMARY ({len(rows)} signals tracked)")
    if summary["win_rate_90d_pct"] is not None:
        print(f"  90d  win rate : {summary['win_rate_90d_pct']}%  "
              f"(avg {summary['avg_return_90d_pct']:+.1f}%)")
    if summary["win_rate_180d_pct"] is not None:
        print(f"  180d win rate : {summary['win_rate_180d_pct']}%  "
              f"(avg {summary['avg_return_180d_pct']:+.1f}%)")
    print(f"✅ Saved to {out_path}")

    return output


if __name__ == "__main__":
    run()
