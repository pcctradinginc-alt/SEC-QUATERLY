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
        "ticker": ticker, "name": kw.pop("name", f"{ticker} Inc"), "filers": filers, "alpha_score": 50.0,
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
        expected = sum(row["contributions"].values()) + row["confluence_bonus"] - row["price_penalty"]
        assert abs(row["signal_score"] - max(0.0, min(100.0, expected))) < 0.06
        assert 0.0 <= row["confluence_bonus"] <= 10.0
        assert 0.0 <= row["score_13f"] <= 100.0


def test_unmapped_cusips_never_enter_the_candidate_universe():
    r = se.compute_signals(make_scores(), INSIDER, TODAY)
    assert not se.is_tradable_ticker("037833100")
    assert not se.is_tradable_ticker("G4474Y214")
    assert not se.is_tradable_ticker("")
    assert se.is_tradable_ticker("AAPL") and se.is_tradable_ticker("BRK/B")
    assert "037833100" not in [t["ticker"] for t in r["top10"]]
    assert r["excluded_no_ticker_count"] >= 1
    pool, _ = se.candidate_pool(make_scores(), TODAY)
    assert all(se.is_tradable_ticker(t) for t in pool)


def test_top10_excludes_cusip_keys_and_has_sequential_ranks():
    r = se.compute_signals(make_scores(), INSIDER, TODAY)
    top = r["top10"]
    assert len(top) == TOP_N
    assert all(t["tradable_ticker_validated"] for t in top)
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


def test_share_classes_collapse_to_one_slot():
    """BRK/A and BRK/B are one issuer and must not take two Top-10 slots."""
    s = {"aggregated": [
            _agg("BRK/A", [_filer("Fund A", 9.0, q=0.9)], name="BERKSHIRE HATHAWAY INC DEL"),
            _agg("BRK/B", [_filer("Fund A", 8.0, q=0.9)], name="BERKSHIRE HATHAWAY INC DEL"),
            _agg("GOOGL", [_filer("Fund B", 7.0, q=0.8)], name="ALPHABET INC CL A"),
            _agg("GOOG",  [_filer("Fund B", 6.0, q=0.8)], name="ALPHABET INC CL C"),
            _agg("META",  [_filer("Fund C", 5.0, q=0.8)], name="META PLATFORMS INC"),
         ], "scored_flat": [], "mq_signals": {}}
    r = se.compute_signals(s, {}, TODAY)
    tickers = [x["ticker"] for x in r["top10"]]
    assert tickers == ["BRK/A", "GOOGL", "META"]          # higher-scoring class kept
    assert [x["rank"] for x in r["top10"]] == [1, 2, 3]    # ranks stay sequential
    folded = {x["ticker"]: x["superseded_by"] for x in r["excluded_same_issuer"]}
    assert folded == {"BRK/B": "BRK/A", "GOOG": "GOOGL"}
    brk = next(x for x in r["top10"] if x["ticker"] == "BRK/A")
    assert brk["same_issuer_alternates"] == [{"ticker": "BRK/B", "signal_score": brk["same_issuer_alternates"][0]["signal_score"]}]


def test_issuer_key_does_not_overmerge():
    k = se.issuer_key
    assert k({"name": "BERKSHIRE HATHAWAY INC DEL", "ticker": "BRK/A"}) == \
           k({"name": "BERKSHIRE HATHAWAY INC DEL", "ticker": "BRK/B"})
    assert k({"name": "ALPHABET INC CL A", "ticker": "GOOGL"}) == \
           k({"name": "ALPHABET INC CL C", "ticker": "GOOG"})
    assert k({"name": "META PLATFORMS INC", "ticker": "META"}) != \
           k({"name": "MICROSOFT CORP", "ticker": "MSFT"})
    assert k({"name": "", "ticker": "ELV"}) == "ELV"       # falls back to ticker


def test_share_class_family_ticker_test():
    f = se.same_share_class_family
    for a, b in [("BRK/A", "BRK/B"), ("GOOG", "GOOGL"), ("UA", "UAA"),
                 ("FOX", "FOXA"), ("CORZ", "CORZW"), ("LLYVA", "LLYVK")]:
        assert f(a, b) and f(b, a), (a, b)
    for a, b in [("EWY", "EWZ"), ("IWM", "INDA"), ("BRK/B", "BRKB2")]:
        assert not f(a, b) and not f(b, a), (a, b)


def test_etfs_of_one_sponsor_are_not_merged():
    """Every iShares fund reports issuer name "ISHARES INC"; matching the name
    alone merged South Korea (EWY) with Brazil (EWZ) in a real run."""
    s = {"aggregated": [
            _agg("EWZ",  [_filer("Fund A", 9.0)], name="Ishares Inc"),
            _agg("EWY",  [_filer("Fund A", 8.0)], name="ISHARES INC"),
            _agg("INDA", [_filer("Fund B", 7.0)], name="ISHARES TR"),
            _agg("IWM",  [_filer("Fund B", 6.0)], name="Ishares Tr"),
            _agg("VZ",   [_filer("Fund C", 5.0)], name="VERIZON COMMUNICATIONS INC"),
            _agg("V",    [_filer("Fund C", 4.0)], name="VISA INC COM CL A"),
         ], "scored_flat": [], "mq_signals": {}}
    r = se.compute_signals(s, {}, TODAY)
    assert [x["ticker"] for x in r["top10"]] == ["EWZ", "EWY", "INDA", "IWM", "VZ", "V"]
    assert r["excluded_same_issuer"] == []


def test_confluence_bonus_rewards_the_combination():
    """A weighted sum rates 'great 13F, no insider' the same as the reverse; the
    engine is looking for both firing together, so co-occurrence earns a bonus."""
    cb = se.confluence_bonus
    assert cb(80.0, 80.0)[0] > 0
    assert cb(80.0, 0.0)[0] == 0.0          # no insider confirmation
    assert cb(20.0, 90.0)[0] == 0.0         # 13F side too weak to matter
    assert cb(100.0, 100.0)[0] == 10.0      # capped
    assert cb(60.0, 45.0)[0] <= cb(90.0, 90.0)[0]


def test_signal_class_is_deterministic_and_named():
    r = se.compute_signals(make_scores(), INSIDER, TODAY)
    for t in r["top10"]:
        assert t["signal_class"]
        assert t["signal_label"]
    again = se.compute_signals(make_scores(), INSIDER, TODAY)
    assert [t["signal_class"] for t in r["top10"]] == [t["signal_class"] for t in again["top10"]]


def test_insider_confirmation_upgrades_the_signal_class():
    agg = _agg("AAA", [_filer("Fund A", 12.0, q=0.95), _filer("Fund B", 6.0, q=0.9)])
    scores = {"aggregated": [agg], "scored_flat": [], "mq_signals": {}}
    strong_insider = {"AAA": {"score": 90.0, "reasons": ["CEO bought"],
                              "summary": {"buy_value_usd": 3_000_000, "distinct_buyers": ["X", "Y"]}}}
    with_ins = se.compute_signals(scores, strong_insider, TODAY)["ranking"][0]
    without = se.compute_signals(scores, {}, TODAY)["ranking"][0]
    assert with_ins["confluence_bonus"] > 0
    assert "INSIDER_CONFIRMATION" in with_ins["signal_class"]
    assert without["confluence_bonus"] == 0.0
    assert "INSIDER_CONFIRMATION" not in without["signal_class"]


def test_json_roundtrip_stable():
    r = se.compute_signals(make_scores(), INSIDER, TODAY)
    again = json.loads(json.dumps(r, sort_keys=True))
    assert again["ranking_fingerprint"] == r["ranking_fingerprint"]
