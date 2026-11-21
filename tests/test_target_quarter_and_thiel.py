"""Regression checks: fetch and scheduler must target the same SEC quarter."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from config import FILERS
import date_check as dc
import fetch_filings as ff


@pytest.mark.parametrize(("today", "expected"), [
    ("2026-08-14", "2026-03-31"),
    ("2026-08-15", "2026-06-30"),
    ("2026-11-16", "2026-06-30"),
    ("2026-11-17", "2026-09-30"),
    ("2026-11-21", "2026-09-30"),
    ("2027-02-16", "2026-09-30"),
    ("2027-02-17", "2026-12-31"),
])
def test_fetch_uses_exact_same_due_quarter_as_schedule(monkeypatch, today, expected):
    monkeypatch.delenv("SEC_TARGET_REPORT_DATE", raising=False)
    assert ff.target_report_date(today) == expected
    from datetime import date
    assert ff.target_report_date(today) == dc.latest_due_quarter(date.fromisoformat(today))[0]


def test_explicit_quarter_override_is_validated(monkeypatch):
    monkeypatch.setenv("SEC_TARGET_REPORT_DATE", "2026-09-30")
    assert ff.target_report_date("2026-11-16") == "2026-09-30"
    monkeypatch.setenv("SEC_TARGET_REPORT_DATE", "2026-09-15")
    with pytest.raises(ValueError, match="quarter end"):
        ff.target_report_date("2026-11-21")
    monkeypatch.setenv("SEC_TARGET_REPORT_DATE", "not-a-date")
    with pytest.raises(ValueError):
        ff.target_report_date("2026-11-21")


def test_thiel_macro_cik_is_tracked_once():
    thiel = [(name, cik) for name, cik in FILERS.items() if cik == "0001562087"]
    assert len(thiel) == 1
    assert "Thiel Macro" in thiel[0][0]
