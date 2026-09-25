"""Time and duration formatting helpers.

Time is displayed in the host's local timezone by default; exact values always
include timezone information.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_UNITS = (
    r"us|ms|secs|seconds|second|sec|s|mins|minutes|minute|min|m|"
    r"hrs|hours|hour|hr|h|days|day|d|weeks|week|w"
)

_DURATION_RE = re.compile(
    rf"^(?P<sign>-)?(?:\d+(?:\.\d+)?(?:{_UNITS})\s*)+$",
    re.IGNORECASE,
)

_DURATION_PART_RE = re.compile(
    rf"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{_UNITS})",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "w": 604800.0,
    "week": 604800.0,
    "weeks": 604800.0,
}

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def is_valid_duration(value: str) -> bool:
    return bool(_DURATION_RE.match(value.strip()))


def duration_seconds(value: str) -> float:
    """Parse a systemd-style duration into seconds. Raises ValueError."""
    text = value.strip()
    if not text:
        raise ValueError("empty duration")
    sign = 1.0
    if text.startswith("-"):
        sign = -1.0
        text = text[1:]
    total = 0.0
    matched = False
    pos = 0
    for match in _DURATION_PART_RE.finditer(text):
        if match.start() != pos:
            raise ValueError(f"invalid duration: {value!r}")
        pos = match.end()
        matched = True
        total += float(match.group("value")) * _UNIT_SECONDS[match.group("unit").lower()]
    if not matched or pos != len(text):
        raise ValueError(f"invalid duration: {value!r}")
    return sign * total


def _display(dt: datetime, *, utc: bool) -> datetime:
    if utc:
        return dt.astimezone(UTC)
    return dt.astimezone()


def local_zone_name(dt: datetime) -> str:
    return dt.tzname() or ""


def format_datetime(dt: datetime, *, utc: bool = False) -> str:
    """Full, unambiguous timestamp such as ``Fri 25 Sep 2026 02:00:00 BST``."""
    dt = _display(dt, utc=utc)
    weekday = _WEEKDAYS[dt.weekday()]
    month = _MONTHS[dt.month - 1]
    stamp = f"{weekday} {dt.day:02d} {month} {dt.year} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    zone = dt.tzname()
    if zone:
        stamp = f"{stamp} {zone}"
    return stamp


def format_clock(dt: datetime, *, utc: bool = False) -> str:
    dt = _display(dt, utc=utc)
    zone = dt.tzname()
    clock = f"{dt.hour:02d}:{dt.minute:02d}"
    return f"{clock} {zone}" if zone else clock


def format_short(dt: datetime, *, now: datetime | None = None, utc: bool = False) -> str:
    """Compact relative-ish timestamp such as ``today 02:00``/``tomorrow 02:00``."""
    dt = _display(dt, utc=utc)
    reference = now or datetime.now(dt.tzinfo or None)
    if utc:
        reference = reference.astimezone(UTC)
    day_delta = (dt.date() - reference.date()).days
    clock = f"{dt.hour:02d}:{dt.minute:02d}"
    if day_delta == 0:
        return f"today {clock}"
    if day_delta == 1:
        return f"tomorrow {clock}"
    if day_delta == -1:
        return f"yesterday {clock}"
    return f"{_WEEKDAYS[dt.weekday()]} {clock}"


def isoformat(dt: datetime) -> str:
    """ISO 8601 with an explicit timezone offset."""
    return dt.isoformat()


def parse_systemd_timestamp(value: str) -> datetime | None:
    """Parse the timestamp forms emitted by ``systemctl show``.

    Handles microsecond epoch values, ``usec``/``s`` suffixed values and the
    ``n/a`` placeholder.
    """
    text = value.strip()
    if not text or text in {"n/a", "0", "infinity"}:
        return None
    if text.isdigit():
        # systemctl prints realtime values in microseconds since the epoch.
        micros = int(text)
        if micros == 0:
            return None
        return datetime.fromtimestamp(micros / 1_000_000, tz=UTC).astimezone()
    for suffix, scale in (("us", 1e-6), ("ms", 1e-3), ("s", 1.0)):
        if text.endswith(suffix):
            try:
                seconds = float(text[: -len(suffix)]) * scale
            except ValueError:
                return None
            if seconds <= 0:
                return None
            return datetime.fromtimestamp(seconds, tz=UTC).astimezone()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        pretty = _parse_pretty(text)
        return pretty
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone()


def _parse_pretty(text: str) -> datetime | None:
    """Parse ``Thu 2026-09-24 06:49:28 BST`` (systemd's human form).

    The trailing zone abbreviation is ignored; the naive value is interpreted
    in the host's local timezone, which is how systemd renders it by default.
    """
    parts = text.split()
    if len(parts) >= 3:
        candidate = " ".join(parts[:3])
        try:
            parsed = datetime.strptime(candidate, "%a %Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return parsed.astimezone()
    return None


def now_utc() -> datetime:
    return datetime.now(UTC)


def add_seconds(dt: datetime, seconds: float) -> datetime:
    return dt + timedelta(seconds=seconds)
