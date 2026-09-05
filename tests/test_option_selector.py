import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import option_selector as osel  # noqa: E402
from config import NO_SUITABLE_OPTION  # noqa: E402

TODAY = date(2026, 9, 5)


def contract(symbol, dte=120, delta=0.45, bid=5.0, ask=5.2, vol=800, oi=2000, iv=0.35, strike=100.0, typ="call"):
    return {
        "symbol": symbol, "option_type": typ, "strike": strike,
        "expiration_date": (TODAY + timedelta(days=dte)).isoformat(),
        "bid": bid, "ask": ask, "volume": vol, "open_interest": oi,
        "greeks": {"delta": delta, "smv_vol": iv},
    }


def test_no_suitable_option_when_nothing_passes():
    chain = [
        contract("A", dte=30),                 # expiry too near
        contract("B", delta=0.15),             # delta too low
        contract("C", bid=5.0, ask=6.0),       # spread 18%
        contract("D", vol=10),                 # volume
        contract("E", oi=50),                  # open interest
        contract("F", iv=0.95),                # IV
        contract("G", typ="put"),              # not a call
    ]
    r = osel.select_call(chain, TODAY, spot=95.0)
    assert r["status"] == NO_SUITABLE_OPTION
    assert r["contract"] is None
    assert r["rejections"]["expiry_days"] >= 1
    assert r["rejections"]["delta"] >= 1
    assert r["rejections"]["spread"] >= 1
    assert r["rejections"]["volume"] >= 1
    assert r["rejections"]["open_interest"] >= 1
    assert r["rejections"]["iv"] >= 1


def test_picks_delta_closest_to_target_then_deterministic():
    chain = [contract("FAR", delta=0.65), contract("NEAR", delta=0.46), contract("MID", delta=0.38)]
    r = osel.select_call(chain, TODAY, spot=95.0)
    assert r["status"] == "OK"
    assert r["contract"]["symbol"] == "NEAR"
    assert r["eligible_count"] == 3
    # same input, reversed order -> same pick
    r2 = osel.select_call(list(reversed(chain)), TODAY, spot=95.0)
    assert r2["contract"]["symbol"] == "NEAR"
    assert r2["contract"] == r["contract"]


def test_tie_breaks_on_symbol():
    chain = [contract("ZZZ"), contract("AAA")]
    r = osel.select_call(chain, TODAY)
    assert r["contract"]["symbol"] == "AAA"


def test_contract_fields():
    r = osel.select_call([contract("X", strike=105.0, bid=4.9, ask=5.1)], TODAY, spot=100.0)
    c = r["contract"]
    assert c["mid"] == 5.0
    assert c["max_risk_per_contract"] == 500.0
    assert c["breakeven"] == 110.0
    assert c["moneyness_pct"] == 5.0
    assert c["dte"] == 120
    assert "Delta" in r["rule_rationale"]


def test_flat_shape_accepted():
    flat = {"symbol": "F", "option_type": "call", "strike": 100.0,
            "expiration_date": (TODAY + timedelta(days=100)).isoformat(),
            "bid": 3.0, "ask": 3.1, "volume": 500, "open_interest": 900, "delta": 0.5, "implied_volatility": 0.3}
    assert osel.check_contract(flat, TODAY) == []
