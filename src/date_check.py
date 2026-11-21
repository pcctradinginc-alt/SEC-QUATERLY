"""Determine when an SEC 13F quarter is due and whether its report was e-mailed.

The SEC deadline is 45 calendar days after quarter-end, adjusted to the
next US business day. We run AFTER the deadline, so late-day filings are in
EDGAR. Every scheduled check can catch up after missed GitHub cron events.
"""
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import holidays

from email_state import last_sent_quarter

US_HOLIDAYS = holidays.UnitedStates(years=range(2024, 2041))
QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))
DEADLINE_MONTHS = (2, 5, 8, 11)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in US_HOLIDAYS


def next_business_day(d: date) -> date:
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def filing_deadline(quarter_end: date) -> date:
    """SEC Form 13F filing deadline for the quarter's period of report."""
    if (quarter_end.month, quarter_end.day) not in QUARTER_ENDS:
        raise ValueError(f"Not a quarter end: {quarter_end}")
    return next_business_day(quarter_end + timedelta(days=45))


def get_expected_run_date(ref: date) -> date:
    """Filing deadline for the 13F filing month (legacy public API)."""
    if ref.month not in DEADLINE_MONTHS:
        raise ValueError(f"{ref.month} is not a 13F filing month")
    year = ref.year - 1 if ref.month == 2 else ref.year
    month_day = {2: (12, 31), 5: (3, 31), 8: (6, 30), 11: (9, 30)}
    q = date(year, *month_day[ref.month])
    return filing_deadline(q)


def latest_due_quarter(today: date) -> tuple[str, date]:
    """Latest due reporting quarter, including a missed quarter after its month."""
    quarters = [
        date(year, month, day)
        for year in (today.year - 2, today.year - 1, today.year)
        for month, day in QUARTER_ENDS
    ]
    eligible = [q for q in quarters if q < today and filing_deadline(q) < today]
    if not eligible:
        raise ValueError(f"No due quarter identified for {today}")
    last = max(eligible)
    return last.isoformat(), filing_deadline(last)


def should_run_today(today: date, force: bool = False, target_override: str = "") -> bool:
    if force:
        print(f"FORCE_RUN=true: explicit manual re-run on {today}; may resend a report")
        return True
    if target_override:
        try:
            target = date.fromisoformat(target_override)
        except ValueError as exc:
            raise ValueError(f"Invalid TARGET_DATE: {target_override!r}") from exc
        return today == target

    quarter, deadline = latest_due_quarter(today)
    sent = last_sent_quarter()
    if sent == quarter:
        print(f"Quarter {quarter} already e-mailed; no new report is needed")
        return False
    print(f"SEC 13F quarter {quarter} was due {deadline}; "
          f"last e-mailed={sent or 'never'}; catch-up is required")
    return True


def write_github_output(key: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT", "")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")
    else:
        print(f"[local] {key}={value}")


if __name__ == "__main__":
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    force = os.environ.get("FORCE_RUN", "false").strip().lower() == "true"
    target_override = os.environ.get("TARGET_DATE", "").strip()
    run = should_run_today(today, force=force, target_override=target_override)
    write_github_output("should_run", "true" if run else "false")
    if run:
        print(f"Scheduled analysis due as of {today}")
    else:
        print(f"No unnotified SEC quarter due as of {today}")
