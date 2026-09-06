"""The gate must stop a degenerate quarter comparison before any signal is made."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import data_quality as dq  # noqa: E402


def _parsed(counts, exits=0, current="2026-06-30", prior="2026-03-31", capped=False):
    positions = []
    for kind, n in counts.items():
        for i in range(n):
            positions.append({"cusip": f"{kind}{i}", "shares": 10,
                              "delta": {"type": kind, "delta_pct": 1.0}})
    return {
        "period_of_report": current,
        "prior_report_date": prior,
        "filers": {"F": {
            "cik": "0000000001", "positions": positions,
            "is_capped": capped,
            "exit_detection_available": not capped,
            "exited_positions": [{"cusip": f"X{i}"} for i in range(exits)],
        }},
    }


def test_healthy_mix_passes():
    r = dq.evaluate(_parsed({"NEW": 120, "ADDED": 300, "REDUCED": 200, "UNCHANGED": 400}, exits=45))
    assert r["passed"], r["failures"]
    assert r["summary"]["counts"]["REDUCED"] == 200
    assert r["summary"]["exits"] == 45


def test_all_new_universe_fails():
    """1000 of 1000 NEW, nothing matched, no exits - must fail."""
    r = dq.evaluate(_parsed({"NEW": 1000}, exits=0))
    assert not r["passed"]
    joined = " ".join(r["failures"])
    assert "no current position matched" in joined or "degenerate" in joined


def test_wrong_prior_period_fails():
    r = dq.evaluate(_parsed({"NEW": 50, "ADDED": 50}, exits=5, prior="2026-06-30"))
    assert not r["passed"]
    assert any("not the required 2026-03-31" in f for f in r["failures"])


def test_missing_prior_period_fails():
    r = dq.evaluate(_parsed({"NEW": 50, "ADDED": 50}, exits=5, prior=""))
    assert not r["passed"]
    assert any("UNKNOWN" in f for f in r["failures"])


def test_zero_exits_is_a_warning_when_every_book_is_capped():
    r = dq.evaluate(_parsed({"NEW": 100, "ADDED": 400, "UNCHANGED": 300}, exits=0, capped=True))
    assert r["passed"]
    assert any("capped" in w for w in r["warnings"])


def test_gate_raises_and_blocks_downstream(tmp_path, monkeypatch):
    monkeypatch.setattr(dq, "DATA_DIR", tmp_path)
    with pytest.raises(dq.DataQualityFailure):
        dq.gate("2026-06-30", _parsed({"NEW": 1000}))
    verdict = tmp_path / "2026-06-30_data_quality.json"
    assert verdict.exists()          # the failure is recorded, not just raised


def test_render_shows_every_category():
    out = dq.render(dq.evaluate(_parsed({"NEW": 10, "ADDED": 20, "REDUCED": 5, "UNCHANGED": 60}, exits=3)))
    for label in ("NEW:", "ADDED:", "REDUCED:", "UNCHANGED:", "EXIT:", "DATA QUALITY GATE: PASS"):
        assert label in out
