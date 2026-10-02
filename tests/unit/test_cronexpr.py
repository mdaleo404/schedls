from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from schedls.cronexpr import next_occurrence


def test_next_occurrence_for_hourly_schedule() -> None:
    after = datetime(2026, 10, 2, 12, 17, 30, tzinfo=UTC)

    assert next_occurrence("17 * * * *", after=after) == datetime(2026, 10, 2, 13, 17, tzinfo=UTC)


def test_next_occurrence_uses_cron_day_of_month_or_weekday() -> None:
    after = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

    assert next_occurrence("0 0 13 * fri", after=after) == datetime(2026, 10, 13, 0, 0, tzinfo=UTC)


def test_next_occurrence_treats_day_steps_as_restricted() -> None:
    after = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    assert next_occurrence("0 0 */2 * *", after=after) == datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    assert next_occurrence("0 0 * * */2", after=after) == datetime(2026, 10, 3, 0, 0, tzinfo=UTC)


def test_next_occurrence_supports_nicknames_and_reboot() -> None:
    after = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    assert next_occurrence("@daily", after=after) == datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    assert next_occurrence("@reboot", after=after) is None


def test_next_occurrence_skips_nonexistent_local_time() -> None:
    london = ZoneInfo("Europe/London")
    after = datetime(2026, 3, 29, 0, 0, tzinfo=london)

    assert next_occurrence("30 1 * * *", after=after) == datetime(2026, 3, 30, 1, 30, tzinfo=london)


def test_next_occurrence_handles_long_leap_day_gap() -> None:
    after = datetime(2096, 3, 1, 0, 0, tzinfo=UTC)

    assert next_occurrence("0 0 29 2 *", after=after) == datetime(2104, 2, 29, 0, 0, tzinfo=UTC)
