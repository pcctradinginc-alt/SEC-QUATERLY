"""End-to-end unit tests for delivery checkpoint and failure alerts (no network)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import send_report as sr
import notify as nt
from email_state import last_sent_quarter


def _fake_report(monkeypatch, tmp_path, run_date="2026-11-21"):
    holdings = tmp_path / "holdings"
    reports = tmp_path / "reports"
    holdings.mkdir()
    reports.mkdir()
    (holdings / f"{run_date}_final_analysis.json").write_text(json.dumps({
        "top10": [{"ticker": "ABC", "option": {"status": "NO_SUITABLE_OPTION_FOUND"}}]
    }))
    (holdings / f"{run_date}_raw_holdings.json").write_text(json.dumps({
        "report_date": "2026-09-30"
    }))
    monkeypatch.setattr(sr, "DATA_DIR", holdings)
    monkeypatch.setattr(sr, "REPORTS_DIR", reports)
    monkeypatch.setattr(sr, "generate_html_report", lambda a: "<p>SEC</p>")
    monkeypatch.setenv("SEC_NOTIFICATION_STATE_PATH", str(tmp_path / "state.json"))
    return run_date


def test_checkpoint_only_after_accepted_email(monkeypatch, tmp_path):
    day = _fake_report(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(sr, "send_gmail", lambda html, date: sent.append(date))
    sr.run(day)
    assert sent == [day]
    assert last_sent_quarter() == "2026-09-30"


def test_no_checkpoint_if_smtp_fails(monkeypatch, tmp_path):
    day = _fake_report(monkeypatch, tmp_path)
    def fail(*args):
        raise ConnectionError("SMTP unavailable")
    monkeypatch.setattr(sr, "send_gmail", fail)
    with pytest.raises(ConnectionError):
        sr.run(day)
    assert last_sent_quarter() is None


def test_no_send_does_not_advance_checkpoint(monkeypatch, tmp_path):
    day = _fake_report(monkeypatch, tmp_path)
    monkeypatch.setattr(sr, "send_gmail", lambda *args: pytest.fail("SMTP must not be used"))
    sr.run(day, send=False)
    assert last_sent_quarter() is None


def test_failure_notification_requires_credentials(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="credentials"):
        nt.send_alert("job failed", "details")


def test_failure_notification_uses_report_recipient(monkeypatch):
    sent = []
    class FakeSMTP:
        def __init__(self, host, port):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def ehlo(self):
            pass
        def starttls(self):
            pass
        def login(self, user, pwd):
            pass
        def sendmail(self, sender, recipients, message):
            sent.append((sender, recipients, message))
    monkeypatch.setenv("GMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setenv("REPORT_RECIPIENT", "recipient@example.com")
    monkeypatch.setattr(nt.smtplib, "SMTP", FakeSMTP)
    nt.send_alert("job failed", "details")
    assert sent[0][0] == "sender@example.com"
    assert sent[0][1] == ["recipient@example.com"]
