"""Render cron entries and managed blocks.

Cron executes command text through a shell.  schedls keeps an argument vector
internally and serializes it into a shell-safe command string, then escapes
``%`` because cron implementations such as Cronie give it special meaning.  If
a safe representation cannot be guaranteed the operation is refused.
"""

from __future__ import annotations

import re
import shlex

from ..errors import InvalidScheduleError, SafetyRefusalError
from ..models import Command
from ..security import has_unsafe_control_characters

BLOCK_BEGIN = "# schedls:begin name={name}{extra}"
BLOCK_END = "# schedls:end name={name}"

_MARKER_RE = re.compile(r"^#\s*schedls:(?P<kind>begin|end)\s+(?P<meta>.*)$")
_FIELD_RE = re.compile(r"^[0-9A-Za-z*,/\-]+$")
_NICKNAMES = {
    "@reboot",
    "@yearly",
    "@annually",
    "@monthly",
    "@weekly",
    "@daily",
    "@midnight",
    "@hourly",
}
_RANGE_RE = re.compile(r"^[0-9]{1,2}-[0-9]{1,2}$")


def escape_percent(text: str) -> str:
    """Escape ``%`` for a cron command field.

    cron implementations such as Cronie translate an unescaped ``%`` into a
    newline and ``\\%`` into a literal ``%`` before handing the line to the
    shell.  Escaping *every* ``%`` is correct even when it follows a backslash:
    ``\\`` + ``%`` becomes ``\\\\%``, which cron reduces back to ``\\%``.
    """
    return text.replace("%", "\\%")


def render_command(command: Command) -> str:
    if command.shell:
        raw = command.raw or ""
        if has_unsafe_control_characters(raw):
            raise SafetyRefusalError("shell command must not contain NUL or newline")
        return escape_percent(raw)
    if not command.argv:
        raise InvalidScheduleError("no command given")
    for arg in command.argv:
        if has_unsafe_control_characters(arg):
            raise SafetyRefusalError("cron command arguments must not contain NUL or newline")
    quoted = " ".join(shlex.quote(arg) for arg in command.argv)
    return escape_percent(quoted)


def _validate_field(field: str, *, low: int, high: int, names: bool) -> None:
    value = field
    if value == "*":
        return
    for part in value.split(","):
        if not part:
            raise InvalidScheduleError(f"invalid cron field: {field!r}")
        step_text, _, step = part.partition("/")
        if step and (not step.isdigit() or int(step) == 0):
            raise InvalidScheduleError(f"invalid cron step in field: {field!r}")
        if step_text == "*":
            continue
        if _RANGE_RE.match(step_text):
            start_str, end_str = step_text.split("-")
            start, end = int(start_str), int(end_str)
            if start > end or not (low <= start <= high) or not (low <= end <= high):
                raise InvalidScheduleError(f"cron range out of bounds: {field!r}")
            continue
        if step_text.isdigit():
            number = int(step_text)
            if not low <= number <= high:
                raise InvalidScheduleError(f"cron value out of range: {field!r}")
            continue
        if names and step_text.isalpha():
            continue
        raise InvalidScheduleError(f"invalid cron field: {field!r}")


def validate_expression(expression: str) -> str:
    expr = expression.strip()
    if not expr:
        raise InvalidScheduleError("empty cron expression")
    if expr.startswith("@"):
        nickname = expr.split()[0].lower()
        if nickname not in _NICKNAMES:
            raise InvalidScheduleError(f"unknown cron nickname: {expr!r}")
        if expr.lower() != nickname:
            raise InvalidScheduleError(f"unexpected text after cron nickname: {expr!r}")
        return nickname
    fields = expr.split()
    if len(fields) != 5:
        raise InvalidScheduleError(
            f"cron expression must have five fields: {expression!r}",
            hint="minute hour day-of-month month day-of-week",
        )
    minute, hour, dom, month, dow = fields
    for value, low, high, names in (
        (minute, 0, 59, False),
        (hour, 0, 23, False),
        (dom, 1, 31, False),
        (month, 1, 12, True),
        (dow, 0, 7, True),
    ):
        if not _FIELD_RE.match(value):
            raise InvalidScheduleError(f"invalid cron field: {value!r}")
        _validate_field(value, low=low, high=high, names=names)
    return " ".join(fields)


def render_line(expression: str, command: Command) -> str:
    return f"{validate_expression(expression)} {render_command(command)}"


def render_block(name: str, lines: list[str], *, extra: str = "") -> str:
    body = [BLOCK_BEGIN.format(name=name, extra=extra), *lines, BLOCK_END.format(name=name)]
    return "\n".join(body) + "\n"
