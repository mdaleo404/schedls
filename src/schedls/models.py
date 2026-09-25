"""Common data model shared by every backend.

The common model describes shared concepts but never erases backend-specific
facts: those live in typed nested structures attached to each job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Backend(StrEnum):
    SYSTEMD = "systemd"
    CRON = "cron"


class Scope(StrEnum):
    USER = "user"
    SYSTEM = "system"


class ScheduleKind(StrEnum):
    CALENDAR = "calendar"
    CRON = "cron"


@dataclass(frozen=True)
class Schedule:
    """A schedule expressed in a backend's native syntax."""

    kind: ScheduleKind
    expression: str
    expressions: tuple[str, ...] = ()

    def all_expressions(self) -> tuple[str, ...]:
        if self.expressions:
            return self.expressions
        return (self.expression,)


@dataclass(frozen=True)
class Command:
    """An execution request.

    ``argv`` is always the authoritative form.  ``shell`` requests that the
    command be executed through ``/bin/sh -c`` using ``raw``.
    """

    argv: tuple[str, ...] = ()
    shell: bool = False
    raw: str | None = None

    def display(self) -> str:
        if self.shell and self.raw is not None:
            return self.raw
        if not self.argv and self.raw:
            return self.raw
        return " ".join(self.argv)

    def is_empty(self) -> bool:
        return not self.argv and not self.raw


@dataclass(frozen=True)
class JobSource:
    """Where a discovered job came from."""

    detail: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class SystemdDetails:
    timer_unit: str | None = None
    service_unit: str | None = None
    timer_path: str | None = None
    service_path: str | None = None
    on_calendar: tuple[str, ...] = ()
    persistent: bool = False
    jitter: str | None = None
    accuracy: str | None = None
    working_directory: str | None = None
    environment: tuple[tuple[str, str], ...] = ()
    active_state: str | None = None
    sub_state: str | None = None
    unit_file_state: str | None = None
    result: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class CronDetails:
    expression: str | None = None
    shell: str | None = None
    mailto: str | None = None
    raw_line: str | None = None
    line: int | None = None
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    backend: Backend
    scope: Scope
    managed: bool
    enabled: bool | None
    schedule: Schedule
    command: Command
    source: JobSource
    next_run: datetime | None = None
    last_run: datetime | None = None
    last_result: str | None = None
    warnings: tuple[str, ...] = ()
    systemd: SystemdDetails | None = None
    cron: CronDetails | None = None

    def schedule_text(self) -> str:
        from .describe import describe_schedule

        return describe_schedule(self.schedule)


@dataclass(frozen=True)
class JobSpec:
    """A request to create or update a scheduled job."""

    name: str
    backend: Backend
    scope: Scope
    command: Command
    calendar: tuple[str, ...] = ()
    cron_expression: str | None = None
    persistent: bool = False
    jitter: str | None = None
    accuracy: str | None = None
    working_directory: str | None = None
    environment: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def schedule(self) -> Schedule:
        if self.backend is Backend.SYSTEMD:
            expr = self.calendar[0] if self.calendar else ""
            return Schedule(ScheduleKind.CALENDAR, expr, tuple(self.calendar))
        expr = self.cron_expression or ""
        return Schedule(ScheduleKind.CRON, expr)
