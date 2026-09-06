"""The prior quarter must be the required quarter - never a re-run of the current one."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import parse_13f as p13  # noqa: E402


def _write(tmp, file_date, period, shares=1000):
    (tmp / f"{file_date}_holdings_parsed.json").write_text(json.dumps({
        "date": file_date, "period_of_report": period,
        "filers": {"F": {"cik": "0000000001", "positions": [
            {"cusip": "A", "shares": shares, "port_weight_pct": 1.0,
             "delta": {"type": "ADDED", "delta_pct": 5.0}}]}}}))


def test_previous_quarter_end():
    q = p13.previous_quarter_end
    assert q("2026-06-30") == "2026-03-31"
    assert q("2026-03-31") == "2025-12-31"
    assert q("2026-09-30") == "2026-06-30"
    assert q("2026-12-31") == "2026-09-30"


def test_a_rerun_of_the_current_quarter_is_never_the_prior_quarter(tmp_path, monkeypatch):
    """A dataset generated on 2026-09-05 for period 2026-06-30 must not be used
    as the baseline for a 2026-06-30 run."""
    monkeypatch.setattr(p13, "DATA_DIR", tmp_path)
    _write(tmp_path, "2026-09-05", "2026-06-30")     # same quarter, newer file
    _write(tmp_path, "2026-05-16", "2026-03-31")     # the required quarter
    prior = p13.load_prior_quarter("2026-09-06", "2026-06-30")
    assert prior is not None
    assert prior["period_of_report"] == "2026-03-31"
    assert prior["date"] == "2026-05-16"


def test_newest_file_wins_for_the_required_quarter(tmp_path, monkeypatch):
    monkeypatch.setattr(p13, "DATA_DIR", tmp_path)
    _write(tmp_path, "2026-05-16", "2026-03-31")
    _write(tmp_path, "2026-05-20", "2026-03-31")     # restated / re-run
    assert p13.load_prior_quarter("2026-09-06", "2026-06-30")["date"] == "2026-05-20"


def test_missing_required_quarter_yields_no_baseline(tmp_path, monkeypatch):
    """Only an older quarter is present; it is not a substitute."""
    monkeypatch.setattr(p13, "DATA_DIR", tmp_path)
    _write(tmp_path, "2026-02-16", "2025-12-31")
    assert p13.load_prior_quarter("2026-09-06", "2026-06-30") is None
    assert p13.load_prior_quarter("2026-09-06", "") is None


def test_a_baseline_without_share_counts_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(p13, "DATA_DIR", tmp_path)
    _write(tmp_path, "2026-05-16", "2026-03-31", shares=0)
    assert p13.load_prior_quarter("2026-09-06", "2026-06-30") is None
