"""Regression tests: real SEC deadlines, missed schedules, and e-mail idempotence."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import date_check as dc
from email_state import last_sent_quarter, record_sent


@pytest.mark.parametrize(("quarter", "deadline"), [
    ("2025-12-31", "2026-02-17"),  # Monday 16 Feb is Presidents' Day
    ("2026-03-31", "2026-05-15"),
    ("2026-06-30", "2026-08-14"),
    ("2026-09-30", "2026-11-16"),  # Day 45 falls on Saturday
    ("2026-12-31", "2027-02-16"),
])
def test_real_13f_deadlines(quarter, deadline):
    assert dc.filing_deadline(date.fromisoformat(quarter)) == date.fromisoformat(deadline)


def test_no_early_report_even_on_deadline(monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_NOTIFICATION_STATE_PATH", str(tmp_path / "state.json"))
    assert dc.latest_due_quarter(date(2026, 11, 16))[0] == "2026-06-30"
    assert dc.latest_due_quarter(date(2026, 11, 17))[0] == "2026-09-30"
    assert dc.should_run_today(date(2026, 11, 17))


def test_missed_schedule_is_caught_up(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setenv("SEC_NOTIFICATION_STATE_PATH", str(state))
    assert dc.should_run_today(date(2026, 11, 21))
    assert dc.should_run_today(date(2026, 12, 1))
    record_sent("2026-09-30", "2026-11-21")
    assert last_sent_quarter() == "2026-09-30"
    assert not dc.should_run_today(date(2026, 11, 22))
    assert not dc.should_run_today(date(2026, 12, 1))
    assert dc.should_run_today(date(2027, 2, 17))


def test_manual_force_can_rebuild(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setenv("SEC_NOTIFICATION_STATE_PATH", str(state))
    record_sent("2026-09-30", "2026-11-21")
    assert dc.should_run_today(date(2026, 11, 22), force=True)
    assert dc.should_run_today(date(2026, 11, 22), target_override="2026-11-22")


def test_invalid_state_fails_closed(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{corrupt", encoding="utf-8")
    monkeypatch.setenv("SEC_NOTIFICATION_STATE_PATH", str(state))
    with pytest.raises(RuntimeError, match="Cannot trust"):
        dc.should_run_today(date(2026, 11, 21))


def test_record_sent_is_atomic_and_validated(tmp_path):
    p = tmp_path / "checkpoint.json"
    record_sent("2026-09-30", "2026-11-21", path=p)
    x = json.loads(p.read_text())
    assert x["last_emailed_quarter"] == "2026-09-30"
    assert x["report_run_date"] == "2026-11-21"
    assert not p.with_suffix(".tmp").exists()
    with pytest.raises(ValueError):
        record_sent("2026-10-31", "2026-11-21", path=p)
    assert last_sent_quarter(path=p) == "2026-09-30"
