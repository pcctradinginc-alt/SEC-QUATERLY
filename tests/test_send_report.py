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


def test_truncated_issuer_name_is_cleaned():
    """13F filers hand-type the issuer name; Elevance arrives truncated mid-phrase."""
    c = sr.clean_issuer_name
    assert c("ELEVANCE HEALTH INC FORMERLY") == "ELEVANCE HEALTH INC"
    assert c("ACME INC FKA") == "ACME INC"
    assert c("BERKSHIRE HATHAWAY INC DEL") == "BERKSHIRE HATHAWAY INC DEL"
    assert c("KRAFT HEINZ CO") == "KRAFT HEINZ CO"
    assert c(None) == ""


def test_send_requires_credentials(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
    with pytest.raises(ValueError, match="must be set"):
        sr.send_gmail("<p>x</p>", "2026-09-05")


def test_current_version_never_inherits_legacy_backtest_alpha():
    summary = {"completed_90d": 10, "avg_excess_90d_pct": -1.4,
               "by_engine": {"v1_llm_top5": {
                   "signals": 10, "avg_excess_90d_pct": -1.4,
                   "avg_return_90d_pct": 6.5, "avg_benchmark_90d_pct": 7.9,
                   "beat_benchmark_pct": 40.0}}}
    html = sr.track_record_block({"summary": summary}, "v2.2_material_streak")
    assert "no 90-day outcomes" in html
    assert "not established" in html
    assert "v1_llm_top5" in html
    assert "non-comparable" in html
    assert "observed excess" not in html


def test_only_current_engine_can_supply_reported_forward_excess():
    backtest = {"summary": {"by_engine": {
        "v1_llm_top5": {"signals": 10, "avg_excess_90d_pct": 12.0},
        "v2.2_material_streak": {
            "signals": 2, "avg_excess_90d_pct": -3.0,
            "avg_return_90d_pct": 2.0,
            "avg_benchmark_90d_pct": 5.0,
            "beat_benchmark_pct": 0.0,
        }}}}
    html = sr.track_record_block(backtest, "v2.2_material_streak")
    assert "-3.0% observed excess" in html
    assert "+12.0% observed excess" not in html
