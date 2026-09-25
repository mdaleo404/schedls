"""Presentation-only human descriptions of schedules.

This module never validates or interprets schedule *meaning* for execution.
When a form is not recognised with full confidence the native expression is
shown unchanged.  Correctness beats prettiness.
"""

from __future__ import annotations

import re

from .models import Schedule, ScheduleKind

_WEEKDAY_NAMES = {
    0: "Sunday",
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
}

_WEEKDAYS_INDEX = {"Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6, "Sun": 0}

_CAL_DAILY = re.compile(r"^\*-\*-\*\s+(\d{1,2}):(\d{2}):(\d{2})$")
_CAL_WEEKDAYS = re.compile(r"^Mon\.\.Fri\s+\*-\*-\*\s+(\d{1,2}):(\d{2}):(\d{2})$")
_CAL_SINGLE_WEEKDAY = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\*-\*-\*\s+(\d{1,2}):(\d{2}):(\d{2})$")
_CAL_MONTHLY = re.compile(r"^\*-\*\-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})$")

_CRON_FIELDS = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")


def _clock(hour: str, minute: str, second: str) -> str:
    if second in {"00", "0"}:
        return f"{int(hour):02d}:{int(minute):02d}"
    return f"{int(hour):02d}:{int(minute):02d}:{int(second):02d}"


def describe_calendar(expression: str) -> str | None:
    expr = expression.strip()
    match = _CAL_WEEKDAYS.match(expr)
    if match:
        return f"weekdays at {_clock(*match.groups())}"
    match = _CAL_SINGLE_WEEKDAY.match(expr)
    if match:
        return f"{_WEEKDAY_NAMES[_WEEKDAYS_INDEX[match.group(1)]]} at {_clock(*match.groups()[1:])}"
    match = _CAL_MONTHLY.match(expr)
    if match:
        day, hour, minute, second = match.groups()
        return f"monthly on day {int(day)} at {_clock(hour, minute, second)}"
    match = _CAL_DAILY.match(expr)
    if match:
        return f"daily at {_clock(*match.groups())}"
    if expr == "daily":
        return "daily"
    return None


def describe_cron(expression: str) -> str | None:
    expr = expression.strip()
    if not _CRON_FIELDS.match(expr):
        return None
    minute, hour, dom, month, dow = expr.split()
    try:
        minute_i = int(minute)
        hour_i = int(hour)
    except ValueError:
        return None
    if not (0 <= minute_i <= 59 and 0 <= hour_i <= 23):
        return None
    clock = f"{hour_i:02d}:{minute_i:02d}"
    if dom == "*" and month == "*":
        if dow == "*":
            return f"daily at {clock}"
        if dow == "1-5":
            return f"weekdays at {clock}"
        if dow.isdigit() and 0 <= int(dow) <= 6:
            return f"{_WEEKDAY_NAMES[int(dow)]} at {clock}"
    if dom.isdigit() and month == "*" and dow == "*":
        return f"monthly on day {int(dom)} at {clock}"
    return None


def describe_schedule(schedule: Schedule) -> str:
    if schedule.kind is ScheduleKind.CALENDAR:
        expression = schedule.expression
        described = describe_calendar(expression)
        if described and len(schedule.all_expressions()) == 1:
            return described
        if len(schedule.all_expressions()) > 1:
            return " + ".join(schedule.all_expressions())
        return expression or "—"
    described = describe_cron(schedule.expression)
    return described or schedule.expression or "—"
