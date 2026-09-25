"""cron backend: lossless crontab parsing, discovery and managed blocks."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..errors import (
    ConflictError,
    InvalidScheduleError,
    OperationalError,
    SafetyRefusalError,
)
from ..models import (
    Backend,
    Command,
    CronDetails,
    JobSource,
    JobSpec,
    Schedule,
    ScheduledJob,
    ScheduleKind,
    Scope,
)
from ..renderers import cron as renderer
from ..runner import CommandRunner
from ..security import validate_name
from .base import (
    Capabilities,
    CommandPlan,
    MutationResult,
    Plan,
    SchedulerBackend,
)

_MARKER_RE = re.compile(r"^#\s*schedls:(?P<kind>begin|end)\s+(?P<meta>.*)$")
_META_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")
_NICKNAME_RE = re.compile(r"^@[A-Za-z]+")


@dataclass
class CronCapabilities:
    supports_validation: bool = False
    supports_user_selection: bool = False
    implementation: str | None = None


@dataclass
class CrontabEntry:
    index: int
    raw: str
    kind: str
    name: str | None = None
    expression: str | None = None
    command: str | None = None
    marker_name: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class ManagedBlock:
    name: str
    begin_index: int
    end_index: int
    job_lines: list[CrontabEntry]
    extra: dict[str, str] = field(default_factory=dict)


class CrontabDocument:
    """An exact, line-preserving view of a crontab."""

    def __init__(self, text: str) -> None:
        self.had_trailing_newline = text.endswith("\n") if text else False
        body = text[:-1] if self.had_trailing_newline else text
        self.lines: list[str] = body.split("\n") if body != "" else []
        self.entries: list[CrontabEntry] = []
        self.warnings: list[str] = []
        self._blocks: dict[str, ManagedBlock] = {}
        self._parse()

    @classmethod
    def parse(cls, text: str) -> CrontabDocument:
        return cls(text)

    @property
    def text(self) -> str:
        return self.render()

    def render(self) -> str:
        body = "\n".join(self.lines)
        if self.had_trailing_newline and body != "":
            return body + "\n"
        return body

    # -- parsing --------------------------------------------------------------

    def _parse(self) -> None:
        open_begin: CrontabEntry | None = None
        open_lines: list[CrontabEntry] = []
        for number, raw in enumerate(self.lines, start=1):
            entry = self._classify(number, raw)
            self.entries.append(entry)
            if entry.kind == "managed_begin":
                if open_begin is not None:
                    self.warnings.append(
                        f"line {number}: nested schedls block; previous block on line {open_begin.index} is malformed"
                    )
                open_begin = entry
                open_lines = []
            elif entry.kind == "managed_end":
                if open_begin is None:
                    self.warnings.append(f"line {number}: schedls:end without a matching begin")
                    continue
                if entry.marker_name != open_begin.marker_name:
                    self.warnings.append(
                        f"line {number}: schedls:end name={entry.marker_name!r} does not match "
                        f"begin name={open_begin.marker_name!r}"
                    )
                    open_begin = None
                    open_lines = []
                    continue
                name = open_begin.marker_name
                if name is None:
                    self.warnings.append(f"line {open_begin.index}: schedls:begin is missing a name")
                    open_begin = None
                    open_lines = []
                    continue
                self._blocks[name] = ManagedBlock(
                    name=name,
                    begin_index=open_begin.index,
                    end_index=entry.index,
                    job_lines=list(open_lines),
                    extra=open_begin.extra,
                )
                open_begin = None
                open_lines = []
            elif open_begin is not None:
                if entry.kind in {"job", "comment", "env", "blank"}:
                    open_lines.append(entry)
        if open_begin is not None:
            self.warnings.append(
                f"line {open_begin.index}: schedls:begin name={open_begin.marker_name!r} has no matching end"
            )

    def _classify(self, number: int, raw: str) -> CrontabEntry:
        stripped = raw.strip()
        if not stripped:
            return CrontabEntry(number, raw, "blank")
        if stripped.startswith("#"):
            match = _MARKER_RE.match(stripped)
            if match:
                extra = {item.group("key"): item.group("value") for item in _META_RE.finditer(match.group("meta"))}
                name = extra.get("name")
                kind = "managed_begin" if match.group("kind") == "begin" else "managed_end"
                return CrontabEntry(number, raw, kind, marker_name=name, extra=extra)
            return CrontabEntry(number, raw, "comment")
        if _ENV_RE.match(stripped):
            return CrontabEntry(number, raw, "env")
        expression, command = _split_job(stripped)
        if expression is None:
            return CrontabEntry(number, raw, "comment")
        return CrontabEntry(number, raw, "job", expression=expression, command=command)

    # -- queries --------------------------------------------------------------

    def managed_blocks(self) -> dict[str, ManagedBlock]:
        return dict(self._blocks)

    def find_block(self, name: str) -> ManagedBlock | None:
        return self._blocks.get(name)

    def has_malformed_markers(self) -> bool:
        return bool(self.warnings)

    # -- transformations ------------------------------------------------------

    def with_block(self, name: str, lines: list[str]) -> str:
        if self.has_malformed_markers():
            raise SafetyRefusalError("existing crontab contains malformed schedls markers; refusing to edit.")
        if name in self._blocks:
            raise ConflictError(
                f"schedule {name!r} already exists in the crontab.",
                hint="Use 'schedls edit' or 'schedls rm' instead.",
            )
        block = renderer.render_block(name, lines).splitlines()
        lines_out = list(self.lines)
        if lines_out and lines_out[-1].strip() != "":
            lines_out.append("")
        lines_out.extend(block)
        return "\n".join(lines_out) + "\n"

    def without_block(self, name: str) -> str:
        if self.has_malformed_markers():
            raise SafetyRefusalError("existing crontab contains malformed schedls markers; refusing to edit.")
        block = self._blocks.get(name)
        if block is None:
            raise SafetyRefusalError(f"no schedls-managed cron block named {name!r}.")
        keep: list[str] = []
        for entry in self.entries:
            if block.begin_index <= entry.index <= block.end_index:
                continue
            keep.append(entry.raw)
        while keep and keep[-1].strip() == "":
            keep.pop()
        if not keep:
            return ""
        return "\n".join(keep) + "\n"


def _split_job(stripped: str) -> tuple[str | None, str | None]:
    nickname = _NICKNAME_RE.match(stripped)
    if nickname:
        rest = stripped[nickname.end() :].strip()
        if not rest:
            return None, None
        return nickname.group(0), rest
    parts = stripped.split(None, 5)
    if len(parts) < 6:
        return None, None
    return " ".join(parts[:5]), parts[5]


class CronBackend(SchedulerBackend):
    name = "cron"
    capabilities = Capabilities(
        discovery=True,
        create=True,
        remove=True,
        next_run=False,
        validation=True,
    )

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner
        self._capabilities: CronCapabilities | None = None

    def available(self) -> bool:
        return self.runner.has("crontab")

    def capabilities_info(self) -> CronCapabilities:
        if self._capabilities is not None:
            return self._capabilities
        info = CronCapabilities()
        if self.available():
            completed = self.runner.run(["crontab", "-V"], env_policy="identity", check=False)
            output = (completed.stdout or completed.stderr).strip()
            if completed.returncode != 0 or not output:
                completed = self.runner.run(["crontab", "--version"], env_policy="identity", check=False)
                output = (completed.stdout or completed.stderr).strip()
            if output:
                info.implementation = output.splitlines()[0].strip()
                lowered = output.lower()
                if "cronie" in lowered:
                    info.implementation = "Cronie"
                elif "vixie" in lowered:
                    info.implementation = "Vixie cron"
            info.supports_validation = self._probe_validation()
            info.supports_user_selection = self._probe_user_selection()
        self._capabilities = info
        return info

    def _probe_validation(self) -> bool:
        completed = self.runner.run(
            ["crontab", "-T", "/dev/null"],
            env_policy="identity",
            check=False,
        )
        return completed.returncode == 0

    def _probe_user_selection(self) -> bool:
        if os.geteuid() != 0:
            return False
        completed = self.runner.run(["crontab", "-u", "root", "-l"], env_policy="identity", check=False)
        return completed.returncode == 0 or "no crontab" in (completed.stderr + completed.stdout).lower()

    # -- read -----------------------------------------------------------------

    def read(self) -> CrontabDocument:
        completed = self.runner.run(["crontab", "-l"], env_policy="identity", check=False)
        if completed.returncode != 0:
            if "no crontab" in (completed.stderr + completed.stdout).lower():
                return CrontabDocument("")
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise OperationalError("could not read the current user's crontab", hint=detail or None)
        return CrontabDocument(completed.stdout)

    def discover(self, scopes: Sequence[Scope]) -> list[ScheduledJob]:
        if Scope.USER not in scopes or not self.available():
            return []
        document = self.read()
        jobs: list[ScheduledJob] = []
        managed_indices: set[int] = set()
        for name, block in document.managed_blocks().items():
            managed_indices.update(range(block.begin_index, block.end_index + 1))
            job_line = next((line for line in block.job_lines if line.kind == "job"), None)
            job = ScheduledJob(
                name=name,
                backend=Backend.CRON,
                scope=Scope.USER,
                managed=True,
                enabled=None,
                schedule=Schedule(ScheduleKind.CRON, (job_line.expression or "") if job_line else ""),
                command=Command(argv=(), raw=job_line.command if job_line else ""),
                source=JobSource("current user's crontab", line=block.begin_index),
                next_run=None,
                last_run=None,
                cron=CronDetails(
                    expression=job_line.expression if job_line else None,
                    line=block.begin_index,
                ),
            )
            jobs.append(job)
        for entry in document.entries:
            if entry.kind != "job" or entry.index in managed_indices:
                continue
            jobs.append(
                ScheduledJob(
                    name=f"cron-{entry.index}",
                    backend=Backend.CRON,
                    scope=Scope.USER,
                    managed=False,
                    enabled=None,
                    schedule=Schedule(ScheduleKind.CRON, entry.expression or ""),
                    command=Command(argv=(), raw=entry.command or ""),
                    source=JobSource("current user's crontab", line=entry.index),
                    cron=CronDetails(
                        expression=entry.expression,
                        raw_line=entry.raw,
                        line=entry.index,
                    ),
                )
            )
        return jobs

    # -- planning -------------------------------------------------------------

    def plan_create(self, spec: JobSpec) -> Plan:
        validate_name(spec.name)
        if spec.scope is not Scope.USER:
            raise SafetyRefusalError("system cron management is not supported.")
        if not self.available():
            raise OperationalError("crontab is not available")
        expression = renderer.validate_expression(spec.cron_expression or "")
        line = renderer.render_line(expression, spec.command)
        document = self.read()
        new_text = document.with_block(spec.name, [line])
        plan = Plan(backend=self.name, action="create")
        plan.summary = [
            ("Backend", "cron (current user)"),
            ("Schedule", expression),
            ("Command", spec.command.display()),
            ("Execute via", "/bin/sh"),
        ]
        plan.files = []
        plan.commands = self._install_commands(new_text)
        plan.warnings.append("Cron executes command text through /bin/sh (or the crontab's configured SHELL).")
        plan.payload = {
            "action": "create",
            "name": spec.name,
            "old_text": document.text,
            "new_text": new_text,
        }
        return plan

    def plan_remove(self, job: ScheduledJob) -> Plan:
        if not job.managed:
            raise SafetyRefusalError(
                f"{job.name} was not created by schedls.",
                hint="schedls will not modify unmanaged schedules by default.",
            )
        document = self.read()
        new_text = document.without_block(job.name)
        plan = Plan(backend=self.name, action="remove")
        plan.summary = [
            ("Backend", "cron (current user)"),
            ("Will remove", f"schedls block name={job.name}"),
        ]
        plan.commands = self._install_commands(new_text)
        plan.payload = {
            "action": "remove",
            "name": job.name,
            "old_text": document.text,
            "new_text": new_text,
        }
        return plan

    def _install_commands(self, new_text: str) -> list[CommandPlan]:
        return [CommandPlan(("crontab", "-"), "install crontab", "identity", input_text=new_text)]

    # -- applying -------------------------------------------------------------

    def apply(self, plan: Plan) -> MutationResult:
        old_text = plan.payload.get("old_text", "")
        new_text = plan.payload.get("new_text", "")
        current_document = self.read()
        if current_document.text != old_text:
            raise SafetyRefusalError("the crontab changed after the plan was prepared; refusing to apply.")
        self._validate(new_text)
        try:
            for command in plan.commands:
                self.runner.run(
                    list(command.argv),
                    env_policy=command.env_policy,
                    input_text=command.input_text,
                )
        except Exception:
            self._restore(old_text)
            raise
        self._verify_installed(plan)
        return MutationResult(
            changed=True,
            messages=["Crontab updated."],
        )

    def _validate(self, text: str) -> None:
        if not self.capabilities_info().supports_validation:
            return
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".cron", delete=False) as handle:
            handle.write(text)
            name = handle.name
        try:
            completed = self.runner.run(["crontab", "-T", name], env_policy="identity", check=False)
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise InvalidScheduleError("generated crontab failed syntax validation", hint=detail or None)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(name)

    def _verify_installed(self, plan: Plan) -> None:
        document = self.read()
        name = plan.payload.get("name")
        if plan.payload.get("action") == "create" and name and name not in document.managed_blocks():
            raise OperationalError("the crontab was installed but the schedls block could not be verified.")

    def _restore(self, text: str) -> None:
        with contextlib.suppress(Exception):
            self.runner.run(["crontab", "-"], env_policy="identity", check=False, input_text=text)
