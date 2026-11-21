"""Financial-semantic regression tests: do not mistake minor rebalances for alpha."""
import json
import sys
from pathlib import Path
from datetime import date

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import buy_signal as bs
import multi_quarter as mq
import signal_engine as se
from config import ENGINE_VERSION, MIN_MATERIAL_ADD_PCT


def position(ticker="AAA", cusip="AAA111", delta="ADDED", pct=10.0, weight=5.0):
    return {"ticker": ticker, "cusip": cusip, "shares": 1000,
            "port_weight_pct": weight, "exposure_type": "COMMON_LONG",
            "delta": {"type": delta, "delta_pct": pct}}


def quarter(period, filers):
    return {"report_date": period, "has_prior_baseline": True,
            "filers": {name: {"cik": cik, "positions": positions}
                       for name, (cik, positions) in filers.items()}}


def write_prior(tmp_path, run_day, record):
    (tmp_path / f"{run_day}_holdings_parsed.json").write_text(json.dumps(record))


def test_immaterial_add_is_not_purchase_signal():
    assert MIN_MATERIAL_ADD_PCT == 5
    assert not bs.material_buy(position(pct=0.16))
    assert not bs.material_buy(position(pct=4.99))
    assert bs.material_buy(position(pct=5))
    assert bs.material_buy(position(delta="NEW", pct=None))
    assert not bs.material_buy(position(delta="NO_BASELINE", pct=None))
    assert not bs.material_buy(position(delta="REDUCED", pct=-25))
    assert not bs.eligible_common_buy(position(weight=0.2))
    assert not bs.eligible_common_buy({**position(), "possible_corporate_action": True})
    assert not bs.eligible_common_buy({**position(), "exposure_type": "CALL_ONLY"})


def test_valid_zero_is_not_replaced_by_neutral_manager_quality():
    zeros = [{"filer": "AQR Capital (Asness)", "manager_quality_score": 0.0,
              "freshness_score": 0.0, "delta_type": "ADDED",
              "delta_pct": 15.0, "port_weight_pct": 5.0}]
    assert se.factor_manager_quality(zeros)[0] == 0.0
    assert se.factor_consensus(zeros)[0] == 15.0
    assert se.factor_freshness(zeros, date(2026, 11, 21))[0] == 15.0  # age fallback 60d
    assert se.factor_manager_quality([{"filer": "X", "manager_quality_score": None}])[0] == 50.0


def test_activity_breadth_only_counted_in_consensus():
    one = [{"filer": "A", "delta_type": "NEW", "port_weight_pct": 4.0,
            "manager_quality_score": 0.8}]
    both = one + [{"filer": "B", "delta_type": "ADDED", "delta_pct": 12.0,
                   "port_weight_pct": 3.0, "manager_quality_score": 0.8}]
    assert se.factor_activity(one)[0] == se.factor_activity(both)[0]
    assert se.factor_consensus(both)[0] > se.factor_consensus(one)[0]


def test_corporate_treasuries_alone_are_not_stock_picker_consensus():
    corp = [{"filer": "NVIDIA Corp", "manager_quality_score": 0.9}]
    assert se.factor_consensus(corp)[0] == 0.0


def test_manager_streak_requires_same_cik_cusip_and_adjacent_quarters(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "DATA_DIR", tmp_path)
    prior2 = quarter("2026-06-30", {
        "Manager Before Rename": ("0000000001", [position(pct=15)])
    })
    prior1 = quarter("2026-03-31", {
        "Earlier Manager": ("0000000001", [position(pct=10)])
    })
    write_prior(tmp_path, "2026-09-06", prior2)
    write_prior(tmp_path, "2026-05-16", prior1)
    current = quarter("2026-09-30", {
        "Manager Current": ("0000000001", [position(pct=25)])
    })
    results = mq.build_multi_quarter_signals("2026-11-21", current)
    assert results["AAA"]["build_quarters"] == 2
    assert results["AAA"]["consecutive_quarters"] == 3
    assert results["AAA"]["manager_cik"] == "0000000001"
    assert se.factor_accumulation(results["AAA"], True)[0] == 75.0

    swapped_manager = quarter("2026-09-30", {
        "Different manager": ("0000000002", [position(pct=25)])
    })
    assert "AAA" not in mq.build_multi_quarter_signals("2026-11-21", swapped_manager)

    swapped_security = quarter("2026-09-30", {
        "Manager Current": ("0000000001", [position(cusip="DIFFERENT", pct=25)])
    })
    assert "AAA" not in mq.build_multi_quarter_signals("2026-11-21", swapped_security)

    newcomer = quarter("2026-09-30", {
        "Manager Current": ("0000000001", [position(delta="NEW", pct=None)])
    })
    assert "AAA" not in mq.build_multi_quarter_signals("2026-11-21", newcomer)


def test_no_accumulation_for_weak_prior_add_or_missing_intermediate_quarter(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "DATA_DIR", tmp_path)
    current = quarter("2026-09-30", {
        "Filer": ("0000000001", [position(pct=20)])
    })
    small_prior = quarter("2026-06-30", {
        "Filer": ("0000000001", [position(pct=0.39)])
    })
    write_prior(tmp_path, "2026-09-06", small_prior)
    assert not mq.build_multi_quarter_signals("2026-11-21", current)

    # Even a strong Q1 cannot bridge missing/invalid Q2 buying.
    write_prior(tmp_path, "2026-05-16", quarter("2026-03-31", {
        "Filer": ("0000000001", [position(pct=40)])
    }))
    assert not mq.build_multi_quarter_signals("2026-11-21", current)


def test_score_model_is_separately_versioned():
    assert ENGINE_VERSION == "v2.2_material_streak"
