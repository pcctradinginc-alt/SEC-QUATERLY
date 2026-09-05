"""Regression tests for report delivery (see run 33991286152)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import send_report as sr  # noqa: E402


def test_blank_report_recipient_falls_back_to_gmail_address():
    """GitHub Actions defines an unset secret as "", and os.environ.get returns
    that empty value rather than the default - the report then went to "" and
    Gmail answered 555."""
    assert sr.resolve_recipient({"REPORT_RECIPIENT": "", "GMAIL_ADDRESS": "me@example.com"}) == "me@example.com"
    assert sr.resolve_recipient({"REPORT_RECIPIENT": "   ", "GMAIL_ADDRESS": "me@example.com"}) == "me@example.com"
    assert sr.resolve_recipient({"GMAIL_ADDRESS": "me@example.com"}) == "me@example.com"


def test_explicit_recipient_wins():
    env = {"REPORT_RECIPIENT": " you@example.com ", "GMAIL_ADDRESS": "me@example.com"}
    assert sr.resolve_recipient(env) == "you@example.com"


def test_send_refuses_a_non_address(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "not-an-address")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    monkeypatch.delenv("REPORT_RECIPIENT", raising=False)
    with pytest.raises(ValueError, match="not an e-mail address"):
        sr.send_gmail("<p>x</p>", "2026-09-05")


def test_send_requires_credentials(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
    with pytest.raises(ValueError, match="must be set"):
        sr.send_gmail("<p>x</p>", "2026-09-05")
