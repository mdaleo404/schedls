from __future__ import annotations

import io
import os
import tempfile
from dataclasses import replace

import pytest

from schedls import __version__
from schedls.backends.base import CommandPlan, FileChange, MutationResult, Plan
from schedls.backends.cron import CronBackend
from schedls.backends.systemd import SystemdBackend
from schedls.errors import InvalidScheduleError, OperationalError, SafetyRefusalError
from schedls.interact import Interaction
from schedls.models import (
    Backend,
    Command,
    CronDetails,
    JobSource,
    JobSpec,
    Schedule,
    ScheduledJob,
    ScheduleKind,
    Scope,
    SystemdDetails,
)
from schedls.operations import doctor as doctor_ops
from schedls.operations import inspect as inspect_ops
from schedls.operations import mutate as mutate_ops
from schedls.output import Output

from ..conftest import FakeRunner


def _job(name: str = "backup") -> ScheduledJob:
    return ScheduledJob(
        name=name,
        backend=Backend.SYSTEMD,
        scope=Scope.USER,
        managed=True,
        enabled=True,
        schedule=Schedule(ScheduleKind.CALENDAR, "*-*-* 02:00:00"),
        command=Command(argv=("/usr/local/bin/backup",)),
        source=JobSource("systemd user timer"),
    )


def _output() -> tuple[Output, io.StringIO]:
    stream = io.StringIO()
    return Output(color="never", stdout=stream), stream


def test_render_list_table() -> None:
    output, stream = _output()
    inspect_ops.render_list(output, [_job(), _job("cleanup")], [])
    text = stream.getvalue()
    assert "NAME" in text and "SCHEDULE" in text
    assert "backup" in text and "cleanup" in text


def test_render_list_json() -> None:
    output, stream = _output()
    output.json_mode = True
    inspect_ops.render_list(output, [_job()], ["note"])
    assert '"schema_version": 1' in stream.getvalue()


def test_render_show() -> None:
    output, stream = _output()
    inspect_ops.render_show(output, _job())
    text = stream.getvalue()
    assert "Managed by schedls" in text
    assert "systemd user timer" in text


def test_render_show_aligns_systemd_details() -> None:
    job = replace(
        _job(),
        systemd=SystemdDetails(
            timer_path="/var/lib/schedls/backup.timer",
            service_path="/var/lib/schedls/backup.service",
            persistent=True,
        ),
    )
    output, stream = _output()

    inspect_ops.render_show(output, job)

    backend = next(line for line in stream.getvalue().splitlines() if line.startswith("Backend"))
    timer = next(line for line in stream.getvalue().splitlines() if line.startswith("Timer"))
    assert backend.index("systemd") == timer.index("/var/lib/schedls/backup.timer")


def test_render_show_explains_unavailable_cron_history() -> None:
    job = ScheduledJob(
        name="cron-1",
        backend=Backend.CRON,
        scope=Scope.USER,
        managed=False,
        enabled=None,
        schedule=Schedule(ScheduleKind.CRON, "0 2 * * *"),
        command=Command(raw="/usr/bin/backup"),
        source=JobSource("current user's crontab", line=1),
        cron=CronDetails(expression="0 2 * * *", line=1),
    )
    output, stream = _output()

    inspect_ops.render_show(output, job)

    text = stream.getvalue()
    previous = next(line for line in text.splitlines() if line.startswith("Previous"))
    assert "unavailable (cron does not provide per-job history)" in previous
    assert "\n\nBackend" not in text
    assert "\n\nSource" in text
    status = next(line for line in text.splitlines() if line.startswith("Status"))
    source = next(line for line in text.splitlines() if line.startswith("Source"))
    assert status.index("active") == source.index("current user's crontab")


def test_render_show_keeps_cron_next_run_null_in_json() -> None:
    job = ScheduledJob(
        name="cron-1",
        backend=Backend.CRON,
        scope=Scope.USER,
        managed=False,
        enabled=None,
        schedule=Schedule(ScheduleKind.CRON, "0 2 * * *"),
        command=Command(raw="/usr/bin/backup"),
        source=JobSource("current user's crontab", line=1),
        cron=CronDetails(expression="0 2 * * *", line=1),
    )
    output, stream = _output()
    output.json_mode = True

    inspect_ops.render_show(output, job)

    assert '"next_run": null' in stream.getvalue()


