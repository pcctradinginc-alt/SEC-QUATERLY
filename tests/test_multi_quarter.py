"""Multi-quarter accumulation must count quarters, not pipeline re-runs."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import multi_quarter as mq  # noqa: E402


def _write(tmp, file_date, report_date, shares=1000, has_prior=True, delta="ADDED"):
    (tmp / f"{file_date}_holdings_parsed.json").write_text(json.dumps({
        "date": file_date, "report_date": report_date, "has_prior_baseline": has_prior,
        "filers": {"F": {"cik": "0000000001", "positions": [
            {"ticker": "AAA", "cusip": "A", "shares": shares, "port_weight_pct": 5.0,
             "delta": {"type": delta, "delta_pct": 20.0}}]}},
    }))


def test_reruns_of_one_quarter_count_once(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "DATA_DIR", tmp_path)
    _write(tmp_path, "2026-09-05", "2026-06-30")
    _write(tmp_path, "2026-09-06", "2026-06-30")     # same quarter, re-run
    _write(tmp_path, "2026-05-16", "2026-03-31")
    hist = mq.load_historical_parsed("2026-09-07")
    assert len(hist) == 2
    assert [h["report_date"] for h in hist] == ["2026-06-30", "2026-03-31"]
    assert hist[0]["date"] == "2026-09-06"           # newest file wins for a quarter


def test_files_without_share_counts_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "DATA_DIR", tmp_path)
    _write(tmp_path, "2026-05-16", "2026-03-31", shares=0)   # pre-fix file
    _write(tmp_path, "2026-02-16", "2025-12-31", shares=500)
    hist = mq.load_historical_parsed("2026-09-07")
    assert [h["report_date"] for h in hist] == ["2025-12-31"]


def test_today_and_later_files_are_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "DATA_DIR", tmp_path)
    _write(tmp_path, "2026-09-07", "2026-06-30")
    assert mq.load_historical_parsed("2026-09-07") == []


def test_a_standalone_baseline_cannot_prove_accumulation(tmp_path, monkeypatch):
    """A quarter built without its own predecessor marks everything NEW.
    Counting it would manufacture accumulation across the whole universe."""
    monkeypatch.setattr(mq, "DATA_DIR", tmp_path)
    _write(tmp_path, "2026-02-15", "2025-12-31", has_prior=False, delta="NEW")
    _write(tmp_path, "2026-05-16", "2026-03-31", has_prior=True)
    hist = mq.load_historical_parsed("2026-09-06")
    assert [h["report_date"] for h in hist] == ["2026-03-31"]


def test_real_comparison_is_inferred_for_files_without_the_flag(tmp_path):
    standalone = {"filers": {"F": {"positions": [
        {"delta": {"type": "NEW"}} for _ in range(50)]}}}
    compared = {"filers": {"F": {"positions": [
        {"delta": {"type": "NEW"}}, {"delta": {"type": "ADDED"}},
        {"delta": {"type": "REDUCED"}}, {"delta": {"type": "UNCHANGED"}}]}}}
    assert mq._had_real_comparison(standalone) is False
    assert mq._had_real_comparison(compared) is True
