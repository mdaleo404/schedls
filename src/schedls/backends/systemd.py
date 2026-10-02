"""systemd timer backend: discovery, rendering and transactional mutation."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime

from ..errors import (
    ConflictError,
    DependencyMissingError,
    InvalidScheduleError,
    OperationalError,
    SafetyRefusalError,
)
from ..models import (
    Backend,
    Command,
    JobSource,
    JobSpec,
    Schedule,
    ScheduledJob,
    ScheduleKind,
    Scope,
    SystemdDetails,
)
from ..renderers import systemd as renderer
from ..runner import CommandRunner
from ..security import (
    atomic_write_text,
    has_unsafe_control_characters,
    is_managed_unit,
    is_safe_unit_name,
    remove_file,
    unit_name,
    validate_absolute_path,
    validate_managed_unit_name,
    validate_name,
)
from ..timefmt import parse_systemd_timestamp
from ..unitfile import first, has_managed_marker, read_units, values
from .base import (
    Capabilities,
    CommandPlan,
    FileChange,
    MutationResult,
    Plan,
    SchedulerBackend,
)

_ENABLED_STATES = {"enabled", "enabled-runtime"}
_TIMER_SUFFIX = ".timer"
_SERVICE_SUFFIX = ".service"

_ITERATION_RE = re.compile(r"(?:Next elapse|Iteration #\d+):\s+(.+?)\s*$")
_UTC_LINE_RE = re.compile(r"\(in UTC\):\s+(.+?)\s*$")
_NORMALIZED_RE = re.compile(r"^\s*Normalized form:\s+(.+?)\s*$", re.MULTILINE)
_ORIGINAL_RE = re.compile(r"^\s*Original form:\s+(.+?)\s*$", re.MULTILINE)


class SystemdBackend(SchedulerBackend):
    name = "systemd"
    capabilities = Capabilities(
        discovery=True,
        create=True,
        update=True,
        remove=True,
        enable=True,
        disable=True,
        logs=True,
        next_run=True,
        validation=True,
    )

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner
        self._manager_ok: dict[Scope, bool] = {}
        self._user_lingering_checked = False
        self._user_lingering: bool | None = None

    # -- capability detection -------------------------------------------------

    def has_systemctl(self) -> bool:
        return self.runner.has("systemctl")

    def has_analyze(self) -> bool:
        return self.runner.has("systemd-analyze")

    def manager_ok(self, scope: Scope) -> bool:
        if scope in self._manager_ok:
            return self._manager_ok[scope]
        if not self.has_systemctl():
            self._manager_ok[scope] = False
            return False
        try:
            completed = self.runner.run(
                self._systemctl(scope, "is-system-running"),
                env_policy=self._env_policy(scope),
                check=False,
            )
        except OperationalError:
            self._manager_ok[scope] = False
            return False
        state = completed.stdout.strip()
        self._manager_ok[scope] = state not in {"", "offline", "unknown"}
        return self._manager_ok[scope]

    def available(self) -> bool:
        return self.has_systemctl() and (self.manager_ok(Scope.USER) or self.manager_ok(Scope.SYSTEM))

    def user_lingering(self) -> bool | None:
        if self._user_lingering_checked:
            return self._user_lingering
        self._user_lingering_checked = True
        if not self.runner.has("loginctl"):
            return None
        try:
            completed = self.runner.run(
                ["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
                env_policy="identity",
                check=False,
            )
        except OperationalError:
            return None
        if completed.returncode != 0:
            return None
        self._user_lingering = completed.stdout.strip().endswith("yes")
        return self._user_lingering

    # -- calendar validation --------------------------------------------------

    def validate_calendar(self, expression: str, *, iterations: int = 1) -> str:
        self._check_expression(expression)
        if not self.has_analyze():
            raise DependencyMissingError("systemd-analyze is not available; cannot validate calendar expressions.")
        completed = self.runner.run(
            [
                "systemd-analyze",
                "calendar",
                f"--iterations={max(iterations, 1)}",
                expression,
            ],
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise InvalidScheduleError(
                f"invalid calendar expression: {expression!r}",
                hint=detail or None,
            )
        match = _NORMALIZED_RE.search(completed.stdout)
        return match.group(1) if match else expression

    def calendar_occurrences(self, expression: str, *, iterations: int = 5) -> list[datetime]:
        self._check_expression(expression)
        completed = self.runner.run(
            [
                "systemd-analyze",
                "calendar",
                f"--iterations={max(iterations, 1)}",
                expression,
            ],
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise InvalidScheduleError(f"invalid calendar expression: {expression!r}", hint=detail or None)
        return _parse_occurrences(completed.stdout)

    # -- discovery ------------------------------------------------------------

    def discover(self, scopes: Sequence[Scope]) -> list[ScheduledJob]:
        jobs: list[ScheduledJob] = []
        for scope in scopes:
            if not self.manager_ok(scope):
                continue
            for unit in self._list_timer_units(scope):
                job = self._build_job(unit, scope)
                if job is not None:
                    jobs.append(job)
        return jobs

    def find(self, name: str) -> ScheduledJob | None:
        """Look up a timer directly instead of discovering every timer."""
        candidates = (f"schedls-{name}.timer", f"{name}.timer")
        for scope in (Scope.USER, Scope.SYSTEM):
            if not self.manager_ok(scope):
                continue
            for unit in candidates:
                if not is_safe_unit_name(unit):
                    continue
                job = self._build_job(unit, scope)
                if job is not None and job.name == name:
                    return job
        return None

    def _list_timer_units(self, scope: Scope) -> list[str]:
        units: list[str] = []
        seen: set[str] = set()
        for args in (
            ("list-unit-files", "--type=timer", "--no-legend", "--plain", "--all"),
            ("list-units", "--type=timer", "--all", "--no-legend", "--plain"),
        ):
            completed = self.runner.run(
                self._systemctl(scope, *args),
                env_policy=self._env_policy(scope),
                check=False,
            )
            if completed.returncode != 0:
                continue
            for line in completed.stdout.splitlines():
                parts = line.split()
                if not parts:
                    continue
                unit = parts[0].lstrip("●").strip()
                if unit.endswith(_TIMER_SUFFIX) and unit not in seen:
                    seen.add(unit)
                    units.append(unit)
        return units

    def _build_job(self, unit: str, scope: Scope) -> ScheduledJob | None:
        properties = self._show(unit, scope)
        if not properties:
            return None
        if properties.get("LoadState") == "not-found":
            return None
        fragment = properties.get("FragmentPath") or None
        timer_sections = read_units(fragment) if fragment else None
        on_calendar = tuple(values(timer_sections, "Timer", "OnCalendar"))
        if not on_calendar:
            on_calendar = _timers_calendar(properties.get("TimersCalendar", ""))

        service_unit = self._service_unit(unit, properties, timer_sections)
        if service_unit and not is_safe_unit_name(service_unit):
            service_unit = None
        service_fragment = None
        service_sections = None
        service_props: dict[str, str] = {}
        if service_unit:
            service_props = self._show(service_unit, scope)
            service_fragment = service_props.get("FragmentPath") or None
            if service_fragment:
                service_sections = read_units(service_fragment)

        prefix_managed = is_managed_unit(os.path.basename(unit))
        name = _job_name(unit, prefix_managed)
        managed = prefix_managed and fragment is not None and has_managed_marker(fragment, name=name)
        if prefix_managed and not managed:
            name = _job_name(unit, False)
        enabled = _enabled_from_state(properties.get("UnitFileState"))
        command = _command_from_service(service_sections)
        next_run = parse_systemd_timestamp(properties.get("NextElapseUSecRealtime", ""))
        last_run = parse_systemd_timestamp(properties.get("LastTriggerUSec", ""))
        last_result = service_props.get("Result") or properties.get("Result") or None

        details = SystemdDetails(
            timer_unit=unit,
            service_unit=service_unit,
            timer_path=fragment,
            service_path=service_fragment,
            on_calendar=on_calendar,
            persistent=_is_true(first(timer_sections, "Timer", "Persistent")),
            jitter=first(timer_sections, "Timer", "RandomizedDelaySec"),
            accuracy=first(timer_sections, "Timer", "AccuracySec"),
            working_directory=first(service_sections, "Service", "WorkingDirectory"),
            environment=_environment_from_service(service_sections),
            active_state=properties.get("ActiveState") or None,
            sub_state=properties.get("SubState") or None,
            unit_file_state=properties.get("UnitFileState") or None,
            result=last_result,
            description=properties.get("Description") or None,
        )

        warnings: list[str] = []
        if scope is Scope.USER and not managed and self.user_lingering() is False:
            warnings.append("user lingering is disabled; this timer may not run while logged out")

        return ScheduledJob(
            name=name,
            backend=Backend.SYSTEMD,
            scope=scope,
            managed=managed,
            enabled=enabled,
            schedule=Schedule(ScheduleKind.CALENDAR, on_calendar[0] if on_calendar else "", on_calendar),
            command=command,
            source=JobSource(
                detail=f"systemd {scope.value} timer",
                path=fragment,
                line=None,
            ),
            next_run=next_run,
            last_run=last_run,
            last_result=last_result,
            warnings=tuple(warnings),
            systemd=details,
        )

    def _service_unit(
        self, unit: str, properties: dict[str, str], timer_sections: dict[str, list[str]] | None
    ) -> str | None:
        declared = first(timer_sections, "Timer", "Unit")
        if declared:
            return declared
        triggers = properties.get("Triggers", "")
        for token in triggers.split():
            if token.endswith(_SERVICE_SUFFIX):
                return token
        return unit[: -len(_TIMER_SUFFIX)] + _SERVICE_SUFFIX

    def _show(self, unit: str, scope: Scope) -> dict[str, str]:
        properties = (
            "Id LoadState FragmentPath UnitFileState ActiveState SubState Description Result "
            "Triggers NextElapseUSecRealtime LastTriggerUSec Persistent "
            "RandomizedDelaySec AccuracySec TimersCalendar"
        )
        completed = self.runner.run(
            self._systemctl(scope, "show", unit, f"--property={properties.replace(' ', ',')}"),
            env_policy=self._env_policy(scope),
            check=False,
        )
        if completed.returncode != 0:
            return {}
        result: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                result[key] = value
        return result

    # -- planning -------------------------------------------------------------

    def plan_create(self, spec: JobSpec) -> Plan:
        validate_name(spec.name)
        self._require_scope(spec.scope)
        self._validate_spec(spec)
        service_unit = unit_name(spec.name, "service")
        timer_unit = unit_name(spec.name, "timer")
        directory = self._unit_dir(spec.scope)
        files = renderer.render_units(spec, service_unit=service_unit, timer_unit=timer_unit)
        changes: list[FileChange] = []
        for unit, content in files.items():
            path = os.path.join(directory, unit)
            if os.path.lexists(path):
                raise ConflictError(
                    f"schedule {spec.name!r} already exists ({path}).",
                    hint="Use 'schedls edit' or 'schedls rm' instead.",
                )
            changes.append(FileChange(path=path, content=content, mode=0o644))
        self._verify_units(files)

        plan = Plan(backend=self.name, action="create")
        plan.files = changes
        plan.summary = self._create_summary(spec, service_unit, timer_unit)
        plan.commands = [
            CommandPlan(
                self._systemctl(spec.scope, "daemon-reload"),
                "reload systemd manager",
                self._env_policy(spec.scope),
            ),
            CommandPlan(
                self._systemctl(spec.scope, "enable", "--now", timer_unit),
                "enable and start timer",
                self._env_policy(spec.scope),
            ),
        ]
        if spec.scope is Scope.USER and self.user_lingering() is False:
            plan.warnings.append(
                "user lingering is disabled; this timer may not run while you are logged out. "
                "schedls will not change lingering automatically."
            )
        plan.payload = {
            "scope": spec.scope,
            "timer_unit": timer_unit,
            "service_unit": service_unit,
            "timer_path": os.path.join(directory, timer_unit),
            "created": True,
            "snapshots": {},
        }
        return plan

    def plan_update(self, current: ScheduledJob, spec: JobSpec) -> Plan:
        if not current.managed:
            raise SafetyRefusalError(
                f"{current.name} was not created by schedls.",
                hint="schedls will not modify unmanaged schedules by default.",
            )
        if current.backend is not Backend.SYSTEMD or current.systemd is None:
            raise SafetyRefusalError(f"{current.name} is not a systemd timer.")
        self._require_scope(current.scope)
        self._validate_spec(spec)
        service_unit = unit_name(current.name, "service")
        timer_unit = unit_name(current.name, "timer")
        directory = self._unit_dir(current.scope)
        files = renderer.render_units(spec, service_unit=service_unit, timer_unit=timer_unit)
        changes: list[FileChange] = []
        snapshots: dict[str, str | None] = {}
        for unit, content in files.items():
            path = os.path.join(directory, unit)
            snapshots[path] = _read_text(path)
            changes.append(FileChange(path=path, content=content, mode=0o644))
        self._verify_units(files)
        plan = Plan(backend=self.name, action="update")
        plan.files = changes
        plan.summary = self._create_summary(spec, service_unit, timer_unit)
        plan.commands = [
            CommandPlan(
                self._systemctl(current.scope, "daemon-reload"),
                "reload systemd manager",
                self._env_policy(current.scope),
            ),
        ]
        plan.payload = {
            "scope": current.scope,
            "timer_unit": timer_unit,
            "service_unit": service_unit,
            "timer_path": os.path.join(directory, timer_unit),
            "created": False,
            "snapshots": snapshots,
        }
        return plan

    def plan_remove(self, job: ScheduledJob) -> Plan:
        if not job.managed:
            raise SafetyRefusalError(
                f"{job.name} was not created by schedls.",
                hint="schedls will not modify unmanaged schedules by default.",
            )
        if job.systemd is None:
            raise SafetyRefusalError(f"{job.name} is not a systemd timer.")
        self._require_scope(job.scope)
        directory = self._unit_dir(job.scope)
        timer_unit = unit_name(job.name, "timer")
        service_unit = unit_name(job.name, "service")
        paths = [
            os.path.join(directory, timer_unit),
            os.path.join(directory, service_unit),
        ]
        plan = Plan(backend=self.name, action="remove")
        plan.files = [FileChange(path=path, content=None) for path in paths]
        plan.summary = [("Backend", f"systemd {job.scope.value} timer")]
        if job.command.display():
            plan.summary.append(("Will NOT remove", job.command.display()))
        plan.commands = [
            CommandPlan(
                self._systemctl(job.scope, "disable", "--now", timer_unit),
                "disable and stop timer",
                self._env_policy(job.scope),
            ),
            CommandPlan(
                self._systemctl(job.scope, "daemon-reload"),
                "reload systemd manager",
                self._env_policy(job.scope),
            ),
        ]
        plan.payload = {
            "scope": job.scope,
            "timer_unit": timer_unit,
            "service_unit": service_unit,
            "created": False,
            "snapshots": {path: _read_text(path) for path in paths},
        }
        return plan

    def plan_set_enabled(self, job: ScheduledJob, enabled: bool) -> Plan:
        if not job.managed:
            raise SafetyRefusalError(
                f"{job.name} was not created by schedls.",
                hint="schedls will not modify unmanaged schedules by default.",
            )
        if job.systemd is None:
            raise SafetyRefusalError(f"{job.name} is not a systemd timer.")
        self._require_scope(job.scope)
        timer_unit = unit_name(job.name, "timer")
        if enabled:
            argv = self._systemctl(job.scope, "enable", "--now", timer_unit)
            description = "enable and start timer"
        else:
            argv = self._systemctl(job.scope, "disable", "--now", timer_unit)
            description = "disable and stop timer"
        plan = Plan(backend=self.name, action="enable" if enabled else "disable")
        plan.summary = [
            ("Backend", f"systemd {job.scope.value} timer"),
            ("Timer", timer_unit),
        ]
        plan.commands = [CommandPlan(argv, description, self._env_policy(job.scope))]
        plan.payload = {"scope": job.scope, "timer_unit": timer_unit, "created": False, "snapshots": {}}
        return plan

    # -- applying -------------------------------------------------------------

    def apply(self, plan: Plan) -> MutationResult:
        snapshots: dict[str, str | None] = plan.payload.get("snapshots", {})
        written: list[str] = []
        result = MutationResult(changed=False)

        try:
            for change in plan.files:
                if change.path in snapshots:
                    if _read_text(change.path) != snapshots[change.path]:
                        raise SafetyRefusalError(
                            f"{change.path} changed on disk after the plan was prepared; refusing to apply."
                        )
                elif change.content is not None and os.path.lexists(change.path):
                    raise ConflictError(f"{change.path} appeared after the plan was prepared.")
                if change.content is None:
                    if remove_file(change.path):
                        written.append(change.path)
                else:
                    atomic_write_text(change.path, change.content, mode=change.mode)
                    written.append(change.path)
            result.changed = True

            for command in plan.commands:
                self.runner.run(list(command.argv), env_policy=command.env_policy)
        except BaseException as exc:
            self._rollback(plan, written, snapshots)
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            if isinstance(exc, OperationalError | SafetyRefusalError):
                raise
            raise OperationalError(str(exc)) from exc

        result.files_written = tuple(written)
        result.messages.extend(self._post_check(plan))
        return result

    def _rollback(self, plan: Plan, written: list[str], snapshots: dict[str, str | None]) -> None:
        scope: Scope = plan.payload.get("scope", Scope.USER)
        timer_unit = plan.payload.get("timer_unit")
        if plan.action == "create" and timer_unit:
            with contextlib.suppress(Exception):
                self.runner.run(
                    self._systemctl(scope, "disable", "--now", timer_unit),
                    env_policy=self._env_policy(scope),
                    check=False,
                )
        for path in reversed(written):
            previous = snapshots.get(path)
            with contextlib.suppress(Exception):
                if previous is None:
                    remove_file(path)
                else:
                    atomic_write_text(path, previous, mode=0o644)
        with contextlib.suppress(Exception):
            self.runner.run(
                self._systemctl(scope, "daemon-reload"),
                env_policy=self._env_policy(scope),
                check=False,
            )

    def _post_check(self, plan: Plan) -> list[str]:
        scope: Scope = plan.payload["scope"]
        timer_unit = plan.payload["timer_unit"]
        action = plan.action
        if action in {"create", "enable", "disable"}:
            completed = self.runner.run(
                self._systemctl(scope, "is-enabled", timer_unit),
                env_policy=self._env_policy(scope),
                check=False,
            )
            state = completed.stdout.strip() or "unknown"
            return [f"Timer state: {state}"]
        return []

    # -- logs -----------------------------------------------------------------

    def logs(self, job: ScheduledJob, *, lines: int, since: str | None) -> str:
        if job.systemd is None or not job.systemd.service_unit:
            raise OperationalError(f"no service unit known for {job.name}")
        service_unit = job.systemd.service_unit
        if not is_safe_unit_name(service_unit):
            raise SafetyRefusalError(f"refusing to query unexpected unit name: {service_unit!r}")
        if not self.runner.has("journalctl"):
            raise DependencyMissingError("journalctl is not available")
        argv = ["journalctl", "--no-pager", "--unit", service_unit, f"--lines={lines}"]
        if since:
            argv.extend(["--since", since])
        if job.scope is Scope.USER:
            argv.insert(1, "--user")
        completed = self.runner.run(argv, env_policy=self._env_policy(job.scope), check=False)
        if completed.returncode != 0 and completed.stderr:
            raise OperationalError(completed.stderr.strip())
        return completed.stdout

    # -- helpers --------------------------------------------------------------

    def _validate_spec(self, spec: JobSpec) -> None:
        if spec.command.is_empty():
            raise InvalidScheduleError("no command given", hint="Provide a command after '--'.")
        if not spec.calendar:
            raise InvalidScheduleError("no schedule given")
        for expression in spec.calendar:
            self.validate_calendar(expression)
        if spec.jitter is not None:
            self._validate_timespan("--jitter", spec.jitter)
        if spec.accuracy is not None:
            self._validate_timespan("--accuracy", spec.accuracy)

    def _check_expression(self, expression: str) -> str:
        if has_unsafe_control_characters(expression):
            raise InvalidScheduleError("calendar expressions must not contain control characters")
        if expression.startswith("-"):
            raise InvalidScheduleError(f"calendar expression must not start with '-': {expression!r}")
        return expression

    def _validate_timespan(self, option: str, value: str) -> str:
        if has_unsafe_control_characters(value):
            raise InvalidScheduleError(f"{option} must not contain control characters")
        if value.startswith("-"):
            raise InvalidScheduleError(f"{option} must not start with '-': {value!r}")
        if not self.has_analyze():
            raise DependencyMissingError("systemd-analyze is required to validate timer durations.")
        completed = self.runner.run(["systemd-analyze", "timespan", value], check=False)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise InvalidScheduleError(f"invalid {option} value: {value!r}", hint=detail or None)
        return value

    def _verify_units(self, files: dict[str, str]) -> None:
        if not self.has_analyze():
            raise DependencyMissingError("systemd-analyze is required to validate generated units.")
        with tempfile.TemporaryDirectory(prefix="schedls-verify-") as tmp:
            paths = []
            for unit, content in files.items():
                validate_managed_unit_name(unit)
                path = os.path.join(tmp, unit)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                paths.append(path)
            completed = self.runner.run(["systemd-analyze", "verify", *paths], check=False)
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise SafetyRefusalError(
                    "generated units failed systemd-analyze verify.",
                    hint=detail or None,
                )

    def _create_summary(self, spec: JobSpec, service_unit: str, timer_unit: str) -> list[tuple[str, str]]:
        return [
            ("Backend", f"systemd {spec.scope.value} timer"),
            ("Schedule", ", ".join(spec.calendar)),
            ("Command", spec.command.display()),
            ("Timer", timer_unit),
            ("Service", service_unit),
        ]

    def _require_scope(self, scope: Scope) -> None:
        if scope is Scope.SYSTEM and os.geteuid() != 0:
            raise SafetyRefusalError(
                "creating a system timer requires appropriate privileges.",
                hint="Run the command under sudo yourself if that is your intention:\n  sudo schedls new ... --system",
            )

    def _unit_dir(self, scope: Scope) -> str:
        if scope is Scope.SYSTEM:
            return "/etc/systemd/system"
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            base = validate_absolute_path(xdg, what="XDG_CONFIG_HOME")
        else:
            home = validate_absolute_path(os.path.expanduser("~"), what="HOME")
            base = os.path.join(home, ".config")
        return os.path.join(base, "systemd", "user")

    def _systemctl(self, scope: Scope, *args: str) -> list[str]:
        if scope is Scope.USER:
            return ["systemctl", "--user", *args]
        return ["systemctl", *args]

    def _env_policy(self, scope: Scope) -> str:
        return "systemd" if scope is Scope.USER else "minimal"


def _parse_occurrences(text: str) -> list[datetime]:
    occurrences: list[datetime | None] = []
    for line in text.splitlines():
        match = _ITERATION_RE.search(line)
        if match:
            occurrences.append(parse_systemd_timestamp(match.group(1).strip()))
            continue
        utc_match = _UTC_LINE_RE.search(line)
        if utc_match and occurrences:
            parsed = _parse_utc_stamp(utc_match.group(1).strip())
            if parsed is not None:
                occurrences[-1] = parsed
    return [item for item in occurrences if item is not None]


def _parse_utc_stamp(stamp: str) -> datetime | None:
    text = stamp.strip()
    if text.endswith(" UTC"):
        text = text[: -len(" UTC")]
    try:
        parsed = datetime.strptime(text, "%a %Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _timers_calendar(value: str) -> tuple[str, ...]:
    found: list[str] = []
    for match in re.finditer(r"OnCalendar=([^;}\s]+(?:\s+[^;}\s]+)*)", value):
        expression = match.group(1).strip()
        if expression and not expression.startswith("n/a"):
            found.append(expression)
    return tuple(found)


def _job_name(unit: str, managed: bool) -> str:
    base = os.path.basename(unit)
    if base.endswith(_TIMER_SUFFIX):
        base = base[: -len(_TIMER_SUFFIX)]
    if managed and base.startswith("schedls-"):
        return base[len("schedls-") :]
    return base


def _enabled_from_state(state: str | None) -> bool | None:
    if state is None:
        return None
    return state in _ENABLED_STATES


def _is_true(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _environment_from_service(sections: dict[str, list[str]] | None) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for line in values(sections, "Service", "Environment"):
        key, sep, value = line.partition("=")
        if sep:
            result.append((key, value.strip('"')))
    return tuple(result)


def _command_from_service(sections: dict[str, list[str]] | None) -> Command:
    exec_lines = values(sections, "Service", "ExecStart")
    if not exec_lines:
        return Command()
    return renderer.parse_exec_start(exec_lines[0])


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None
