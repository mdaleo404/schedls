"""Interactive input collection for the opt-in guided wizard.

The wizard only gathers input.  It never builds a plan, writes a file or runs a
command: callers turn the collected values into a :class:`JobSpec` and reuse the
normal preview/confirm/apply path.  Prompts are line-based and use the standard
input stream only; no editor, pager or extra dependency is involved.
"""

from __future__ import annotations

import re
import shlex
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .convenience import (
    daily_calendar,
    monthly_calendar,
    weekly_calendar,
)
from .errors import ConfirmationRequiredError, SchedlsError
from .models import Backend, Command
from .output import Output
from .renderers import cron as cron_renderer

InputFn = Callable[[str], str]
Validator = Callable[[str], object]
CalendarValidator = Callable[[str], object]

_ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _valid_time(value: str) -> str:
    daily_calendar(value)
    return value


def _valid_day(value: str) -> str:
    weekly_calendar(value, "00:00")
    return value


def _valid_month_day(value: str) -> str:
    monthly_calendar(value, "00:00")
    return value


def _valid_cron(value: str) -> str:
    cron_renderer.validate_expression(value)
    return value


@dataclass
class Prompter:
    """Collects values from an interactive terminal.

    ``input_fn`` is resolved at call time so callers and tests can substitute
    the builtin.  ``calendar_validator`` is an optional callable (typically the
    systemd backend's ``validate_calendar``) used to re-prompt on a bad native
    ``OnCalendar`` expression.
    """

    output: Output
    input_fn: InputFn | None = None
    calendar_validator: CalendarValidator | None = None

    def require_terminal(self) -> None:
        if not sys.stdin.isatty():
            raise ConfirmationRequiredError(
                "interactive mode requires a terminal.",
                hint="Re-run without --interactive and pass flags, or run from a terminal.",
            )

    def text(
        self,
        prompt: str,
        *,
        default: str | None = None,
        validator: Validator | None = None,
    ) -> str:
        label = f"{prompt} [{default}]" if default else prompt
        while True:
            raw = self._read(f"{label}: ").strip()
            if not raw:
                if default is not None:
                    raw = default
                else:
                    self.output.line("A value is required.")
                    continue
            if validator is None:
                return raw
            try:
                validator(raw)
            except SchedlsError as exc:
                hint = f" {exc.hint}" if exc.hint else ""
                self.output.line(f"{exc.message}{hint}")
                continue
            return raw

    def optional_text(self, prompt: str, *, default: str | None = None) -> str | None:
        label = f"{prompt} [{default}]" if default else f"{prompt} (optional)"
        raw = self._read(f"{label}: ").strip()
        if not raw:
            return default
        return raw

    def yes_no(self, prompt: str, *, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            raw = self._read(f"{prompt} {suffix} ").strip().lower()
            if not raw:
                return default
            if raw in {"y", "yes"}:
                return True
            if raw in {"n", "no"}:
                return False
            self.output.line("Please answer 'y' or 'n'.")

    def choice(self, prompt: str, options: Sequence[tuple[str, str]], *, default: str | None = None) -> str:
        self.output.line(prompt)
        width = len(str(len(options)))
        for index, (_, label) in enumerate(options, start=1):
            self.output.line(f"  {str(index).rjust(width)}. {label}")
        values = [value for value, _ in options]
        hint = f"{prompt} [{default}]" if default else prompt
        while True:
            raw = self._read(f"{hint}: ").strip()
            if not raw and default is not None:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1][0]
            if raw in values:
                return raw
            self.output.line("Enter a number from the list.")

    def command(self) -> Command:
        if self.yes_no("Run the command through a shell (/bin/sh -c)?", default=False):
            return Command(shell=True, raw=self.text("Shell command"))
        while True:
            raw = self.text("Command")
            try:
                argv = tuple(shlex.split(raw))
            except ValueError as exc:
                self.output.line(f"Could not parse the command: {exc}")
                continue
            if not argv:
                self.output.line("A command is required.")
                continue
            return Command(argv=argv)

    def environment(self) -> list[str]:
        self.output.line("Environment variables (KEY=VALUE, blank line to finish):")
        entries: list[str] = []
        while True:
            raw = self._read("  KEY=VALUE: ").strip()
            if not raw:
                return entries
            key, sep, _ = raw.partition("=")
            if not sep or not _ENV_NAME_RE.match(key):
                self.output.line("Expected KEY=VALUE with a valid variable name.")
                continue
            entries.append(raw)

    def schedule(self, backend: Backend) -> dict[str, object]:
        options = [
            ("daily", "Daily at a time"),
            ("weekdays", "Weekdays (Mon-Fri) at a time"),
            ("weekly", "Weekly on a day of the week"),
            ("monthly", "Monthly on a day of the month"),
            ("custom", "Custom native expression"),
        ]
        kind = self.choice("Schedule:", options, default="daily")
        if kind == "daily":
            return {"daily": self.text("Time (HH:MM)", validator=_valid_time)}
        if kind == "weekdays":
            return {"weekdays": self.text("Time (HH:MM)", validator=_valid_time)}
        if kind == "weekly":
            day = self.text("Day of week (sun..sat)", validator=_valid_day)
            return {"weekly": [day, self.text("Time (HH:MM)", validator=_valid_time)]}
        if kind == "monthly":
            day = self.text("Day of month (1-31)", validator=_valid_month_day)
            return {"monthly": [day, self.text("Time (HH:MM)", validator=_valid_time)]}
        if backend is Backend.SYSTEMD:
            return {"calendar": [self.text("OnCalendar expression", validator=self._validate_calendar)]}
        return {"cron_expr": self.text("Cron expression (five fields)", validator=_valid_cron)}

    def _validate_calendar(self, value: str) -> str:
        if self.calendar_validator is not None:
            self.calendar_validator(value)
        return value

    def _read(self, label: str) -> str:
        fn = self.input_fn or input
        try:
            return fn(label)
        except EOFError:
            raise ConfirmationRequiredError(
                "interactive input ended unexpectedly.",
                hint="Run without --interactive or provide answers in a terminal.",
            ) from None
