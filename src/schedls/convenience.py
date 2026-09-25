"""Compile convenience flags into native scheduler expressions.

These forms are sugar only; they always compile to native ``OnCalendar=`` or
five-field cron syntax and never introduce a new scheduling engine.
"""

from __future__ import annotations

import re

from .errors import InvalidScheduleError

_TIME_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?$")

_WEEKDAY_CALENDAR = {
    "sun": "Sun",
    "sunday": "Sun",
    "mon": "Mon",
    "monday": "Mon",
    "tue": "Tue",
    "tuesday": "Tue",
    "wed": "Wed",
    "wednesday": "Wed",
    "thu": "Thu",
    "thursday": "Thu",
    "fri": "Fri",
    "friday": "Fri",
    "sat": "Sat",
    "saturday": "Sat",
}

_CRON_DOW = {
    "sun": "0",
    "sunday": "0",
    "mon": "1",
    "monday": "1",
    "tue": "2",
    "tuesday": "2",
    "wed": "3",
    "wednesday": "3",
    "thu": "4",
    "thursday": "4",
    "fri": "5",
    "friday": "5",
    "sat": "6",
    "saturday": "6",
}


def _parse_time(value: str) -> tuple[int, int, int]:
    match = _TIME_RE.match(value.strip())
    if not match:
        raise InvalidScheduleError(
            f"invalid time of day: {value!r}",
            hint="Expected HH:MM or HH:MM:SS (24-hour clock).",
        )
    hour = int(match.group("h"))
    minute = int(match.group("m"))
    second = int(match.group("s") or 0)
    if hour > 23 or minute > 59 or second > 59:
        raise InvalidScheduleError(f"invalid time of day: {value!r}")
    return hour, minute, second


def daily_calendar(time: str) -> str:
    hour, minute, second = _parse_time(time)
    return f"*-*-* {hour:02d}:{minute:02d}:{second:02d}"


def weekdays_calendar(time: str) -> str:
    hour, minute, second = _parse_time(time)
    return f"Mon..Fri *-*-* {hour:02d}:{minute:02d}:{second:02d}"


def weekly_calendar(day: str, time: str) -> str:
    dow = _WEEKDAY_CALENDAR.get(day.strip().lower())
    if dow is None:
        raise InvalidScheduleError(
            f"invalid weekday: {day!r}",
            hint="Use one of: sun, mon, tue, wed, thu, fri, sat.",
        )
    hour, minute, second = _parse_time(time)
    return f"{dow} *-*-* {hour:02d}:{minute:02d}:{second:02d}"


def monthly_calendar(day: str, time: str) -> str:
    try:
        day_i = int(day)
    except ValueError:
        raise InvalidScheduleError(f"invalid day of month: {day!r}") from None
    if not 1 <= day_i <= 31:
        raise InvalidScheduleError(f"day of month out of range: {day!r}")
    hour, minute, second = _parse_time(time)
    return f"*-*-{day_i:02d} {hour:02d}:{minute:02d}:{second:02d}"


def daily_cron(time: str) -> str:
    hour, minute, _ = _parse_time(time)
    return f"{minute} {hour} * * *"


def weekdays_cron(time: str) -> str:
    hour, minute, _ = _parse_time(time)
    return f"{minute} {hour} * * 1-5"


def weekly_cron(day: str, time: str) -> str:
    dow = _CRON_DOW.get(day.strip().lower())
    if dow is None:
        raise InvalidScheduleError(
            f"invalid weekday: {day!r}",
            hint="Use one of: sun, mon, tue, wed, thu, fri, sat.",
        )
    hour, minute, _ = _parse_time(time)
    return f"{minute} {hour} * * {dow}"


def monthly_cron(day: str, time: str) -> str:
    try:
        day_i = int(day)
    except ValueError:
        raise InvalidScheduleError(f"invalid day of month: {day!r}") from None
    if not 1 <= day_i <= 31:
        raise InvalidScheduleError(f"day of month out of range: {day!r}")
    hour, minute, _ = _parse_time(time)
    return f"{minute} {hour} {day_i} * *"
