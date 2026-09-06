"""Stock alpha and option P&L are different questions and stay separate."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import backtest as bt  # noqa: E402


def _stock(strike=100.0, expiry="2027-01-15", mid=5.0, status="OK", ticker="X"):
    contract = {"symbol": f"{ticker}C", "strike": strike, "expiration": expiry,
                "mid": mid, "max_risk_per_contract": mid * 100, "breakeven": strike + mid}
    return {"ticker": ticker, "option": {"status": status,
                                         "contract": contract if status == "OK" else None}}


def test_an_open_contract_is_pending_not_a_result():
    future = (date.today() + timedelta(days=120)).isoformat()
    r = bt.option_outcome(_stock(expiry=future), "2026-09-06")
    assert r["option_status"] == "pending"
    assert r["option_days_to_expiry"] > 0
    assert "option_return_pct" not in r


def test_no_qualifying_contract_is_recorded_as_such():
    r = bt.option_outcome(_stock(status="NO_SUITABLE_OPTION_FOUND"), "2026-09-06")
    assert r["option_status"] == "NO_OPTION"
    assert r["option_reason"] == "NO_SUITABLE_OPTION_FOUND"


def test_settlement_uses_intrinsic_value(monkeypatch):
    """A long call settles at max(0, S - K), whatever the share price did."""
    monkeypatch.setattr(bt, "_underlying_close_on_or_after", lambda t, d: 130.0)
    r = bt.option_outcome(_stock(strike=100.0, expiry="2020-01-17", mid=5.0), "2019-09-01")
    assert r["option_value_at_expiry"] == 30.0
    assert r["option_return_pct"] == 500.0
    assert r["option_status"] == "win"
    assert r["option_expired_worthless"] is False


def test_a_rising_stock_can_still_leave_the_call_worthless(monkeypatch):
    """The whole reason option results are reported separately."""
    monkeypatch.setattr(bt, "_underlying_close_on_or_after", lambda t, d: 95.0)
    r = bt.option_outcome(_stock(strike=100.0, expiry="2020-01-17", mid=5.0), "2019-09-01")
    assert r["option_expired_worthless"] is True
    assert r["option_return_pct"] == -100.0
    assert r["option_status"] == "loss"


def test_a_gain_below_the_premium_is_still_a_loss(monkeypatch):
    monkeypatch.setattr(bt, "_underlying_close_on_or_after", lambda t, d: 103.0)
    r = bt.option_outcome(_stock(strike=100.0, expiry="2020-01-17", mid=5.0), "2019-09-01")
    assert r["option_value_at_expiry"] == 3.0
    assert r["option_return_pct"] == -40.0
    assert r["option_status"] == "loss"


def test_missing_price_data_is_not_scored_as_a_loss(monkeypatch):
    monkeypatch.setattr(bt, "_underlying_close_on_or_after", lambda t, d: None)
    r = bt.option_outcome(_stock(expiry="2020-01-17"), "2019-09-01")
    assert r["option_status"] == "no_price_data"
    assert "option_return_pct" not in r
