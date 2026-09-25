from __future__ import annotations

import os

import pytest

from schedls.backends.cron import CronBackend
from schedls.models import Backend, Command, JobSpec, Scope
from schedls.runner import CommandRunner

pytestmark = pytest.mark.integration

NAME = "schedls-it-cron"


def _backend_or_skip() -> CronBackend:
    backend = CronBackend(CommandRunner())
    if not backend.available():
        pytest.skip("crontab not available")
    if os.environ.get("SCHEDLS_RUN_CRON_TESTS") != "1":
        pytest.skip("set SCHEDLS_RUN_CRON_TESTS=1 to allow crontab mutation")
    return backend


def _restore(backend: CronBackend, original: str) -> None:
    backend.runner.run(["crontab", "-"], env_policy="identity", input_text=original, check=False)


def test_cron_lifecycle_preserves_content() -> None:
    backend = _backend_or_skip()
    original_document = backend.read()
    original = original_document.text
    # Add an unrelated line first so we can prove preservation.
    seeded = original + ("\n" if original and not original.endswith("\n") else "")
    seeded += "# unrelated comment\n"
    backend.runner.run(["crontab", "-"], env_policy="identity", input_text=seeded)
    try:
        spec = JobSpec(
            name=NAME,
            backend=Backend.CRON,
            scope=Scope.USER,
            command=Command(argv=("/usr/bin/true", "a b", "50%")),
            cron_expression="15 2 * * *",
        )
        plan = backend.plan_create(spec)
        result = backend.apply(plan)
        assert result.changed

        jobs = [job for job in backend.discover([Scope.USER]) if job.name == NAME]
        assert len(jobs) == 1
        assert jobs[0].managed is True
        assert jobs[0].schedule.expression == "15 2 * * *"

        raw = backend.read().text
        assert "# unrelated comment" in raw
        assert "a\\%b" not in raw  # sanity: percent is escaped, not raw in a bad way

        job = backend.find(NAME)
        assert job is not None
        backend.apply(backend.plan_remove(job))
        assert "# unrelated comment" in backend.read().text
        assert backend.find(NAME) is None
    finally:
        _restore(backend, original)
