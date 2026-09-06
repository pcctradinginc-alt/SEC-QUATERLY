"""Acceptance tests for the Institutional Dissent Penalty."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dissent as d  # noqa: E402
from config import DISSENT_MAX_PENALTY  # noqa: E402


def sell(filer="Fund A", type_="REDUCE", delta=-50.0, prior_rank=None,
         prior_weight=3.0, quality=0.8, **kw):
    s = {"filer": filer, "type": type_, "manager_quality_score": quality,
         "prior_port_weight": prior_weight, "prior_rank": prior_rank}
    if type_ == "REDUCE":
        s["delta_pct"] = delta
    s.update(kw)
    return s


def buy(filer="Buyer A", quality=0.8, delta_type="NEW", delta=None):
    return {"filer": filer, "manager_quality_score": quality,
            "delta_type": delta_type, "delta_pct": delta}


BUYERS = [buy("Buyer A"), buy("Buyer B", 0.7, "ADDED", 40.0)]


def p(signals, buyers=BUYERS):
    return d.compute(signals, buyers)["penalty"]


def test_no_sellers_means_no_penalty():
    r = d.compute([], BUYERS)
    assert r["penalty"] == 0.0
    assert r["stance"] == "NO_DISSENT"
    assert r["seller_count"] == 0


def test_severity_orders_reduce_below_exit():
    small = p([sell(delta=-20.0)])
    half = p([sell(delta=-50.0)])
    exit_ = p([sell(type_="EXIT")])
    assert 0 < small < half < exit_


def test_a_former_top3_exit_outweighs_a_small_one():
    top3 = p([sell(type_="EXIT", prior_rank=1, prior_weight=8.0)])
    minor = p([sell(type_="EXIT", prior_rank=40, prior_weight=0.3)])
    assert top3 > minor


def test_a_strong_manager_outweighs_a_weak_one():
    strong = p([sell(type_="EXIT", quality=0.95)])
    weak = p([sell(type_="EXIT", quality=0.10)])
    assert strong > weak


def test_three_independent_sellers_outweigh_one():
    one = p([sell("Fund A", type_="EXIT")])
    three = p([sell("Fund A", type_="EXIT"), sell("Fund B", type_="EXIT"),
               sell("Fund C", type_="EXIT")])
    assert three > one
    assert three <= DISSENT_MAX_PENALTY


def test_many_buyers_soften_the_same_seller():
    seller = [sell(type_="EXIT", prior_rank=2)]
    crowd = [buy(f"Buyer {i}", 0.9) for i in range(10)]
    lonely = [buy("Buyer A", 0.5, "ADDED", 20.0)]
    assert p(seller, crowd) < p(seller, lonely)


def test_corporate_actions_are_not_dissent():
    assert p([sell(type_="EXIT", possible_corporate_action=True)]) == 0.0


def test_capped_books_without_exit_data_are_not_dissent():
    assert p([sell(type_="EXIT", exit_detection_available=False)]) == 0.0


def test_reductions_below_the_threshold_are_ignored():
    assert p([sell(delta=-19.9)]) == 0.0
    assert d.sell_severity(sell(delta=-19.9)) == 0.0


def test_result_is_order_independent():
    signals = [sell("Fund A", type_="EXIT", prior_rank=1), sell("Fund B", delta=-60.0),
               sell("Fund C", delta=-25.0, quality=0.4)]
    base = d.compute(signals, BUYERS)
    for seed in (1, 2, 3):
        shuffled = signals[:]
        random.Random(seed).shuffle(shuffled)
        assert d.compute(shuffled, BUYERS) == base


def test_penalty_stays_inside_its_cap():
    brutal = [sell(f"Fund {i}", type_="EXIT", prior_rank=1, quality=1.0) for i in range(12)]
    r = d.compute(brutal, [buy("Only", 0.2, "ADDED", 5.0)])
    assert 0.0 <= r["penalty"] <= DISSENT_MAX_PENALTY
    assert r["stance"] == "SELL_DOMINANT"


def test_the_vst_case():
    """A high-quality manager fully exits its former #1 while three accumulate."""
    lone_pine = [sell("Lone Pine Capital (Mandel)", "EXIT", prior_rank=1,
                      prior_weight=7.4, quality=0.54)]
    buyers = [buy("Thiel Macro (Thiel)", 0.44), buy("Appaloosa (Tepper)", 0.72, "ADDED", 10.0),
              buy("Sound Shore Management", 0.71, "ADDED", 20.0)]
    r = d.compute(lone_pine, buyers)
    assert 8.0 <= r["penalty"] <= 12.0
    assert r["stance"] == "STRONG_DISSENT"
    assert r["strongest_action"] == "EXIT"
    assert "former #1 position" in r["reason"]


def test_related_vehicles_count_as_one_dissenting_voice():
    """The same rule as the buy side: two vehicles of one manager are one vote."""
    li_lu = [sell("H&H International Investment (Li Lu)", "EXIT", quality=0.99),
             sell("Himalaya Capital Management (Li Lu)", "EXIT", quality=1.0)]
    assert d.compute(li_lu, BUYERS)["seller_count"] == 1


def test_a_manager_on_both_sides_is_netted_to_the_buy_side():
    """Buying one share class while exiting another is a swap, not a change of mind."""
    signals = [sell("Buyer A", "EXIT", prior_rank=1, quality=0.9)]
    assert d.compute(signals, [buy("Buyer A")])["penalty"] == 0.0


def test_stance_bands():
    assert d.classify(0.0, 0) == "NO_DISSENT"
    assert d.classify(2.0, 1) == "MINOR_DISSENT"
    assert d.classify(5.0, 1) == "MIXED"
    assert d.classify(9.0, 2) == "STRONG_DISSENT"
    assert d.classify(13.0, 3) == "SELL_DOMINANT"
