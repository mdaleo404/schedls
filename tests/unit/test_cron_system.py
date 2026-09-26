from __future__ import annotations

import os

import pytest

from schedls.backends.base import FileChange, Plan
from schedls.backends.cron import CronBackend, CronPaths
from schedls.errors import ConflictError, InvalidScheduleError, SafetyRefusalError
from schedls.models import Backend, Command, JobSpec, Scope
from schedls.renderers import cron as renderer


def _make_paths(tmp_path) -> CronPaths:
    root = tmp_path / "etc"
    root.mkdir()
    return CronPaths(
        crontab=str(root / "crontab"),
        cron_d=str(root / "cron.d"),
        hourly=str(root / "cron.hourly"),
        daily=str(root / "cron.daily"),
        weekly=str(root / "cron.weekly"),
        monthly=str(root / "cron.monthly"),
        spool_crontabs=str(tmp_path / "var" / "spool" / "cron" / "crontabs"),
        spool=str(tmp_path / "var" / "spool" / "cron"),
    )


def _write(path, text: str, mode: int = 0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def test_available_without_crontab_uses_file_sources(tmp_path, fake_runner) -> None:
    paths = _make_paths(tmp_path)
    assert CronBackend(fake_runner, paths).available() is False
    _write(tmp_path / "etc/cron.d/backup", "0 2 * * * root /usr/bin/backup\n")
    assert CronBackend(fake_runner, paths).available() is True


def test_discovers_system_crontabs(tmp_path, fake_runner) -> None:
    paths = _make_paths(tmp_path)
    _write(
        tmp_path / "etc/crontab",
        "SHELL=/bin/sh\n17 * * * * root /usr/bin/hourly\n@daily www-data /usr/bin/report\n",
    )
    _write(tmp_path / "etc/cron.d/backup", "0 2 * * * root /usr/bin/backup\n")
    _write(
        tmp_path / "etc/cron.d/schedls-cleanup",
        renderer.render_system_file("cleanup", ["30 3 * * 0 deploy /usr/bin/cleanup"]),
    )
    _write(tmp_path / "etc/cron.d/with.dot", "0 2 * * * root /usr/bin/ignored\n")
    _write(tmp_path / "etc/cron.d/.placeholder", "")

    jobs = CronBackend(fake_runner, paths).discover([Scope.SYSTEM])
    by_name = {job.name: job for job in jobs}
    assert set(by_name) == {"crontab:2", "crontab:3", "backup:1", "cleanup"}

    assert by_name["crontab:2"].scope is Scope.SYSTEM
    assert by_name["crontab:2"].cron.user == "root"
    assert by_name["crontab:2"].command.raw == "/usr/bin/hourly"
    assert by_name["crontab:3"].cron.user == "www-data"
    assert by_name["backup:1"].managed is False
    assert by_name["backup:1"].schedule.expression == "0 2 * * *"
    assert by_name["backup:1"].source.path == paths.cron_d + "/backup"

    managed = by_name["cleanup"]
    assert managed.managed is True
    assert managed.cron.user == "deploy"
    assert managed.schedule.expression == "30 3 * * 0"
    assert managed.source.path == paths.cron_d + "/schedls-cleanup"


def test_discovers_periodic_directories(tmp_path, fake_runner) -> None:
    paths = _make_paths(tmp_path)
    _write(tmp_path / "etc/cron.daily/logrotate", "#!/bin/sh\n", mode=0o755)
    _write(tmp_path / "etc/cron.daily/README", "text\n", mode=0o644)
    _write(tmp_path / "etc/cron.hourly/.hidden", "#!/bin/sh\n", mode=0o755)

    jobs = CronBackend(fake_runner, paths).discover([Scope.SYSTEM])
    assert [job.name for job in jobs] == ["logrotate"]
    job = jobs[0]
    assert job.schedule.expression == "@daily"
    assert job.command.display() == paths.daily + "/logrotate"
    assert job.cron.user == "root"
    assert job.managed is False
    assert "cron.daily" in job.source.detail


def test_spool_crontabs_require_root(tmp_path, fake_runner, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    _write(tmp_path / "var/spool/cron/crontabs/alice", "5 4 * * * /usr/bin/alice\n")
    _write(tmp_path / "var/spool/cron/crontabs/root", "5 4 * * * /usr/bin/root\n")
    backend = CronBackend(fake_runner, paths)

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert backend.discover([Scope.USER]) == []

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(CronBackend, "_current_user", staticmethod(lambda: "root"))
    jobs = backend.discover([Scope.USER])
    assert [job.name for job in jobs] == ["alice:1"]
    assert jobs[0].scope is Scope.USER
    assert jobs[0].cron.user == "alice"
    assert jobs[0].source.path.endswith("crontabs/alice")


def test_spool_falls_back_to_flat_layout(tmp_path, fake_runner, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    _write(tmp_path / "var/spool/cron/alice", "5 4 * * * /usr/bin/alice\n")
    (tmp_path / "var/spool/cron/atjobs").mkdir(parents=True)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(CronBackend, "_current_user", staticmethod(lambda: "root"))

    jobs = CronBackend(fake_runner, paths).discover([Scope.USER])
    assert [job.name for job in jobs] == ["alice:1"]


def test_malformed_drop_in_is_unmanaged(tmp_path, fake_runner) -> None:
    paths = _make_paths(tmp_path)
    _write(tmp_path / "etc/cron.d/schedls-broken", "0 2 * * * root /bin/true\n")

    jobs = CronBackend(fake_runner, paths).discover([Scope.SYSTEM])
    assert [job.name for job in jobs] == ["schedls-broken:1"]
    assert jobs[0].managed is False
    assert jobs[0].warnings


def _system_spec(**overrides) -> JobSpec:
    values = {
        "name": "backup",
        "backend": Backend.CRON,
        "scope": Scope.SYSTEM,
        "command": Command(argv=("/usr/bin/backup", "/srv/data")),
        "cron_expression": "0 2 * * *",
        "run_as": "www-data",
    }
    values.update(overrides)
    return JobSpec(**values)


def test_plan_create_system_cron(tmp_path, fake_runner, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    (tmp_path / "etc/cron.d").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("schedls.backends.cron.check_trusted_directory", lambda path, *, expected_uid: None)

    plan = CronBackend(fake_runner, paths).plan_create(_system_spec())
    assert plan.payload["mode"] == "file"
    assert plan.payload["expected_uid"] == 0
    assert not plan.commands
    change = plan.files[0]
    assert change.path == paths.cron_d + "/schedls-backup"
    assert "0 2 * * * www-data /usr/bin/backup /srv/data" in change.content
    assert "# schedls:begin name=backup" in change.content
    assert ("Run as", "www-data") in plan.summary


def test_plan_create_system_cron_requires_root(tmp_path, fake_runner, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SafetyRefusalError):
        CronBackend(fake_runner, paths).plan_create(_system_spec())


def test_plan_create_system_cron_rejects_dotted_name(tmp_path, fake_runner, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(InvalidScheduleError):
        CronBackend(fake_runner, paths).plan_create(_system_spec(name="my.job"))


def test_plan_create_system_cron_conflict(tmp_path, fake_runner, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    _write(tmp_path / "etc/cron.d/schedls-backup", "0 2 * * * root /bin/true\n")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("schedls.backends.cron.check_trusted_directory", lambda path, *, expected_uid: None)
    with pytest.raises(ConflictError):
        CronBackend(fake_runner, paths).plan_create(_system_spec())


def test_apply_file_mode_create_and_remove(tmp_path, fake_runner) -> None:
    paths = _make_paths(tmp_path)
    (tmp_path / "etc/cron.d").mkdir(parents=True, exist_ok=True)
    backend = CronBackend(fake_runner, paths)
    uid = os.getuid()
    path = tmp_path / "etc/cron.d/schedls-backup"
    content = renderer.render_system_file("backup", ["0 2 * * * root /usr/bin/backup"])

    create = Plan(backend="cron", action="create")
    create.files = [FileChange(path=str(path), content=content, mode=0o644, expected_uid=uid)]
    create.payload = {
        "mode": "file",
        "action": "create",
        "name": "backup",
        "path": str(path),
        "expected_uid": uid,
        "snapshots": {str(path): None},
    }
    result = backend.apply(create)
    assert result.changed
    assert path.read_text() == content

    remove = Plan(backend="cron", action="remove")
    remove.files = [FileChange(path=str(path), content=None, expected_uid=uid)]
    remove.payload = {
        "mode": "file",
        "action": "remove",
        "name": "backup",
        "path": str(path),
        "expected_uid": uid,
        "snapshots": {str(path): content},
    }
    result = backend.apply(remove)
    assert result.changed
    assert not path.exists()


def test_apply_file_mode_refuses_changed_snapshot(tmp_path, fake_runner) -> None:
    paths = _make_paths(tmp_path)
    _write(tmp_path / "etc/cron.d/schedls-backup", "changed\n")
    backend = CronBackend(fake_runner, paths)
    path = tmp_path / "etc/cron.d/schedls-backup"

    plan = Plan(backend="cron", action="remove")
    plan.files = [FileChange(path=str(path), content=None, expected_uid=os.getuid())]
    plan.payload = {
        "mode": "file",
        "action": "remove",
        "name": "backup",
        "path": str(path),
        "expected_uid": os.getuid(),
        "snapshots": {str(path): "expected\n"},
    }
    with pytest.raises(SafetyRefusalError):
        backend.apply(plan)
    assert path.exists()


def test_apply_file_mode_rolls_back_on_failure(tmp_path, fake_runner) -> None:
    paths = _make_paths(tmp_path)
    (tmp_path / "etc/cron.d").mkdir(parents=True, exist_ok=True)
    backend = CronBackend(fake_runner, paths)
    uid = os.getuid()
    first = tmp_path / "etc/cron.d/schedls-one"
    second = tmp_path / "etc/cron.d/schedls-two"
    _write(second, "conflict\n")

    plan = Plan(backend="cron", action="create")
    plan.files = [
        FileChange(path=str(first), content=renderer.render_system_file("one", ["0 2 * * * root /bin/true"])),
        FileChange(path=str(second), content=renderer.render_system_file("two", ["0 3 * * * root /bin/true"])),
    ]
    plan.payload = {
        "mode": "file",
        "action": "create",
        "name": "one",
        "path": str(first),
        "expected_uid": uid,
        "snapshots": {str(first): None, str(second): None},
    }
    with pytest.raises(SafetyRefusalError):
        backend.apply(plan)
    assert not first.exists()


def test_plan_remove_system_cron(tmp_path, fake_runner, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    _write(
        tmp_path / "etc/cron.d/schedls-cleanup",
        renderer.render_system_file("cleanup", ["30 3 * * 0 root /usr/bin/cleanup"]),
    )
    backend = CronBackend(fake_runner, paths)
    job = backend.discover([Scope.SYSTEM])[0]
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    plan = backend.plan_remove(job)
    assert plan.payload["mode"] == "file"
    assert plan.files[0].path == paths.cron_d + "/schedls-cleanup"
    assert plan.files[0].content is None


def test_plan_remove_system_cron_requires_root(tmp_path, fake_runner, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    _write(
        tmp_path / "etc/cron.d/schedls-cleanup",
        renderer.render_system_file("cleanup", ["30 3 * * 0 root /usr/bin/cleanup"]),
    )
    backend = CronBackend(fake_runner, paths)
    job = backend.discover([Scope.SYSTEM])[0]
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SafetyRefusalError):
        backend.plan_remove(job)


def test_plan_remove_refuses_unmanaged(tmp_path, fake_runner) -> None:
    paths = _make_paths(tmp_path)
    _write(tmp_path / "etc/cron.d/backup", "0 2 * * * root /usr/bin/backup\n")
    backend = CronBackend(fake_runner, paths)
    job = backend.discover([Scope.SYSTEM])[0]
    with pytest.raises(SafetyRefusalError):
        backend.plan_remove(job)
