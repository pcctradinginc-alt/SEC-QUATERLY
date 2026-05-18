"""
options_lookup.py
Fetches real options chains from Tradier for the Top 5 tickers.

Improvements:
  - Spread filter tightened: 15% → 8% (OPTION_MAX_SPREAD_PCT)
  - Volume threshold raised: 100 → 300 (OPTION_MIN_VOLUME)
  - IV filter: skip options with IV > 70% (OPTION_MAX_IV) – avoids
    buying expensive premium on high-IV names like NVDA/PLTR
  - OTM warning when strike > +10% above spot
  - Greeks=None pre-market → fallback to volume/OI filter
  - Empty results handled with relaxed fallback
"""

import json
import os
from datetime import date, timedelta

import requests

from config import (
    DATA_DIR, OPTION_DELTA_MAX, OPTION_DELTA_MIN,
    OPTION_MAX_DAYS, OPTION_MAX_IV, OPTION_MAX_SPREAD_PCT,
    OPTION_MIN_DAYS, OPTION_MIN_VOLUME, TRADIER_BASE_URL,
)


def get_headers() -> dict:
    api_key = os.environ.get("TRADIER_API_KEY", "")
    if not api_key:
        raise ValueError("TRADIER_API_KEY environment variable not set")
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def get_stock_quotes(tickers: list[str], headers: dict) -> dict[str, dict]:
    url    = f"{TRADIER_BASE_URL}/markets/quotes"
    params = {"symbols": ",".join(tickers), "greeks": "false"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        quotes = resp.json().get("quotes", {}).get("quote", [])
        if isinstance(quotes, dict):
            quotes = [quotes]
        return {q["symbol"]: q for q in quotes if "symbol" in q}
    except Exception as e:
        print(f"  ⚠️  Stock quote fetch failed: {e}")
        return {}


def normalize_ticker_for_tradier(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "/")


def get_expiration_dates(ticker: str, headers: dict) -> list[str]:
    url    = f"{TRADIER_BASE_URL}/markets/options/expirations"
    params = {"symbol": ticker, "includeAllRoots": "true", "strikes": "false"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    ⚠️  Expiration fetch failed for {ticker}: {e}")
        return []

    dates     = data.get("expirations", {})
    date_list = dates.get("date", []) if dates else []
    if isinstance(date_list, str):
        date_list = [date_list]

    today    = date.today()
    min_date = today + timedelta(days=OPTION_MIN_DAYS)
    max_date = today + timedelta(days=OPTION_MAX_DAYS)

    return [d for d in date_list if min_date <= date.fromisoformat(d) <= max_date]


def get_option_chain(ticker: str, expiry: str, headers: dict) -> list[dict]:
    url    = f"{TRADIER_BASE_URL}/markets/options/chains"
    params = {"symbol": ticker, "expiration": expiry, "greeks": "true"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    ⚠️  Chain fetch failed for {ticker} {expiry}: {e}")
        return []

    options = data.get("options", {})
    if not options:
        return []

    chain = options.get("option", [])
    return [chain] if isinstance(chain, dict) else chain


def filter_options(chain: list[dict], direction: str = "BULLISH",
                   current_price: float | None = None) -> list[dict]:
    """
    Filters to liquid, reasonably priced options matching our criteria.

    Key changes from original:
      - spread ≤ OPTION_MAX_SPREAD_PCT (8%, was 15%)
      - volume  ≥ OPTION_MIN_VOLUME (300, was 100)
      - IV      ≤ OPTION_MAX_IV (70%) when available
      - OTM warning added as metadata when strike > spot + 10%
    """
    option_type      = "call" if direction == "BULLISH" else "put"
    candidates       = []
    greeks_available = False

    for opt in chain:
        if opt.get("option_type", "").lower() != option_type:
            continue

        volume = opt.get("volume", 0) or 0
        oi     = opt.get("open_interest", 0) or 0

        # Require meaningful liquidity
        if volume < OPTION_MIN_VOLUME and oi < 500:
            continue

        bid = opt.get("bid") or 0
        ask = opt.get("ask") or 0
        mid = (bid + ask) / 2

        if mid <= 0:
            continue

        # Strict spread filter
        spread_ratio = (ask - bid) / mid
        if spread_ratio > OPTION_MAX_SPREAD_PCT / 100:
            continue

        greeks = opt.get("greeks") or {}
        delta  = greeks.get("delta")
        iv     = greeks.get("smv_vol") or greeks.get("mid_iv")

        if delta is not None:
            greeks_available = True
            if not (OPTION_DELTA_MIN <= abs(float(delta)) <= OPTION_DELTA_MAX):
                continue

        # Skip overpriced premium (high IV)
        if iv is not None:
            try:
                if float(iv) > OPTION_MAX_IV:
                    continue
            except (TypeError, ValueError):
                pass

        strike     = opt.get("strike")
        spread_pct = round(spread_ratio * 100, 1)

        # OTM warning: flag if strike is >10% above current price
        otm_warning = False
        if strike and current_price and current_price > 0:
            otm_pct = ((float(strike) / current_price) - 1) * 100
            otm_warning = otm_pct > 10.0

        candidates.append({
            "symbol":           opt.get("symbol"),
            "option_type":      opt.get("option_type"),
            "strike":           strike,
            "expiration_date":  opt.get("expiration_date"),
            "bid":              bid,
            "ask":              ask,
            "mid":              round(mid, 2),
            "spread_pct":       spread_pct,
            "last":             opt.get("last"),
            "volume":           volume,
            "open_interest":    oi,
            "implied_volatility": iv,
            "delta":            delta,
            "gamma":            greeks.get("gamma"),
            "theta":            greeks.get("theta"),
            "greeks_available": greeks_available,
            "otm_warning":      otm_warning,
        })

    if not greeks_available and candidates:
        print(f"    ⚠️  Greeks unavailable (pre-market?). Using volume/OI/spread filter only.")

    candidates.sort(key=lambda x: x["volume"] or 0, reverse=True)
    return candidates[:5]


def fetch_options_for_ticker(ticker: str, direction: str, headers: dict,
                             stock_quote: dict | None = None) -> dict:
    """Full options lookup for one ticker across all valid expiry dates."""
    tradier_ticker = normalize_ticker_for_tradier(ticker)
    current_price  = stock_quote.get("last") if stock_quote else None
    change_pct     = stock_quote.get("change_percentage") if stock_quote else None
    print(f"  📈 {ticker} ({tradier_ticker}) – ${current_price} ({change_pct}%) – direction: {direction}")

    expiries = get_expiration_dates(tradier_ticker, headers)
    if not expiries:
        print(f"    ⚠️  No valid expiry dates found for {ticker}")
        return {
            "ticker":        ticker,
            "current_price": current_price,
            "change_pct":    change_pct,
            "direction":     direction,
            "error":         "no_valid_expiries",
            "options":       [],
        }

    print(f"    Valid expiries ({OPTION_MIN_DAYS}-{OPTION_MAX_DAYS} days): {expiries}")

    all_options = []
    for expiry in expiries:
        chain    = get_option_chain(tradier_ticker, expiry, headers)
        filtered = filter_options(chain, direction=direction, current_price=current_price)
        all_options.extend(filtered)
        print(f"    {expiry}: {len(chain)} total → {len(filtered)} after filter "
              f"(spread≤{OPTION_MAX_SPREAD_PCT}%, vol≥{OPTION_MIN_VOLUME}, IV≤{int(OPTION_MAX_IV*100)}%)")

    if not all_options:
        print(f"    ⚠️  No options passed strict filters for {ticker}. Relaxing volume threshold.")
        for expiry in expiries[:2]:
            chain = get_option_chain(tradier_ticker, expiry, headers)
            for opt in chain:
                if opt.get("option_type", "").lower() == ("call" if direction == "BULLISH" else "put"):
                    bid = opt.get("bid") or 0
                    ask = opt.get("ask") or 0
                    mid = (bid + ask) / 2
                    all_options.append({
                        "symbol":           opt.get("symbol"),
                        "option_type":      opt.get("option_type"),
                        "strike":           opt.get("strike"),
                        "expiration_date":  opt.get("expiration_date"),
                        "bid":              bid,
                        "ask":              ask,
                        "mid":              round(mid, 2) if mid > 0 else None,
                        "volume":           opt.get("volume", 0),
                        "open_interest":    opt.get("open_interest", 0),
                        "implied_volatility": None,
                        "delta":            None,
                        "greeks_available": False,
                        "otm_warning":      False,
                        "note":             "fallback_relaxed_filter",
                    })
            if all_options:
                break

    all_options.sort(key=lambda x: x.get("volume") or 0, reverse=True)

    return {
        "ticker":           ticker,
        "current_price":    current_price,
        "change_pct":       change_pct,
        "direction":        direction,
        "expiries_checked": expiries,
        "options":          all_options[:10],
    }


def run():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Tradier Options Lookup – {today_str}")
    print(f"{'='*60}")

    r1_path = DATA_DIR / f"{today_str}_claude_round1.json"
    if not r1_path.exists():
        raise FileNotFoundError(f"Claude Round 1 results not found: {r1_path}")

    with open(r1_path) as f:
        r1 = json.load(f)

    headers = get_headers()
    top5    = r1.get("top5", [])

    if not top5:
        raise ValueError("Claude Round 1 returned no top5 picks")

    tickers      = [s["ticker"] for s in top5]
    stock_quotes = get_stock_quotes(tickers, headers)
    print(f"📊 Live quotes fetched: {list(stock_quotes.keys())}")

    results = {}
    for stock in top5:
        ticker    = stock["ticker"]
        direction = stock.get("direction", "BULLISH")
        quote     = stock_quotes.get(ticker)
        results[ticker] = fetch_options_for_ticker(ticker, direction, headers, stock_quote=quote)

    output = {
        "date":          today_str,
        "top5_tickers":  [s["ticker"] for s in top5],
        "options":       results,
    }

    output_path = DATA_DIR / f"{today_str}_options.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n✅ Options data saved to {output_path}")
    for ticker, data in results.items():
        count = len(data.get("options", []))
        print(f"   {ticker}: {count} option candidates")


if __name__ == "__main__":
    run()
