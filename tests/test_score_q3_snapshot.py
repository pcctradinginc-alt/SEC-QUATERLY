"""Audit regression using the archived Q3-2026 SEC snapshot (no network)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import buy_signal
import multi_quarter
import scoring


ROOT = Path(__file__).resolve().parents[1]
Q3 = ROOT / "data" / "holdings" / "2026-11-21_holdings_parsed.json"


def test_q3_tiny_berkshire_adds_are_not_buy_votes():
    parsed = json.loads(Q3.read_text())
    assert parsed["period_of_report"] == "2026-09-30"
    # Q3's v2.1 report incorrectly called +0.16%, +0.39%, +1.73%
    # meaningful consensus buys. The values are still present in raw data.
    for manager in ("Lountzis Asset Management", "Muhlenkamp & Co",
                    "Semper Augustus Investments Group"):
        filer = parsed["filers"][manager]
        brk = [p for p in filer["positions"] if p.get("ticker") == "BRK/B"]
        assert brk, manager
        assert not any(buy_signal.eligible_common_buy(p) for p in brk), manager

    quality = {n: {"quality_score": 0.5} for n in parsed["filers"]}
    freshness = {n: {"freshness_score": 0.5} for n in parsed["filers"]}
    scored = scoring.build_scored_universe(parsed, quality, freshness)
    assert not any(e["ticker"] == "BRK/B" and e["filer"] in (
        "Lountzis Asset Management", "Muhlenkamp & Co",
        "Semper Augustus Investments Group") for e in scored)


def test_q3_new_ewy_filer_does_not_inherit_prior_managers_builds():
    parsed = json.loads(Q3.read_text())
    old_style = json.loads(
        (ROOT / "data" / "holdings" / "2026-11-21_final_analysis.json").read_text()
    )
    old_ewy = next(s for s in old_style["top10"] if s["ticker"] == "EWY")
    assert old_ewy["factors"]["accumulation"] == 100.0
    assert old_ewy["filers"][0]["delta_type"] == "NEW"
    signals = multi_quarter.build_multi_quarter_signals("2026-11-21", current_parsed=parsed)
    new_ewy = signals.get("EWY", {})
    assert new_ewy.get("consecutive_quarters", 1) < 4
