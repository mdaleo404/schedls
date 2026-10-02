"""Output abstraction: human tables, key/value views and stable JSON."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import IO, Any

from .models import ScheduledJob
from .timefmt import format_datetime, isoformat

SCHEMA_VERSION = 1

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_text(text: str) -> str:
    """Escape control characters that could manipulate a terminal.

    Newlines and tabs are preserved; every other C0/C1/DEL byte is rendered as
    a visible ``\\xNN`` sequence.
    """
    return _CONTROL_RE.sub(lambda match: f"\\x{ord(match.group(0)):02x}", text)


_COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}


class Output:
    def __init__(
        self,
        *,
        json_mode: bool = False,
        color: str = "auto",
        utc: bool = False,
        stdout: IO[str] | None = None,
        stderr: IO[str] | None = None,
    ) -> None:
        self.json_mode = json_mode
        self.color = color
        self.utc = utc
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr

    def use_color(self) -> bool:
        if self.color == "always":
            return True
        if self.color == "never":
            return False
        if os.environ.get("NO_COLOR") is not None:
            return False
        return self.stdout.isatty()

    def style(self, text: str, *names: str) -> str:
        if not self.use_color() or not names:
            return text
        prefix = "".join(_COLORS[name] for name in names if name in _COLORS)
        return f"{prefix}{text}{_COLORS['reset']}"

    def line(self, text: str = "") -> None:
        print(text, file=self.stdout)

    def write(self, text: str) -> None:
        """Write text verbatim, without adding a trailing newline."""
        print(text, end="", file=self.stdout)

    def heading(self, text: str) -> None:
        self.line(self.style(text, "bold"))

    def diagnostic(self, text: str) -> None:
        print(text, file=self.stderr)

    def warning(self, text: str) -> None:
        print(self.style(f"Warning: {text}", "yellow"), file=self.stderr)

    def emit_json(self, payload: dict[str, Any]) -> None:
        self.line(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
        materialized = [[str(cell) for cell in row] for row in rows]
        widths = [len(header) for header in headers]
        for row in materialized:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], len(cell))
        header_line = "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
        self.line(self.style(header_line.rstrip(), "bold"))
        for row in materialized:
            self.line("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())

    def key_values(self, pairs: Sequence[tuple[str, str]]) -> None:
        if not pairs:
            return
        width = max(len(key) for key, _ in pairs)
        for key, value in pairs:
            label = self.style(key.ljust(width), "bold", "cyan")
            self.line(f"{label}  {value}".rstrip())

    def job_datetime(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return format_datetime(value, utc=self.utc)


def job_to_dict(job: ScheduledJob) -> dict[str, Any]:
    schedule: dict[str, Any] = {
        "kind": job.schedule.kind.value,
        "expression": job.schedule.expression,
    }
    if job.schedule.expressions:
        schedule["expressions"] = list(job.schedule.expressions)

    data: dict[str, Any] = {
        "name": job.name,
        "backend": job.backend.value,
        "scope": job.scope.value,
        "managed": job.managed,
        "enabled": job.enabled,
        "schedule": schedule,
        "command": {
            "argv": list(job.command.argv),
            "shell": job.command.shell,
            "raw": job.command.raw,
        },
        "source": {
            "detail": job.source.detail,
            "path": job.source.path,
            "line": job.source.line,
        },
        "next_run": isoformat(job.next_run) if job.next_run else None,
        "last_run": isoformat(job.last_run) if job.last_run else None,
        "last_result": job.last_result,
        "warnings": list(job.warnings),
    }
    if job.systemd is not None:
        data["systemd"] = {
            "timer_unit": job.systemd.timer_unit,
            "service_unit": job.systemd.service_unit,
            "timer_path": job.systemd.timer_path,
            "service_path": job.systemd.service_path,
            "on_calendar": list(job.systemd.on_calendar),
            "persistent": job.systemd.persistent,
            "jitter": job.systemd.jitter,
            "accuracy": job.systemd.accuracy,
            "working_directory": job.systemd.working_directory,
            "environment": [list(item) for item in job.systemd.environment],
            "active_state": job.systemd.active_state,
            "sub_state": job.systemd.sub_state,
            "unit_file_state": job.systemd.unit_file_state,
            "result": job.systemd.result,
        }
    if job.cron is not None:
        data["cron"] = {
            "expression": job.cron.expression,
            "shell": job.cron.shell,
            "mailto": job.cron.mailto,
            "line": job.cron.line,
            "user": job.cron.user,
            "environment": [list(item) for item in job.cron.environment],
        }
    return data


def jobs_document(jobs: Sequence[ScheduledJob], warnings: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "jobs": [job_to_dict(job) for job in jobs],
        "warnings": list(warnings),
    }
