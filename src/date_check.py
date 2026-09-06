"""
date_check.py
Determines if today is a valid SEC 13F analysis run date.
Target dates: 16th of May, Aug, Nov, Feb (Berlin time)
Weekend/holiday → next business day
Writes GitHub Actions output: should_run=true/false
"""

import os
import sys
from datetime import date, timedelta

import holidays


TARGET_MONTHS = {2, 5, 8, 11}
TARGET_DAY = 16

# The filing deadline this pipeline follows is a US one, so the calendar has to
# be US federal holidays - not German ones. A run landing on Independence Day or
# Thanksgiving would otherwise fire on a day EDGAR support and the markets are
# shut, and the option quotes would be a stale snapshot.
US_HOLIDAYS = holidays.UnitedStates(years=range(2024, 2036))


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in US_HOLIDAYS


def next_business_day(d: date) -> date:
    """Advance d until it is a US business day (weekends and federal holidays)."""
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def get_expected_run_date(ref: date) -> date:
    """
    For the given reference date's month/year, return the actual
    run date (16th of that month → adjusted for weekends).
    Only valid if ref.month is in TARGET_MONTHS.
    """
    target = date(ref.year, ref.month, TARGET_DAY)
    return next_business_day(target)


def should_run_today(today: date, force: bool = False, target_override: str = "") -> bool:
    if force:
        print(f"FORCE_RUN=true → running regardless of date ({today})")
        return True

    if target_override:
        try:
            override = date.fromisoformat(target_override)
            result = today == override
            print(f"Target override {override}: today={today}, match={result}")
            return result
        except ValueError:
            print(f"Invalid TARGET_DATE '{target_override}', ignoring.")

    if today.month not in TARGET_MONTHS:
        print(f"Month {today.month} is not a target month {TARGET_MONTHS}. Skip.")
        return False

    expected = get_expected_run_date(today)
    result = today == expected
    print(f"Expected run date for {today.year}-{today.month:02d}: {expected}. Today: {today}. Run: {result}")
    return result


def write_github_output(key: str, value: str):
    """Write to GITHUB_OUTPUT file (new Actions syntax)."""
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        # Local testing fallback
        print(f"[local] {key}={value}")


if __name__ == "__main__":
    today = date.today()
    force = os.environ.get("FORCE_RUN", "false").lower() == "true"
    target_override = os.environ.get("TARGET_DATE", "").strip()

    run = should_run_today(today, force=force, target_override=target_override)
    write_github_output("should_run", "true" if run else "false")

    if not run:
        print("Not a run date. Pipeline will stop here.")
        sys.exit(0)

    print(f"✅ Run date confirmed: {today}")
