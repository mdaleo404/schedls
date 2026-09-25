"""Backend interface, capability model and mutation plans."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..models import JobSpec, ScheduledJob, Scope


@dataclass(frozen=True)
class Capabilities:
    discovery: bool = False
    create: bool = False
    update: bool = False
    remove: bool = False
    enable: bool = False
    disable: bool = False
    logs: bool = False
    next_run: bool = False
    validation: bool = False


@dataclass(frozen=True)
class FileChange:
    path: str
    content: str | None
    mode: int = 0o644
    expected_uid: int | None = None


@dataclass(frozen=True)
class CommandPlan:
    argv: Sequence[str]
    description: str
    env_policy: str = "minimal"
    input_text: str | None = None


@dataclass
class Plan:
    backend: str
    action: str
    summary: list[tuple[str, str]] = field(default_factory=list)
    files: list[FileChange] = field(default_factory=list)
    commands: list[CommandPlan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def display_files(self) -> list[str]:
        return [change.path for change in self.files]


@dataclass
class MutationResult:
    changed: bool
    messages: list[str] = field(default_factory=list)
    files_written: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    dry_run: bool = False


class SchedulerBackend:
    """Base class for backends. Unsupported operations must not be faked."""

    name: str = "unknown"
    capabilities = Capabilities()

    def available(self) -> bool:
        raise NotImplementedError

    def discover(self, scopes: Sequence[Scope]) -> list[ScheduledJob]:
        raise NotImplementedError

    def find(self, name: str) -> ScheduledJob | None:
        for job in self.discover([Scope.USER, Scope.SYSTEM]):
            if job.name == name:
                return job
        return None

    def plan_create(self, spec: JobSpec) -> Plan:
        raise NotImplementedError

    def plan_update(self, current: ScheduledJob, spec: JobSpec) -> Plan:
        raise NotImplementedError

    def plan_remove(self, job: ScheduledJob) -> Plan:
        raise NotImplementedError

    def plan_set_enabled(self, job: ScheduledJob, enabled: bool) -> Plan:
        raise NotImplementedError

    def logs(self, job: ScheduledJob, *, lines: int, since: str | None) -> str:
        raise NotImplementedError

    def apply(self, plan: Plan) -> MutationResult:
        raise NotImplementedError