def test_filter_jobs() -> None:
    jobs = [_job(), _job("other")]
    assert inspect_ops.filter_jobs(jobs, managed=True) == jobs
    assert inspect_ops.filter_jobs(jobs, managed=False) == []
    assert inspect_ops.filter_jobs(jobs, enabled=False) == []
    assert len(inspect_ops.filter_jobs(jobs, backend=Backend.SYSTEMD)) == 2


def test_collect_reports_unavailable_backend() -> None:
    backend = SystemdBackend(FakeRunner())
    jobs, warnings = inspect_ops.collect([backend], [Scope.USER])
    assert jobs == []
    assert any("unavailable" in warning for warning in warnings)


class _DummyBackend:
    name = "systemd"

    def __init__(self, result: MutationResult) -> None:
        self.result = result
        self.applied = False

    def apply(self, plan: Plan) -> MutationResult:
        self.applied = True
        return self.result


def test_run_plan_dry_run(tmp_path) -> None:
    output, stream = _output()
    plan = Plan(backend="systemd", action="create")
    plan.files = [FileChange(str(tmp_path / "example.service"), "data")]
    plan.commands = [CommandPlan(("systemctl", "reload"), "reload")]
    backend = _DummyBackend(MutationResult(changed=True))
    result = mutate_ops.run_plan(backend, plan, output, Interaction(output=output), dry_run=True)
    assert result.dry_run is True
    assert backend.applied is False
    assert "No changes made." in stream.getvalue()


def test_run_plan_confirms_and_applies() -> None:
    output, stream = _output()
    plan = Plan(backend="systemd", action="create")
    backend = _DummyBackend(MutationResult(changed=True, messages=["done"]))
    interaction = Interaction(output=output, assume_yes=True)
    result = mutate_ops.run_plan(backend, plan, output, interaction)
    assert backend.applied is True
    assert result.changed is True
    assert "done" in stream.getvalue()


def test_run_plan_json_mode() -> None:
    output, stream = _output()
    output.json_mode = True
    plan = Plan(backend="systemd", action="create")
    backend = _DummyBackend(MutationResult(changed=True))
    mutate_ops.run_plan(backend, plan, output, Interaction(output=output, assume_yes=True))
    assert '"action": "create"' in stream.getvalue()


def _doctor_runner() -> FakeRunner:
    def systemctl(argv, _input):
        if "is-system-running" in argv:
            return ("running\n", "")
        return ("", "")

    def crontab(argv, _input):
        if "-V" in argv:
            return ("cronie 1.7.2\n", "")
        return ("", "")

    return FakeRunner(
        handlers={
            "systemctl": systemctl,
            "crontab": crontab,
            "loginctl": lambda argv, _input: ("Linger=no\n", ""),
        },
        available={"systemctl", "systemd-analyze", "crontab", "loginctl"},
    )


def test_doctor_human() -> None:
    runner = _doctor_runner()
    output, stream = _output()
    doctor_ops.run_doctor(runner, output, SystemdBackend(runner), CronBackend(runner))
    text = stream.getvalue()
    assert "Cronie" in text
    assert "usable" in text
    version = next(line for line in text.splitlines() if line.startswith("version"))
    available = next(line for line in text.splitlines() if line.startswith("available"))
    crontab = next(line for line in text.splitlines() if line.startswith("crontab"))
    assert version.index(__version__) == available.index("yes") == crontab.index("yes")


def test_doctor_json() -> None:
    runner = _doctor_runner()
    output, stream = _output()
    output.json_mode = True
    doctor_ops.run_doctor(runner, output, SystemdBackend(runner), CronBackend(runner))
    assert '"result": "usable"' in stream.getvalue()


