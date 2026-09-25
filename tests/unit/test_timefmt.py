from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from schedls import timefmt


@pytest.fixture
def london_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("1s", 1.0),
        ("30m", 1800.0),
        ("1h", 3600.0),
        ("2h30m", 9000.0),
        ("1d", 86400.0),
        ("1w", 604800.0),
        ("500ms", 0.5),
        ("1min", 60.0),
    ],
)
def test_duration_seconds(value: str, seconds: float) -> None:
    assert timefmt.duration_seconds(value) == pytest.approx(seconds)


@pytest.mark.parametrize("bad", ["", "abc", "1x", "h", "1h2x"])
def test_invalid_duration(bad: str) -> None:
    with pytest.raises(ValueError):
        timefmt.duration_seconds(bad)
    assert not timefmt.is_valid_duration(bad)


def test_parse_microseconds() -> None:
    parsed = timefmt.parse_systemd_timestamp("1758700800000000")
    assert parsed is not None
    assert parsed.year == 2025


def test_parse_pretty() -> None:
    parsed = timefmt.parse_systemd_timestamp("Thu 2026-09-24 06:49:28 BST")
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 9, 24)
    assert parsed.tzinfo is not None


def test_parse_placeholders() -> None:
    assert timefmt.parse_systemd_timestamp("") is None
    assert timefmt.parse_systemd_timestamp("n/a") is None
    assert timefmt.parse_systemd_timestamp("0") is None


def test_format_datetime_round_trip_zone(london_tz) -> None:
    dt = datetime(2026, 9, 25, 2, 0, 0, tzinfo=timezone(timedelta(hours=1)))
    text = timefmt.format_datetime(dt)
    assert text.startswith("Fri 25 Sep 2026 02:00:00")
    assert text.endswith("BST")


def test_format_short_relative() -> None:
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    today = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)
    tomorrow = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)
    assert timefmt.format_short(today, now=now, utc=True) == "today 18:00"
    assert timefmt.format_short(tomorrow, now=now, utc=True) == "tomorrow 03:00"


def test_isoformat_has_offset() -> None:
    dt = datetime(2026, 9, 25, 2, 0, tzinfo=UTC)
    assert timefmt.isoformat(dt).endswith("+00:00")
