"""
Determinism and factor tests for the signal engine.
Run: python -m pytest -q tests
"""
import copy
import json
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import signal_engine as se  # noqa: E402
from config import SIGNAL_WEIGHTS, TOP_N  # noqa: E402

TODAY = date(2026, 9, 5)


def _agg(ticker, filers, **kw):
    base = {
        "ticker": ticker, "name": f"{ticker} Inc", "filers": filers, "alpha_score": 50.0,
        "flags": [], "crowding_label": "LOW", "crowding_penalty": 0.0,
        "post_filing_perf": {}, "cluster_count": len(filers), "cluster_funds": [],
    }
    base.update(kw)
    return base


def _filer(name, wt, delta_type="NEW", delta_pct=None, q=0.8, fresh=0.8, tier="TOP10"):
    return {"filer": name, "port_weight_pct": wt, "delta_type": delta_type, "delta_pct": delta_pct,
            "manager_quality_score": q, "freshness_score": fresh, "position_tier": tier,
            "weight_vs_median": 3.0, "report_date": "2026-06-30", "filing_date_actual": "2026-08-14"}


def make_scores(n_extra=30, seed=1):
    rnd = random.Random(seed)
    aggs = [
        _agg("AAA", [_filer("Fund A", 12.0, q=0.95), _filer("Fund B", 4.0, "ADDED", 45.0, q=0.7)]),
        _agg("BBB", [_filer("Fund C", 6.0, q=0.9, tier="TOP3")]),
        _agg("CCC", [_filer("Fund D", 1.2, "ADDED", 10.0, q=0.4, fresh=0.4)], crowding_penalty=25.0),
        _agg("037833100", [_filer("Fund E", 9.0)]),  # CUSIP-only key: never in Top 10
        _agg("DDD", [_filer("Fund F", 2.0)], post_filing_perf={"pct_change": 40.0}),
    ]
    for i in range(n_extra):
        t = f"T{i:02d}"
        aggs.append(_agg(t, [_filer(f"F{i}", rnd.uniform(0.5, 8.0), q=rnd.uniform(0.3, 1.0))]))
    scored_flat = []
    for a in aggs:
        for f in a["filers"]:
            scored_flat.append({"ticker": a["ticker"], "filer": f["filer"], "report_date": "2026-06-30",
                                "filing_date_actual": "2026-08-14", "filing_date": "2026-06-30"})
    return {"aggregated": aggs, "scored_flat": scored_flat,
            "mq_signals": {"BBB": {"build_quarters": 2, "silent_build": True}}}


INSIDER = {
    "AAA": {"score": 80.0, "reasons": ["$1,500,000 of open-market insider purchases since quarter-end"],
            "summary": {"buy_value_usd": 1_500_000, "distinct_buyers": ["X", "Y"]}},
    "CCC": {"score": 100.0, "reasons": ["big buys"], "summary": {"buy_value_usd": 5_000_000, "distinct_buyers": ["A", "B", "C"]}},
}


def test_identical_input_identical_output():
    a = se.compute_signals(make_scores(), INSIDER, TODAY)
    b = se.compute_signals(make_scores(), INSIDER, TODAY)
    assert a == b
    assert a["ranking_fingerprint"] == b["ranking_fingerprint"]


def test_order_independent():
    s1 = make_scores()
    s2 = copy.deepcopy(s1)
    random.Random(7).shuffle(s2["aggregated"])
    random.Random(8).shuffle(s2["scored_flat"])
    for a in s2["aggregated"]:
        random.Random(9).shuffle(a["filers"])
    r1 = se.compute_signals(s1, INSIDER, TODAY)
    r2 = se.compute_signals(s2, INSIDER, TODAY)
    assert [(r["ticker"], r["signal_score"]) for r in r1["ranking"]] == \
           [(r["ticker"], r["signal_score"]) for r in r2["ranking"]]
    assert r1["ranking_fingerprint"] == r2["ranking_fingerprint"]


def test_scores_bounded_and_weights_sum():
    r = se.compute_signals(make_scores(), INSIDER, TODAY)
    assert sum(SIGNAL_WEIGHTS.values()) == 100
    for row in r["ranking"]:
        assert 0.0 <= row["signal_score"] <= 100.0
        for k, v in row["factors"].items():
            assert 0.0 <= v <= 100.0, k
        expected = sum(row["contributions"].values()) - row["price_penalty"]
        assert abs(row["signal_score"] - max(0.0, min(100.0, expected))) < 0.06


def test_top10_excludes_cusip_keys_and_has_sequential_ranks():
    r = se.compute_signals(make_scores(), INSIDER, TODAY)
    top = r["top10"]
    assert len(top) == TOP_N
    assert all(t["option_eligible"] for t in top)
    assert "037833100" not in [t["ticker"] for t in top]
    assert [t["rank"] for t in top] == list(range(1, TOP_N + 1))


def test_insider_moves_ranking():
    without = se.compute_signals(make_scores(), {}, TODAY)
    with_ins = se.compute_signals(make_scores(), INSIDER, TODAY)
    pos = lambda res, t: [r["ticker"] for r in res["ranking"]].index(t)
    assert pos(with_ins, "CCC") < pos(without, "CCC")
    ccc = next(r for r in with_ins["ranking"] if r["ticker"] == "CCC")
    assert ccc["factors"]["insider"] == 100.0
    assert ccc["contributions"]["insider"] == SIGNAL_WEIGHTS["insider"]


def test_price_penalty_and_crowding():
    r = se.compute_signals(make_scores(), {}, TODAY)
    ddd = next(x for x in r["ranking"] if x["ticker"] == "DDD")
    assert ddd["price_penalty"] == 15.0
    assert any("priced in" in b for b in ddd["why"]["bullets"])
    ccc = next(x for x in r["ranking"] if x["ticker"] == "CCC")
    assert ccc["factors"]["crowding"] == 75.0
    assert ccc["crowding_label"] == "MODERATE"


def test_why_block_present():
    r = se.compute_signals(make_scores(), INSIDER, TODAY)
    for t in r["top10"]:
        assert len(t["why"]["top_drivers"]) == 3
        assert t["why"]["bullets"]
        assert t["ticker"] in t["why"]["summary"]
        assert set(t["reasons"]) == set(SIGNAL_WEIGHTS)


def test_tie_break_is_alphabetical():
    s = {"aggregated": [_agg("ZZZ", [_filer("F", 5.0)]), _agg("AAA", [_filer("F", 5.0)])],
         "scored_flat": [], "mq_signals": {}}
    r = se.compute_signals(s, {}, TODAY)
    assert [x["ticker"] for x in r["ranking"]] == ["AAA", "ZZZ"]


def test_json_roundtrip_stable():
    r = se.compute_signals(make_scores(), INSIDER, TODAY)
    again = json.loads(json.dumps(r, sort_keys=True))
    assert again["ranking_fingerprint"] == r["ranking_fingerprint"]
