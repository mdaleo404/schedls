from __future__ import annotations

import pytest

from schedls import convenience
from schedls.errors import InvalidScheduleError


@pytest.mark.parametrize(
    ("time", "expected"),
    [("02:00", "*-*-* 02:00:00"), ("2:05", "*-*-* 02:05:00"), ("23:59:59", "*-*-* 23:59:59")],
)
def test_daily_calendar(time: str, expected: str) -> None:
    assert convenience.daily_calendar(time) == expected


def test_weekdays_and_weekly_calendar() -> None:
    assert convenience.weekdays_calendar("08:30") == "Mon..Fri *-*-* 08:30:00"
    assert convenience.weekly_calendar("sun", "04:00") == "Sun *-*-* 04:00:00"
    assert convenience.weekly_calendar("Saturday", "4:00") == "Sat *-*-* 04:00:00"
    assert convenience.monthly_calendar("1", "06:00") == "*-*-01 06:00:00"


def test_cron_variants() -> None:
    assert convenience.daily_cron("02:00") == "0 2 * * *"
    assert convenience.weekdays_cron("08:30") == "30 8 * * 1-5"
    assert convenience.weekly_cron("sun", "04:00") == "0 4 * * 0"
    assert convenience.monthly_cron("15", "06:00") == "0 6 15 * *"


@pytest.mark.parametrize("bad", ["25:00", "02:60", "nope", "2", "02:00:99"])
def test_invalid_time(bad: str) -> None:
    with pytest.raises(InvalidScheduleError):
        convenience.daily_calendar(bad)


def test_invalid_weekday_and_day() -> None:
    with pytest.raises(InvalidScheduleError):
        convenience.weekly_calendar("funday", "04:00")
    with pytest.raises(InvalidScheduleError):
        convenience.monthly_calendar("32", "06:00")
    with pytest.raises(InvalidScheduleError):
        convenience.monthly_calendar("x", "06:00")
