"""A run must compare quarters, not re-runs of the same quarter."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import parse_13f as p13  # noqa: E402


def _write(tmp, date_str, report_date):
    (tmp / f"{date_str}_holdings_parsed.json").write_text(json.dumps(
        {"date": date_str, "report_date": report_date, "filers": {}}))


def test_skips_a_rerun_of_the_same_quarter(tmp_path, monkeypatch):
    monkeypatch.setattr(p13, "DATA_DIR", tmp_path)
    _write(tmp_path, "2026-05-18", "2026-03-31")     # previous quarter
    _write(tmp_path, "2026-09-05", "2026-06-30")     # yesterday, SAME quarter
    prior = p13.load_prior_quarter("2026-09-06", "2026-06-30")
    assert prior is not None
    assert prior["report_date"] == "2026-03-31"
    assert prior["date"] == "2026-05-18"


def test_without_a_known_quarter_it_falls_back_to_the_newest_earlier_file(tmp_path, monkeypatch):
    monkeypatch.setattr(p13, "DATA_DIR", tmp_path)
    _write(tmp_path, "2026-05-18", "2026-03-31")
    assert p13.load_prior_quarter("2026-09-06", "")["date"] == "2026-05-18"


def test_no_prior_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(p13, "DATA_DIR", tmp_path)
    assert p13.load_prior_quarter("2026-09-06", "2026-06-30") is None
