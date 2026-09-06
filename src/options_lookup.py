"""
options_lookup.py
Fetches real Call chains from Tradier for the deterministic Top-10 and
applies the predefined filters via option_selector.select_call().

Output per ticker:
    status          "OK" | "NO_SUITABLE_OPTION_FOUND"
    contract        the single selected Call (or null)
    rejections      {filter_name: count} for transparency
    iv_metrics      IV rank vs. realised-vol proxy (context only)

There is deliberately NO relaxed fallback: if no contract passes every
filter, the stock is reported with NO_SUITABLE_OPTION_FOUND.
"""

import json
import os
from datetime import date, timedelta

import requests

from config import (
    run_date,
    DATA_DIR, NO_SUITABLE_OPTION, OPTION_MAX_DAYS, OPTION_MIN_DAYS, TRADIER_BASE_URL,
)
import option_selector


def compute_iv_rank(ticker: str, current_atm_iv: float | None) -> dict:
    """IV Rank proxy from 52 weeks of realised vol (context for the reader; not a filter)."""
    if current_atm_iv is None:
        return {}
    try:
        import numpy as np
        import yfinance as yf

        yf_t  = ticker.replace("/", "-")
        start = (date.today() - timedelta(days=400)).isoformat()
        hist  = yf.download(yf_t, start=start, end=date.today().isoformat(),
                            auto_adjust=True, progress=False)
        if hist.empty or len(hist) < 63:
            return {}
        closes = hist["Close"].squeeze().dropna()
        log_r  = np.log(closes / closes.shift(1)).dropna()
        roll_hv = log_r.rolling(21).std() * np.sqrt(252)
        hv_52w = roll_hv.tail(252).dropna()
        if len(hv_52w) < 20:
            return {}
        low, high = float(hv_52w.min()), float(hv_52w.max())
        hv_30d = float(roll_hv.iloc[-1])
        if high <= low:
            return {}
        iv_rank = max(0.0, min(100.0, (current_atm_iv - low) / (high - low) * 100))
        verdict = ("EXPENSIVE – premium rich vs. realised vol" if iv_rank >= 70
                   else "MODERATE" if iv_rank >= 40 else "CHEAP – premium low vs. realised vol")
        return {
            "iv_rank":     round(iv_rank, 1),
            "hv_30d_pct":  round(hv_30d * 100, 1),
            "iv_hv_ratio": round(current_atm_iv / hv_30d, 2) if hv_30d > 0 else None,
            "verdict":     verdict,
        }
    except Exception as e:
        print(f"    ⚠️  IV rank calc failed for {ticker}: {e}")
        return {}


