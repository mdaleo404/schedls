"""Calculate upcoming occurrences for the cron syntax schedls accepts."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

_NICKNAMES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def next_occurrence(expression: str, *, after: datetime | None = None) -> datetime | None:
    """Return the next local occurrence, or ``None`` when it cannot be known.

    ``@reboot`` and unsupported foreign expressions have no calculable
    wall-clock next occurrence.
    """
    expression = expression.lower()
    if expression == "@reboot":
        return None
    expression = _NICKNAMES.get(expression, expression)
    try:
        minute_text, hour_text, dom_text, month_text, dow_text = expression.split()
        minutes, _ = _field_values(minute_text, 0, 59)
        hours, _ = _field_values(hour_text, 0, 23)
        days, dom_any = _field_values(dom_text, 1, 31)
        months, _ = _field_values(month_text, 1, 12, aliases=_MONTHS)
        weekdays, dow_any = _field_values(dow_text, 0, 7, aliases=_WEEKDAYS)
    except ValueError:
        return None

    weekdays = {value % 7 for value in weekdays}
    reference = after or datetime.now(_local_timezone())
    if reference.tzinfo is None:
        raise ValueError("after must include a timezone")
    zone = reference.tzinfo
    reference = reference.astimezone(zone)
    times = [(hour, minute) for hour in sorted(hours) for minute in sorted(minutes)]

    # Gregorian leap days can be eight years apart across a non-leap century.
    for offset in range(9 * 366):
        candidate_date = reference.date() + timedelta(days=offset)
        if not _date_matches(candidate_date, months, days, weekdays, dom_any=dom_any, dow_any=dow_any):
            continue
        for hour, minute in times:
            candidate = datetime.combine(candidate_date, time(hour, minute), tzinfo=zone)
            if not _is_real_local_time(candidate, zone) or candidate <= reference:
                continue
            return candidate
    return None


def _field_values(
    field: str,
    low: int,
    high: int,
    *,
    aliases: dict[str, int] | None = None,
) -> tuple[set[int], bool]:
    values: set[int] = set()
    unrestricted = field == "*"
    for part in field.split(","):
        base, separator, step_text = part.partition("/")
        step = int(step_text) if separator else 1
        if step < 1:
            raise ValueError("invalid step")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _field_value(start_text, aliases)
            end = _field_value(end_text, aliases)
        else:
            start = end = _field_value(base, aliases)
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise ValueError("field out of range")
        values.update(range(start, end + 1, step))
    return values, unrestricted


def _field_value(value: str, aliases: dict[str, int] | None) -> int:
    if aliases and value in aliases:
        return aliases[value]
    return int(value)


def _date_matches(
    candidate: date,
    months: set[int],
    days: set[int],
    weekdays: set[int],
    *,
    dom_any: bool,
    dow_any: bool,
) -> bool:
    if candidate.month not in months:
        return False
    dom_matches = candidate.day in days
    cron_weekday = (candidate.weekday() + 1) % 7
    dow_matches = cron_weekday in weekdays
    if dom_any:
        return dow_matches
    if dow_any:
        return dom_matches
    return dom_matches or dow_matches


def _is_real_local_time(candidate: datetime, zone: tzinfo) -> bool:
    normalized = candidate.astimezone(UTC).astimezone(zone)
    return normalized.replace(tzinfo=None) == candidate.replace(tzinfo=None)


def _local_timezone() -> tzinfo:
    try:
        with open("/etc/localtime", "rb") as handle:
            return ZoneInfo.from_file(handle)
    except (OSError, ValueError):
        return datetime.now().astimezone().tzinfo or UTC
