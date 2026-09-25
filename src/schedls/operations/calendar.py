"""``schedls calendar`` — validate and explore systemd calendar syntax."""

from __future__ import annotations

from datetime import UTC, datetime

from ..errors import DependencyMissingError, InvalidScheduleError
from ..output import Output
from ..runner import CommandRunner
from ..security import has_unsafe_control_characters
from ..timefmt import format_datetime


def run_calendar(
    runner: CommandRunner,
    output: Output,
    expression: str,
    *,
    next_count: int = 5,
) -> None:
    if has_unsafe_control_characters(expression):
        raise InvalidScheduleError("calendar expressions must not contain control characters")
    if expression.startswith("-"):
        raise InvalidScheduleError(f"calendar expression must not start with '-': {expression!r}")
    if not runner.has("systemd-analyze"):
        raise DependencyMissingError("systemd-analyze is not available; cannot evaluate calendar expressions.")
    completed = runner.run(
        [
            "systemd-analyze",
            "calendar",
            f"--iterations={max(next_count, 1)}",
            expression,
        ],
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise InvalidScheduleError(f"invalid calendar expression: {expression!r}", hint=detail or None)
    original, normalized, occurrences = _parse(completed.stdout)
    if output.json_mode:
        output.emit_json(
            {
                "schema_version": 1,
                "expression": expression,
                "original": original,
                "normalized": normalized,
                "next_occurrences": [_iso(dt, utc=output.utc) for dt in occurrences],
            }
        )
        return
    output.key_values([("Expression", original or expression)])
    output.line()
    output.key_values([("Normalized", normalized or expression)])
    output.line()
    output.heading("Next occurrences")
    for occurrence in occurrences:
        output.line(f"  {format_datetime(occurrence, utc=output.utc)}")


def _iso(dt: datetime, *, utc: bool) -> str:
    if utc:
        dt = dt.astimezone(UTC)
    return dt.isoformat()


def _parse(text: str) -> tuple[str, str, list[datetime]]:
    original = ""
    normalized = ""
    occurrences: list[datetime | None] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Original form:"):
            original = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Normalized form:"):
            normalized = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Next elapse:") or stripped.startswith("Iteration #"):
            occurrences.append(_parse_stamp(stripped.split(":", 1)[1].strip()))
        elif stripped.startswith("(in UTC):") and occurrences:
            parsed = _parse_utc(stripped.split(":", 1)[1].strip())
            if parsed is not None:
                occurrences[-1] = parsed
    return original, normalized, [item for item in occurrences if item is not None]


def _parse_stamp(stamp: str) -> datetime | None:
    parts = stamp.split()
    if len(parts) >= 3:
        candidate = " ".join(parts[:3])
        try:
            parsed = datetime.strptime(candidate, "%a %Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return parsed.astimezone()
    return None


def _parse_utc(stamp: str) -> datetime | None:
    text = stamp.strip()
    if text.endswith(" UTC"):
        text = text[: -len(" UTC")]
    try:
        parsed = datetime.strptime(text, "%a %Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)
