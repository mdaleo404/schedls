from __future__ import annotations

import os

import pytest

from schedls.backends.systemd import SystemdBackend
from schedls.models import Backend, Command, JobSpec, Scope
from schedls.runner import CommandRunner

pytestmark = pytest.mark.integration

NAME = "schedls-it"


def _backend_or_skip() -> SystemdBackend:
    backend = SystemdBackend(CommandRunner())
    if not backend.has_systemctl():
        pytest.skip("systemctl not available")
    if not backend.manager_ok(Scope.USER):
        pytest.skip("user systemd manager not reachable")
    if not backend.has_analyze():
        pytest.skip("systemd-analyze not available")
    return backend


def _spec(calendar: str) -> JobSpec:
    return JobSpec(
        name=NAME,
        backend=Backend.SYSTEMD,
        scope=Scope.USER,
        command=Command(argv=("/bin/true",)),
        calendar=(calendar,),
        persistent=True,
    )


def test_systemd_timer_lifecycle() -> None:
    backend = _backend_or_skip()
    directory = backend._unit_dir(Scope.USER)
    timer_path = os.path.join(directory, f"schedls-{NAME}.timer")
    service_path = os.path.join(directory, f"schedls-{NAME}.service")
    # Clean any leftover from a previous run.
    if os.path.exists(timer_path):
        existing = backend.find(NAME)
        if existing is not None:
            backend.apply(backend.plan_remove(existing))
    try:
        plan = backend.plan_create(_spec("*-*-* 03:30:00"))
        result = backend.apply(plan)
        assert result.changed
        assert os.path.exists(timer_path)
        assert os.path.exists(service_path)

        job = backend.find(NAME)
        assert job is not None
        assert job.managed is True
        assert job.schedule.expression == "*-*-* 03:30:00"
        assert job.command.argv == ("/bin/true",)

        backend.apply(backend.plan_set_enabled(job, False))
        job = backend.find(NAME)
        assert job is not None and job.enabled is False

        backend.apply(backend.plan_set_enabled(job, True))
        job = backend.find(NAME)
        assert job is not None and job.enabled is True

        backend.apply(backend.plan_update(job, _spec("*-*-* 05:15:00")))
        job = backend.find(NAME)
        assert job is not None
        assert job.schedule.expression == "*-*-* 05:15:00"

        backend.apply(backend.plan_remove(job))
        assert not os.path.exists(timer_path)
        assert not os.path.exists(service_path)
        assert backend.find(NAME) is None
    finally:
        leftover = backend.find(NAME)
        if leftover is not None:
            backend.apply(backend.plan_remove(leftover))