def test_systemd_backend_discovery_parses_units() -> None:
    timer = (
        "# Managed by schedls\n"
        "# Name: backup\n"
        "[Unit]\nDescription=foo\n[Timer]\nOnCalendar=*-*-* 02:00:00\nPersistent=true\nUnit=schedls-backup.service\n"
    )
    service = '[Service]\nExecStart="/usr/local/bin/backup" "/srv/data"\n'

    with tempfile.TemporaryDirectory() as tmp:
        timer_path = os.path.join(tmp, "schedls-backup.timer")
        service_path = os.path.join(tmp, "schedls-backup.service")
        with open(timer_path, "w") as handle:
            handle.write(timer)
        with open(service_path, "w") as handle:
            handle.write(service)

        def systemctl(argv, _input):
            if "is-system-running" in argv:
                return ("running\n", "")
            if "list-unit-files" in argv or "list-units" in argv:
                return ("schedls-backup.timer enabled\n", "")
            if "show" in argv:
                unit = argv[argv.index("show") + 1]
                if unit.endswith(".timer"):
                    return (
                        f"Id={unit}\nLoadState=loaded\nFragmentPath={timer_path}\n"
                        "UnitFileState=enabled\nActiveState=active\nSubState=waiting\n"
                        "NextElapseUSecRealtime=1758700800000000\nTriggers=schedls-backup.service\n",
                        "",
                    )
                return (
                    f"Id={unit}\nLoadState=loaded\nFragmentPath={service_path}\nResult=success\n",
                    "",
                )
            return ("", "")

        runner = FakeRunner(handlers={"systemctl": systemctl}, available={"systemctl"})
        backend = SystemdBackend(runner)
        jobs = backend.discover([Scope.USER])
        assert len(jobs) == 1
        job = jobs[0]
        assert job.name == "backup"
        assert job.managed is True
        assert job.schedule.expression == "*-*-* 02:00:00"
        assert job.command.argv == ("/usr/local/bin/backup", "/srv/data")
        assert job.enabled is True
        assert job.next_run is not None
        assert job.last_result == "success"


def test_systemd_find_uses_direct_lookup_and_cached_manager_check() -> None:
    def systemctl(argv, _input):
        if "is-system-running" in argv:
            return ("running\n", "")
        if "show" in argv:
            unit = argv[argv.index("show") + 1]
            if unit == "schedls-backup.timer":
                return ("LoadState=not-found\n", "")
            if unit == "backup.timer":
                return ("LoadState=loaded\nUnitFileState=enabled\n", "")
        return ("", "")

    runner = FakeRunner(handlers={"systemctl": systemctl}, available={"systemctl"})
    backend = SystemdBackend(runner)

    assert backend.available() is True
    job = backend.find("backup")

    assert job is not None
    assert job.name == "backup"
    calls = [argv for argv, _, _ in runner.calls]
    assert sum("is-system-running" in argv for argv in calls) == 1
    assert not any("list-unit-files" in argv or "list-units" in argv for argv in calls)


def test_plan_update_refuses_unmanaged() -> None:
    runner = FakeRunner()
    backend = SystemdBackend(runner)
    unmanaged = replace(_job("certbot"), managed=False)
    spec = JobSpec(
        name="certbot",
        backend=Backend.SYSTEMD,
        scope=Scope.USER,
        command=Command(argv=("/bin/true",)),
        calendar=("*-*-* 01:00:00",),
    )
    with pytest.raises(SafetyRefusalError):
        backend.plan_update(unmanaged, spec)


def _managed_job(name: str = "backup", *, scope: Scope = Scope.USER, service_unit: str | None = None) -> ScheduledJob:
    return replace(
        _job(name),
        scope=scope,
        systemd=SystemdDetails(
            timer_unit=f"schedls-{name}.timer",
            service_unit=service_unit or f"schedls-{name}.service",
            on_calendar=("*-*-* 02:00:00",),
        ),
    )


def _spec(name: str = "backup") -> JobSpec:
    return JobSpec(
        name=name,
        backend=Backend.SYSTEMD,
        scope=Scope.USER,
        command=Command(argv=("/bin/true",)),
        calendar=("*-*-* 03:00:00",),
    )


def _analyze_runner() -> FakeRunner:
    def analyze(argv, _input):
        if "calendar" in argv:
            return ("Normalized form: *-*-* 03:00:00\n", "")
        return ("", "")

    return FakeRunner(handlers={"systemd-analyze": analyze}, available={"systemd-analyze"})


