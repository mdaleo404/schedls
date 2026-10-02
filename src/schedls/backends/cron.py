"""cron backend: lossless crontab parsing, discovery and managed blocks."""

from __future__ import annotations

import contextlib
import os
import pwd
import re
import stat
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
from ..security import (
    atomic_write_text,
    check_trusted_directory,
    remove_file,
    validate_name,
)
from .base import (
    Capabilities,
    CommandPlan,
    FileChange,
    MutationResult,
    Plan,
    SchedulerBackend,
)

_MARKER_RE = re.compile(r"^#\s*schedls:(?P<kind>begin|end)\s+(?P<meta>.*)$")
_META_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")
_NICKNAME_RE = re.compile(r"^@[A-Za-z]+")

_MANAGED_PREFIX = "schedls-"
_CRON_D_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True)
class CronPaths:
    """Filesystem locations of system cron sources (injectable for tests)."""

    crontab: str = "/etc/crontab"
    cron_d: str = "/etc/cron.d"
    hourly: str = "/etc/cron.hourly"
    daily: str = "/etc/cron.daily"
    weekly: str = "/etc/cron.weekly"
    monthly: str = "/etc/cron.monthly"
    spool_crontabs: str = "/var/spool/cron/crontabs"
    spool: str = "/var/spool/cron"

    def periodic_dirs(self) -> dict[str, str]:
        return {
            "@hourly": self.hourly,
            "@daily": self.daily,
            "@weekly": self.weekly,
            "@monthly": self.monthly,
        }


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
    user: str | None = None
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
    """An exact, line-preserving view of a crontab.

    ``system=True`` parses system crontabs (``/etc/crontab``,
    ``/etc/cron.d/*``), whose entries carry a user field between the schedule
    and the command.
    """

    def __init__(self, text: str, *, system: bool = False) -> None:
        self.had_trailing_newline = text.endswith("\n") if text else False
        body = text[:-1] if self.had_trailing_newline else text
        self.lines: list[str] = body.split("\n") if body != "" else []
        self.system = system
        self.entries: list[CrontabEntry] = []
        self.warnings: list[str] = []
        self._blocks: dict[str, ManagedBlock] = {}
        self._parse()

    @classmethod
    def parse(cls, text: str, *, system: bool = False) -> CrontabDocument:
        return cls(text, system=system)

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
        expression, command, user = _split_job(stripped, system=self.system)
        if expression is None:
            return CrontabEntry(number, raw, "comment")
        return CrontabEntry(number, raw, "job", expression=expression, command=command, user=user)

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


def _split_job(stripped: str, *, system: bool = False) -> tuple[str | None, str | None, str | None]:
    nickname = _NICKNAME_RE.match(stripped)
    if nickname:
        rest = stripped[nickname.end() :].strip()
        if not rest:
            return None, None, None
        if system:
            parts = rest.split(None, 1)
            if len(parts) < 2:
                return None, None, None
            return nickname.group(0), parts[1], parts[0]
        return nickname.group(0), rest, None
    maxsplit = 6 if system else 5
    parts = stripped.split(None, maxsplit)
    needed = maxsplit + 1
    if len(parts) < needed:
        return None, None, None
    if system:
        return " ".join(parts[:5]), parts[6], parts[5]
    return " ".join(parts[:5]), parts[5], None


