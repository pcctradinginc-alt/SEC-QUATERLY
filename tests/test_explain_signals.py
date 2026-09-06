"""Regression tests for narrative facts (see the 2026-09-05 report)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import explain_signals as ex  # noqa: E402


VISA = {"buy_value_usd": 0, "sell_value_usd": 37_056_411}
UBER = {"buy_value_usd": 0, "sell_value_usd": 451_600_000}
ELV = {"buy_value_usd": 1_368_280, "sell_value_usd": 0}


def test_rejects_claiming_no_sales_when_there_were_sales():
    ok, why = ex._insider_facts_ok("Zero insider buys but no sales either.", VISA)
    assert not ok and "no insider sales" in why


def test_rejects_a_dollar_figure_that_matches_nothing():
    ok, why = ex._insider_facts_ok("Massive insider selling ($471M) is a red flag.", UBER)
    assert not ok and "matches no Form 4 total" in why


def test_rejects_claiming_purchases_when_there_were_none():
    assert not ex._insider_facts_ok("Insiders bought aggressively this quarter.", VISA)[0]


def test_accepts_faithful_wording():
    assert ex._insider_facts_ok("No insider purchases and $37.1M in insider sales.", VISA)[0]
    assert ex._insider_facts_ok("Insiders sold $451.6M since quarter-end.", UBER)[0]
    assert ex._insider_facts_ok("Officers purchased $1,368,280 in open-market trades.", ELV)[0]


def test_money_parser():
    assert ex._money_figures("$1.4M and $250,072 and $2B") == [1_400_000.0, 250_072.0, 2e9]
    assert ex._money_figures("$5.2 million") == [5_200_000.0]


def test_money_parser_does_not_read_a_following_word_as_a_magnitude():
    """"$10.55 mid" is ten dollars fifty-five, not 10.55 million."""
    assert ex._money_figures("$10.55 mid") == [10.55]
    assert ex._money_figures("$2.96 Bid / Ask") == [2.96]


CONTRACT = {"status": "OK", "contract": {
    "mid": 10.55, "max_risk_per_contract": 1055.0, "breakeven": 120.55,
    "strike": 110.0, "delta": 0.456}}
NO_OPTION = {"status": "NO_SUITABLE_OPTION_FOUND", "contract": None}


def test_option_note_must_match_the_selected_contract():
    assert ex._option_facts_ok("Jan 2027 110 call at delta 0.46 costs $10.55 mid.", CONTRACT)[0]
    ok, why = ex._option_facts_ok("The delta 0.80 call is the pick.", CONTRACT)
    assert not ok and "delta" in why
    ok, why = ex._option_facts_ok("Premium of $99.99 per contract.", CONTRACT)
    assert not ok and "not part of the selected contract" in why


def test_option_note_may_not_invent_a_contract():
    assert ex._option_facts_ok("No suitable call option found; the chain is illiquid.", NO_OPTION)[0]
    ok, why = ex._option_facts_ok("We recommend buying the 110 strike at delta 0.45.", NO_OPTION)
    assert not ok and "none qualified" in why


def test_validator_rejects_a_contradicting_stock():
    validate = ex.make_validator(["V"], {"V": VISA})
    payload = {
        "market_context": "A sober two-sentence market summary for the quarter under review.",
        "stocks": [{
            "ticker": "V",
            "why_strongest": "Eight independent buyers led by TCI and ValueAct built new positions here.",
            "insider_read": "Zero insider buys but no sales either.",
            "risks": ["a", "b"],
            "option_note": "n/a",
            "verdict": "STRONG",
        }],
    }
    ok, why = validate(payload)
    assert not ok and "insider_read" in why

    payload["stocks"][0]["insider_read"] = "No insider purchases, and $37.1M sold post-quarter."
    assert validate(payload)[0]
