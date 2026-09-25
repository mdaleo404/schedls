from __future__ import annotations

from schedls.describe import describe_calendar, describe_cron, describe_schedule
from schedls.models import Schedule, ScheduleKind


def test_describe_calendar() -> None:
    assert describe_calendar("*-*-* 02:00:00") == "daily at 02:00"
    assert describe_calendar("Mon..Fri *-*-* 08:30:00") == "weekdays at 08:30"
    assert describe_calendar("Sun *-*-* 04:00:00") == "Sunday at 04:00"
    assert describe_calendar("*-*-01 06:00:00") == "monthly on day 1 at 06:00"
    assert describe_calendar("*-*-* *:00:00") is None


def test_describe_cron() -> None:
    assert describe_cron("0 2 * * *") == "daily at 02:00"
    assert describe_cron("30 8 * * 1-5") == "weekdays at 08:30"
    assert describe_cron("0 4 * * 0") == "Sunday at 04:00"
    assert describe_cron("0 6 15 * *") == "monthly on day 15 at 06:00"
    assert describe_cron("0 4 1,15 * 5") is None
    assert describe_cron("@daily") is None


def test_describe_schedule() -> None:
    assert describe_schedule(Schedule(ScheduleKind.CALENDAR, "*-*-* 02:00:00")) == "daily at 02:00"
    assert describe_schedule(Schedule(ScheduleKind.CRON, "@reboot")) == "@reboot"
    multi = Schedule(ScheduleKind.CALENDAR, "A", ("A", "B"))
    assert describe_schedule(multi) == "A + B"
