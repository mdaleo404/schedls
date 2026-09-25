from __future__ import annotations

import io

import pytest

from schedls.errors import InvalidScheduleError
from schedls.operations.calendar import run_calendar
from schedls.output import Output
from schedls.runner import CommandRunner

pytestmark = pytest.mark.integration


def _has_analyze() -> bool:
    return CommandRunner().has("systemd-analyze")


def test_calendar_valid() -> None:
    if not _has_analyze():
        pytest.skip("systemd-analyze not available")
    stream = io.StringIO()
    output = Output(color="never", stdout=stream)
    run_calendar(CommandRunner(), output, "Mon..Fri 02:30", next_count=3)
    text = stream.getvalue()
    assert "Normalized" in text
    assert "Mon..Fri *-*-* 02:30:00" in text


def test_calendar_invalid() -> None:
    if not _has_analyze():
        pytest.skip("systemd-analyze not available")
    stream = io.StringIO()
    output = Output(color="never", stdout=stream)
    with pytest.raises(InvalidScheduleError):
        run_calendar(CommandRunner(), output, "definitely not a calendar", next_count=1)