def market_status(headers: dict) -> dict:
    """
    Tradier's clock, so the report can say whether the option quotes are live or
    a weekend snapshot. A chain pulled while the market is shut shows near-zero
    volume for every strike, which must not be read as illiquidity.
    """
    from datetime import datetime, timezone
    info = {"state": "unknown", "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "description": ""}
    try:
        r = requests.get(f"{TRADIER_BASE_URL}/markets/clock", headers=headers, timeout=10)
        r.raise_for_status()
        clock = (r.json() or {}).get("clock") or {}
        info["state"] = (clock.get("state") or "unknown").lower()
        info["description"] = clock.get("description", "")
        if clock.get("date"):
            info["trading_day"] = clock["date"]
    except Exception as e:
        print(f"  ⚠️  Market clock unavailable: {e}")
    return info


def get_headers() -> dict:
    api_key = os.environ.get("TRADIER_API_KEY", "")
    if not api_key:
        raise ValueError("TRADIER_API_KEY environment variable not set")
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def normalize_ticker_for_tradier(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "/")


def get_stock_quotes(tickers: list[str], headers: dict) -> dict[str, dict]:
    url = f"{TRADIER_BASE_URL}/markets/quotes"
    try:
        resp = requests.get(url, params={"symbols": ",".join(tickers), "greeks": "false"},
                            headers=headers, timeout=15)
        resp.raise_for_status()
        quotes = resp.json().get("quotes", {}).get("quote", [])
        if isinstance(quotes, dict):
            quotes = [quotes]
        return {q["symbol"]: q for q in quotes if "symbol" in q}
    except Exception as e:
        print(f"  ⚠️  Stock quote fetch failed: {e}")
        return {}


def get_expiration_dates(ticker: str, headers: dict, today: date) -> list[str]:
    url = f"{TRADIER_BASE_URL}/markets/options/expirations"
    try:
        resp = requests.get(url, params={"symbol": ticker, "includeAllRoots": "true", "strikes": "false"},
                            headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    ⚠️  Expiration fetch failed for {ticker}: {e}")
        return []
    dates = (data.get("expirations") or {}).get("date", []) or []
    if isinstance(dates, str):
        dates = [dates]
    lo, hi = today + timedelta(days=OPTION_MIN_DAYS), today + timedelta(days=OPTION_MAX_DAYS)
    return sorted(d for d in dates if lo <= date.fromisoformat(d) <= hi)


def get_option_chain(ticker: str, expiry: str, headers: dict) -> list[dict]:
    url = f"{TRADIER_BASE_URL}/markets/options/chains"
    try:
        resp = requests.get(url, params={"symbol": ticker, "expiration": expiry, "greeks": "true"},
                            headers=headers, timeout=20)
        resp.raise_for_status()
        options = resp.json().get("options") or {}
    except Exception as e:
        print(f"    ⚠️  Chain fetch failed for {ticker} {expiry}: {e}")
        return []
    chain = options.get("option", []) if options else []
    return [chain] if isinstance(chain, dict) else chain


def lookup_ticker(ticker: str, headers: dict, quote: dict | None, today: date) -> dict:
    tradier_ticker = normalize_ticker_for_tradier(ticker)
    spot = (quote or {}).get("last")
    print(f"  📈 {ticker} – spot ${spot}")

    expiries = get_expiration_dates(tradier_ticker, headers, today)
    if not expiries:
        print(f"    ⚠️  no expiries in {OPTION_MIN_DAYS}–{OPTION_MAX_DAYS}d window")
        return {"ticker": ticker, "current_price": spot, "status": NO_SUITABLE_OPTION,
                "contract": None, "rejections": {"expiry_days": 0}, "expiries_checked": [],
                "note": "no expiries in window", "iv_metrics": {}}

    full_chain: list[dict] = []
    for exp in expiries:
        chain = get_option_chain(tradier_ticker, exp, headers)
        full_chain.extend(c for c in chain if (c.get("option_type") or "").lower() == "call")
    print(f"    {len(expiries)} expiries, {len(full_chain)} call contracts")

    selection = option_selector.select_call(full_chain, today, spot)
    if selection["status"] == "OK":
        c = selection["contract"]
        print(f"    ✅ {c['symbol']}  Δ{c['delta']}  mid ${c['mid']}  spread {c['spread_pct']}%  "
              f"vol {c['volume']} / OI {c['open_interest']}  ({selection['eligible_count']} eligible)")
    else:
        print(f"    ❌ {NO_SUITABLE_OPTION}  rejections={selection['rejections']}")

    # IV rank context from the ATM contract (any expiry)
    atm_iv = None
    if spot and full_chain:
        with_iv = [c for c in full_chain if (c.get("greeks") or {}).get("smv_vol") or (c.get("greeks") or {}).get("mid_iv")]
        if with_iv:
            atm = min(with_iv, key=lambda c: (abs(float(c.get("strike") or 0) - float(spot)), c.get("symbol", "")))
            g = atm.get("greeks") or {}
            try:
                atm_iv = float(g.get("smv_vol") or g.get("mid_iv"))
            except (TypeError, ValueError):
                atm_iv = None

    return {
        "ticker":           ticker,
        "current_price":    spot,
        "change_pct":       (quote or {}).get("change_percentage"),
        "expiries_checked": expiries,
        "iv_metrics":       compute_iv_rank(ticker, atm_iv),
        **selection,
    }


def run(today_str: str | None = None) -> dict:
    today_str = today_str or run_date()
    today = date.fromisoformat(today_str)
    print(f"\n{'='*60}\nTradier Call Selection – {today_str}\n{'='*60}")

    sig_path = DATA_DIR / f"{today_str}_signals.json"
    if not sig_path.exists():
        raise FileNotFoundError(f"Signals not found: {sig_path}")
    signals = json.load(open(sig_path))
    top = signals.get("top10", [])
    if not top:
        raise ValueError("Signal engine produced no Top-10")

    headers = get_headers()
    clock = market_status(headers)
    print(f"🕒 Market is {clock['state'].upper()}"
          + (f" – {clock['description']}" if clock.get("description") else ""))
    if clock["state"] != "open":
        print("   Option volume reflects the last session, not live trading.")
    tickers = [s["ticker"] for s in top]
    quotes = get_stock_quotes([normalize_ticker_for_tradier(t) for t in tickers], headers)

    results = {}
    for s in top:
        t = s["ticker"]
        results[t] = lookup_ticker(t, headers, quotes.get(normalize_ticker_for_tradier(t)), today)

    output = {"date": today_str, "tickers": tickers, "options": results,
              "market": clock,
              "filters": option_selector.FILTERS_DESCRIPTION}
    out = DATA_DIR / f"{today_str}_options.json"
    with open(out, "w") as f:
        json.dump(output, f, indent=2, default=str)
    ok = sum(1 for r in results.values() if r["status"] == "OK")
    print(f"\n✅ Options saved to {out}  ({ok}/{len(results)} with a suitable Call)")
    return output


if __name__ == "__main__":
    run()