def test_plan_update_derives_unit_names(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    backend = SystemdBackend(_analyze_runner())
    job = _managed_job(service_unit="../../../../tmp/evil.service")
    plan = backend.plan_update(job, _spec())
    expected_dir = os.path.join(str(tmp_path), "systemd", "user")
    assert sorted(change.path for change in plan.files) == [
        os.path.join(expected_dir, "schedls-backup.service"),
        os.path.join(expected_dir, "schedls-backup.timer"),
    ]
    assert "evil" not in " ".join(change.path for change in plan.files)


def test_verify_units_rejects_traversal() -> None:
    backend = SystemdBackend(_analyze_runner())
    with pytest.raises(SafetyRefusalError):
        backend._verify_units({"../../evil.service": "content"})


def test_unit_dir_rejects_relative_xdg(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    backend = SystemdBackend(FakeRunner())
    with pytest.raises(SafetyRefusalError):
        backend._unit_dir(Scope.USER)


def test_plan_update_requires_scope(monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    backend = SystemdBackend(_analyze_runner())
    with pytest.raises(SafetyRefusalError):
        backend.plan_update(_managed_job(scope=Scope.SYSTEM), _spec())


def test_validate_spec_rejects_unsafe_calendar() -> None:
    backend = SystemdBackend(FakeRunner())
    spec = replace(_spec(), calendar=("-x",))
    with pytest.raises(InvalidScheduleError):
        backend._validate_spec(spec)


def test_validate_spec_rejects_unsafe_jitter() -> None:
    backend = SystemdBackend(_analyze_runner())
    for jitter in ("5min\nExecStart=/bin/true", "-5min"):
        with pytest.raises(InvalidScheduleError):
            backend._validate_spec(replace(_spec(), jitter=jitter))


def test_apply_rolls_back_on_keyboard_interrupt(tmp_path) -> None:
    target = tmp_path / "schedls-backup.service"
    raised = []

    def systemctl(argv, _input):
        if not raised:
            raised.append(True)
            raise KeyboardInterrupt
        return ("", "")

    runner = FakeRunner(handlers={"systemctl": systemctl}, available={"systemctl"})
    backend = SystemdBackend(runner)
    plan = Plan(backend="systemd", action="update")
    plan.files = [FileChange(str(target), "content", mode=0o600)]
    plan.commands = [CommandPlan(("systemctl", "daemon-reload"), "reload")]
    plan.payload = {"scope": Scope.USER, "snapshots": {}}
    with pytest.raises(KeyboardInterrupt):
        backend.apply(plan)
    assert not target.exists()


def test_rollback_disables_timer_on_create_failure(tmp_path) -> None:
    target = tmp_path / "schedls-backup.timer"

    def systemctl(argv, _input):
        if "daemon-reload" in argv:
            return (1, "", "boom")
        return ("", "")

    runner = FakeRunner(handlers={"systemctl": systemctl}, available={"systemctl"})
    backend = SystemdBackend(runner)
    plan = Plan(backend="systemd", action="create")
    plan.files = [FileChange(str(target), "content", mode=0o600)]
    plan.commands = [CommandPlan(("systemctl", "daemon-reload"), "reload")]
    plan.payload = {"scope": Scope.USER, "timer_unit": "schedls-backup.timer", "snapshots": {}}
    with pytest.raises(OperationalError):
        backend.apply(plan)
    assert any("disable" in call[0] for call in runner.calls)
    assert not target.exists()


def test_logs_rejects_unsafe_service_unit() -> None:
    backend = SystemdBackend(FakeRunner(available={"journalctl"}))
    job = _managed_job(service_unit="--output=json.service")
    with pytest.raises(SafetyRefusalError):
        backend.logs(job, lines=5, since=None)


def test_logs_returns_journal_text() -> None:
    runner = FakeRunner(
        handlers={"journalctl": lambda argv, _input: ("hello\n", "")},
        available={"journalctl"},
    )
    backend = SystemdBackend(runner)
    assert backend.logs(_managed_job(), lines=5, since="2 hours ago") == "hello\n"
    argv = runner.calls[0][0]
    assert "--since" in argv and "--lines=5" in argv


def test_prefix_named_unit_without_marker_is_unmanaged(tmp_path) -> None:
    timer_path = tmp_path / "schedls-backup.timer"
    timer_path.write_text("[Unit]\n[Timer]\nOnCalendar=*-*-* 02:00:00\n")

    def systemctl(argv, _input):
        if "is-system-running" in argv:
            return ("running\n", "")
        if "list-unit-files" in argv or "list-units" in argv:
            return ("schedls-backup.timer enabled\n", "")
        if "show" in argv:
            unit = argv[argv.index("show") + 1]
            if unit.endswith(".timer"):
                return (f"Id={unit}\nLoadState=loaded\nFragmentPath={timer_path}\n", "")
            return ("", "")
        return ("", "")

    runner = FakeRunner(handlers={"systemctl": systemctl}, available={"systemctl"})
    jobs = SystemdBackend(runner).discover([Scope.USER])
    assert len(jobs) == 1
    assert jobs[0].managed is False
    assert jobs[0].name == "schedls-backup"