class CronBackend(SchedulerBackend):
    name = "cron"
    capabilities = Capabilities(
        discovery=True,
        create=True,
        remove=True,
        next_run=False,
        validation=True,
    )

    def __init__(self, runner: CommandRunner, paths: CronPaths | None = None) -> None:
        self.runner = runner
        self.paths = paths or CronPaths()
        self._capabilities: CronCapabilities | None = None

    def crontab_available(self) -> bool:
        return self.runner.has("crontab")

    def file_sources_available(self) -> bool:
        candidates = (
            self.paths.crontab,
            self.paths.cron_d,
            *self.paths.periodic_dirs().values(),
            self.paths.spool_crontabs,
            self.paths.spool,
        )
        return any(os.path.exists(path) for path in candidates)

    def available(self) -> bool:
        return self.crontab_available() or self.file_sources_available()

    def capabilities_info(self) -> CronCapabilities:
        if self._capabilities is not None:
            return self._capabilities
        info = CronCapabilities()
        if self.crontab_available():
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

    # -- discovery ------------------------------------------------------------

    def discover(self, scopes: Sequence[Scope]) -> list[ScheduledJob]:
        jobs: list[ScheduledJob] = []
        if Scope.USER in scopes:
            jobs.extend(self._discover_user_crontab())
            jobs.extend(self._discover_spool_crontabs())
        if Scope.SYSTEM in scopes:
            jobs.extend(self._discover_system_crontabs())
            jobs.extend(self._discover_periodic_directories())
        return jobs

    def _discover_user_crontab(self) -> list[ScheduledJob]:
        if not self.crontab_available():
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

    def _discover_system_crontabs(self) -> list[ScheduledJob]:
        jobs: list[ScheduledJob] = []
        jobs.extend(self._discover_system_file(self.paths.crontab, managed_drop_in=False))
        for name in self._list_dir(self.paths.cron_d):
            if not _CRON_D_NAME_RE.match(name):
                continue
            path = os.path.join(self.paths.cron_d, name)
            jobs.extend(self._discover_system_file(path, managed_drop_in=True))
        return jobs

    def _discover_system_file(self, path: str, *, managed_drop_in: bool) -> list[ScheduledJob]:
        text = _read_text(path)
        if text is None:
            return []
        document = CrontabDocument(text, system=True)
        base = os.path.basename(path)
        blocks = document.managed_blocks()
        managed_name: str | None = None
        warnings: list[str] = []
        if managed_drop_in and base.startswith(_MANAGED_PREFIX):
            expected = base[len(_MANAGED_PREFIX) :]
            if expected in blocks and not document.warnings:
                managed_name = expected
            else:
                warnings.append(
                    f"{path} looks like a schedls drop-in but its managed block is missing or "
                    "malformed; treating its jobs as unmanaged."
                )
        elif blocks:
            warnings.append(
                f"{path} contains schedls block markers but is not a schedls-owned drop-in; "
                "treating its jobs as unmanaged."
            )

        jobs: list[ScheduledJob] = []
        managed_range: set[int] = set()
        if managed_name is not None:
            block = blocks[managed_name]
            managed_range = set(range(block.begin_index, block.end_index + 1))
            job_line = next((line for line in block.job_lines if line.kind == "job"), None)
            jobs.append(
                ScheduledJob(
                    name=managed_name,
                    backend=Backend.CRON,
                    scope=Scope.SYSTEM,
                    managed=True,
                    enabled=None,
                    schedule=Schedule(ScheduleKind.CRON, (job_line.expression or "") if job_line else ""),
                    command=Command(argv=(), raw=job_line.command if job_line else ""),
                    source=JobSource("system cron drop-in", path=path, line=block.begin_index),
                    cron=CronDetails(
                        expression=job_line.expression if job_line else None,
                        raw_line=job_line.raw if job_line else None,
                        line=block.begin_index,
                        user=job_line.user if job_line else None,
                    ),
                )
            )
        for entry in document.entries:
            if entry.kind != "job" or entry.index in managed_range:
                continue
            jobs.append(
                ScheduledJob(
                    name=f"{base}:{entry.index}",
                    backend=Backend.CRON,
                    scope=Scope.SYSTEM,
                    managed=False,
                    enabled=None,
                    schedule=Schedule(ScheduleKind.CRON, entry.expression or ""),
                    command=Command(argv=(), raw=entry.command or ""),
                    source=JobSource("system cron", path=path, line=entry.index),
                    cron=CronDetails(
                        expression=entry.expression,
                        raw_line=entry.raw,
                        line=entry.index,
                        user=entry.user,
                    ),
                    warnings=tuple(warnings),
                )
            )
        return jobs

    def _discover_periodic_directories(self) -> list[ScheduledJob]:
        jobs: list[ScheduledJob] = []
        for expression, directory in self.paths.periodic_dirs().items():
            for name in self._list_dir(directory):
                if not _CRON_D_NAME_RE.match(name):
                    continue
                path = os.path.join(directory, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o111:
                    continue
                jobs.append(
                    ScheduledJob(
                        name=name,
                        backend=Backend.CRON,
                        scope=Scope.SYSTEM,
                        managed=False,
                        enabled=None,
                        schedule=Schedule(ScheduleKind.CRON, expression),
                        command=Command(argv=(), raw=path),
                        source=JobSource(f"{os.path.basename(directory)} (run-parts)", path=path, line=None),
                        cron=CronDetails(expression=expression, user="root"),
                    )
                )
        return jobs

    def _discover_spool_crontabs(self) -> list[ScheduledJob]:
        if os.geteuid() != 0:
            return []
        directory = self._spool_directory()
        if directory is None:
            return []
        current_user = self._current_user()
        jobs: list[ScheduledJob] = []
        for name in self._list_dir(directory):
            if name.startswith(".") or name == current_user:
                continue
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            text = _read_text(path)
            if text is None:
                continue
            document = CrontabDocument(text)
            for entry in document.entries:
                if entry.kind != "job":
                    continue
                jobs.append(
                    ScheduledJob(
                        name=f"{name}:{entry.index}",
                        backend=Backend.CRON,
                        scope=Scope.USER,
                        managed=False,
                        enabled=None,
                        schedule=Schedule(ScheduleKind.CRON, entry.expression or ""),
                        command=Command(argv=(), raw=entry.command or ""),
                        source=JobSource(f"user crontab ({name})", path=path, line=entry.index),
                        cron=CronDetails(
                            expression=entry.expression,
                            raw_line=entry.raw,
                            line=entry.index,
                            user=name,
                        ),
                    )
                )
        return jobs

    def _spool_directory(self) -> str | None:
        if os.path.isdir(self.paths.spool_crontabs):
            return self.paths.spool_crontabs
        if os.path.isdir(self.paths.spool):
            return self.paths.spool
        return None

    @staticmethod
    def _current_user() -> str:
        try:
            return pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            return ""

    @staticmethod
    def _list_dir(directory: str) -> list[str]:
        try:
            return sorted(os.listdir(directory))
        except OSError:
            return []

    # -- planning -------------------------------------------------------------

    def plan_create(self, spec: JobSpec) -> Plan:
        validate_name(spec.name)
        if spec.scope is Scope.SYSTEM:
            return self._plan_create_system(spec)
        if spec.scope is not Scope.USER:
            raise SafetyRefusalError("unsupported cron scope.")
        if not self.crontab_available():
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
            "mode": "crontab",
            "action": "create",
            "name": spec.name,
            "old_text": document.text,
            "new_text": new_text,
        }
        return plan

    def _plan_create_system(self, spec: JobSpec) -> Plan:
        if os.geteuid() != 0:
            raise SafetyRefusalError(
                "creating a system cron job requires appropriate privileges.",
                hint="Run the command under sudo yourself if that is your intention:\n  sudo schedls new ... --system",
            )
        renderer.validate_system_name(spec.name)
        directory = self.paths.cron_d
        if not os.path.isdir(directory):
            raise OperationalError(f"system cron directory does not exist: {directory}")
        check_trusted_directory(directory, expected_uid=0)
        expression = renderer.validate_expression(spec.cron_expression or "")
        run_as = renderer.validate_run_as(spec.run_as or "root")
        line = renderer.render_system_line(expression, spec.command, run_as)
        content = renderer.render_system_file(spec.name, [line])
        path = os.path.join(directory, f"{_MANAGED_PREFIX}{spec.name}")
        if os.path.lexists(path):
            raise ConflictError(
                f"schedule {spec.name!r} already exists ({path}).",
                hint="Use 'schedls rm' instead.",
            )
        plan = Plan(backend=self.name, action="create")
        plan.summary = [
            ("Backend", "cron (system)"),
            ("Schedule", expression),
            ("Run as", run_as),
            ("Command", spec.command.display()),
            ("File", path),
        ]
        plan.files = [FileChange(path=path, content=content, mode=0o644, expected_uid=0)]
        plan.warnings.append("Cron executes command text through /bin/sh (or the crontab's configured SHELL).")
        plan.payload = {
            "mode": "file",
            "action": "create",
            "name": spec.name,
            "path": path,
            "expected_uid": 0,
            "snapshots": {path: None},
        }
        return plan

    def plan_remove(self, job: ScheduledJob) -> Plan:
        if not job.managed:
            raise SafetyRefusalError(
                f"{job.name} was not created by schedls.",
                hint="schedls will not modify unmanaged schedules by default.",
            )
        if job.scope is Scope.SYSTEM:
            return self._plan_remove_system(job)
        document = self.read()
        new_text = document.without_block(job.name)
        plan = Plan(backend=self.name, action="remove")
        plan.summary = [
            ("Backend", "cron (current user)"),
            ("Will remove", f"schedls block name={job.name}"),
        ]
        plan.commands = self._install_commands(new_text)
        plan.payload = {
            "mode": "crontab",
            "action": "remove",
            "name": job.name,
            "old_text": document.text,
            "new_text": new_text,
        }
        return plan

    def _plan_remove_system(self, job: ScheduledJob) -> Plan:
        if os.geteuid() != 0:
            raise SafetyRefusalError(
                "removing a system cron job requires appropriate privileges.",
                hint="Run the command under sudo yourself if that is your intention:\n  sudo schedls rm ...",
            )
        path = job.source.path
        expected_path = os.path.join(self.paths.cron_d, f"{_MANAGED_PREFIX}{job.name}")
        if path != expected_path:
            raise SafetyRefusalError(f"refusing to remove unexpected system cron file: {path!r}")
        content = _read_text(path)
        if content is None:
            raise OperationalError(f"could not read {path}")
        document = CrontabDocument(content, system=True)
        if document.warnings or document.find_block(job.name) is None:
            raise SafetyRefusalError(f"{path} is no longer a valid schedls-managed drop-in; refusing to remove it.")
        plan = Plan(backend=self.name, action="remove")
        plan.summary = [
            ("Backend", "cron (system)"),
            ("Will remove", path),
        ]
        plan.files = [FileChange(path=path, content=None, expected_uid=0)]
        plan.payload = {
            "mode": "file",
            "action": "remove",
            "name": job.name,
            "path": path,
            "expected_uid": 0,
            "snapshots": {path: content},
        }
        return plan

    def _install_commands(self, new_text: str) -> list[CommandPlan]:
        return [CommandPlan(("crontab", "-"), "install crontab", "identity", input_text=new_text)]

    # -- applying -------------------------------------------------------------

    def apply(self, plan: Plan) -> MutationResult:
        if plan.payload.get("mode") == "file":
            return self._apply_file(plan)
        return self._apply_crontab(plan)

    def _apply_crontab(self, plan: Plan) -> MutationResult:
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

    def _apply_file(self, plan: Plan) -> MutationResult:
        snapshots: dict[str, str | None] = plan.payload.get("snapshots", {})
        expected_uid = plan.payload.get("expected_uid", 0)
        written: list[str] = []
        try:
            for change in plan.files:
                if _read_text(change.path) != snapshots.get(change.path):
                    raise SafetyRefusalError(
                        f"{change.path} changed on disk after the plan was prepared; refusing to apply."
                    )
                if change.content is None:
                    if remove_file(change.path, expected_uid=expected_uid):
                        written.append(change.path)
                else:
                    atomic_write_text(
                        change.path,
                        change.content,
                        mode=change.mode,
                        expected_uid=expected_uid,
                    )
                    written.append(change.path)
        except BaseException as exc:
            self._rollback_file(written, snapshots, expected_uid)
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            if isinstance(exc, OperationalError | SafetyRefusalError):
                raise
            raise OperationalError(str(exc)) from exc
        self._verify_installed(plan)
        action = plan.payload.get("action")
        message = "System cron drop-in installed." if action == "create" else "System cron drop-in removed."
        return MutationResult(changed=bool(written), messages=[message])

    def _rollback_file(
        self,
        written: list[str],
        snapshots: dict[str, str | None],
        expected_uid: int | None,
    ) -> None:
        for path in reversed(written):
            previous = snapshots.get(path)
            with contextlib.suppress(Exception):
                if previous is None:
                    remove_file(path, expected_uid=expected_uid)
                else:
                    atomic_write_text(path, previous, mode=0o644, expected_uid=expected_uid)

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
        name = plan.payload.get("name")
        if plan.payload.get("mode") == "file":
            if plan.payload.get("action") != "create" or not name:
                return
            document = CrontabDocument(_read_text(plan.payload.get("path", "")) or "", system=True)
            if name not in document.managed_blocks():
                raise OperationalError("the drop-in was written but the schedls block could not be verified.")
            return
        document = self.read()
        if plan.payload.get("action") == "create" and name and name not in document.managed_blocks():
            raise OperationalError("the crontab was installed but the schedls block could not be verified.")

    def _restore(self, text: str) -> None:
        with contextlib.suppress(Exception):
            self.runner.run(["crontab", "-"], env_policy="identity", check=False, input_text=text)


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None
