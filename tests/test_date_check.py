"""The filing deadline is a US one, so the calendar must be too."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import date_check as dc  # noqa: E402


def test_weekends_are_skipped():
    assert dc.next_business_day(date(2026, 5, 16)) == date(2026, 5, 18)   # Sat -> Mon


def test_us_federal_holidays_are_skipped():
    """4 July 2026 falls on a Saturday, observed Friday 3 July."""
    assert not dc.is_business_day(date(2026, 7, 3))
    assert not dc.is_business_day(date(2025, 11, 27))                     # Thanksgiving
    assert dc.is_business_day(date(2026, 8, 17))


def test_german_holidays_do_not_block_a_us_run():
    """3 October is a German national holiday and an ordinary US trading day."""
    assert dc.is_business_day(date(2025, 10, 3))
