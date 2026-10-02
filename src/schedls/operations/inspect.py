"""Discovery, listing and detail views."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime

from ..backends.base import SchedulerBackend
from ..cronexpr import next_occurrence
from ..errors import SchedlsError
from ..models import Backend, ScheduledJob, Scope
from ..output import Output, jobs_document
from ..timefmt import format_short


def collect(backends: Sequence[SchedulerBackend], scopes: Sequence[Scope]) -> tuple[list[ScheduledJob], list[str]]:
    jobs: list[ScheduledJob] = []
    warnings: list[str] = []
    for backend in backends:
        if not backend.available():
            warnings.append(f"{backend.name} backend is unavailable; skipping it.")
            continue
        try:
            jobs.extend(backend.discover(scopes))
        except SchedlsError as exc:
            warnings.append(f"{backend.name} discovery failed: {exc.message}")
    jobs.sort(key=lambda job: (job.name, job.backend.value))
    if os.geteuid() != 0 and Scope.SYSTEM in scopes:
        warnings.append(
            "some system schedules could not be inspected with your current permissions.\n"
            "Run schedls as root if you intentionally want wider visibility."
        )
    return jobs, warnings


def job_status(job: ScheduledJob) -> str:
    if job.enabled is False:
        return "disabled"
    if job.backend is Backend.SYSTEMD and job.systemd is not None:
        sub = job.systemd.sub_state or ""
        if sub in {"waiting", "running"}:
            return sub
        return job.systemd.active_state or "unknown"
    if job.backend is Backend.CRON:
        return "active"
    return "unknown"


def filter_jobs(
    jobs: Sequence[ScheduledJob],
    *,
    scope: Scope | None = None,
    backend: Backend | None = None,
    managed: bool | None = None,
    enabled: bool | None = None,
) -> list[ScheduledJob]:
    selected = []
    for job in jobs:
        if scope is not None and job.scope is not scope:
            continue
        if backend is not None and job.backend is not backend:
            continue
        if managed is not None and job.managed is not managed:
            continue
        if enabled is not None and job.enabled is not enabled:
            continue
        selected.append(job)
    return selected


def render_list(output: Output, jobs: Sequence[ScheduledJob], warnings: Sequence[str]) -> None:
    if output.json_mode:
        output.emit_json(jobs_document(jobs, warnings))
        return
    if not jobs:
        output.line("No scheduled jobs found.")
    else:
        rows = []
        for job in jobs:
            next_run = _next_run(job)
            next_text = format_short(next_run, utc=output.utc) if next_run else "—"
            rows.append(
                [
                    job.name,
                    job.schedule_text(),
                    next_text,
                    job.backend.value,
                    job.scope.value,
                    job_status(job),
                ]
            )
        output.table(["NAME", "SCHEDULE", "NEXT", "BACKEND", "SCOPE", "STATUS"], rows)
    for warning in warnings:
        output.diagnostic(f"\nNote: {warning}")


def render_show(output: Output, job: ScheduledJob) -> None:
    if output.json_mode:
        output.emit_json(jobs_document([job]))
        return
    output.heading(job.name)
    output.line()
    output.key_values([("Status", job_status(job))])
    output.line()
    output.key_values([("Backend", _backend_label(job))])
    output.line()
    output.key_values([("Managed by schedls", "yes" if job.managed else "no")])
    output.line()
    output.key_values([("Schedule", job.schedule_text())])
    if job.schedule.expressions:
        for expression in job.schedule.expressions:
            output.key_values([("OnCalendar", expression)])
    output.line()
    next_run = _next_run(job)
    output.key_values([("Next", output.job_datetime(next_run) or _next_unavailable(job))])
    output.line()
    previous = output.job_datetime(job.last_run) or _previous_unavailable(job)
    if job.last_result:
        previous = f"{previous}\nresult: {job.last_result}"
    output.key_values([("Previous", previous)])
    output.line()
    output.key_values([("Command", job.command.display() or "unknown")])
    if job.systemd is not None:
        if job.systemd.timer_path:
            output.line()
            output.key_values([("Timer", job.systemd.timer_path)])
        if job.systemd.service_path:
            output.key_values([("Service", job.systemd.service_path)])
        extras = []
        extras.append(("Persistent", "yes" if job.systemd.persistent else "no"))
        if job.systemd.jitter:
            extras.append(("Randomized delay", job.systemd.jitter))
        if job.systemd.accuracy:
            extras.append(("Accuracy", job.systemd.accuracy))
        if job.systemd.working_directory:
            extras.append(("Working directory", job.systemd.working_directory))
        for key, value in job.systemd.environment:
            extras.append(("Environment", f"{key}={value}"))
        if extras:
            output.line()
            output.key_values(extras)
    if job.cron is not None:
        output.line()
        output.key_values([("Source", job.source.detail)])
        if job.source.path:
            output.key_values([("File", job.source.path)])
        if job.cron.user:
            output.key_values([("Run as", job.cron.user)])
        if job.cron.shell:
            output.key_values([("SHELL", job.cron.shell)])
        if job.cron.mailto:
            output.key_values([("MAILTO", job.cron.mailto)])
    for warning in job.warnings:
        output.line()
        output.diagnostic(f"Warning: {warning}")


def _backend_label(job: ScheduledJob) -> str:
    if job.backend is Backend.SYSTEMD:
        return f"systemd {job.scope.value} timer"
    return "cron"


def _next_unavailable(job: ScheduledJob) -> str:
    if job.backend is Backend.CRON and job.schedule.expression.lower() == "@reboot":
        return "unavailable (@reboot has no wall-clock next run)"
    return "unavailable"


def _next_run(job: ScheduledJob) -> datetime | None:
    if job.next_run is not None:
        return job.next_run
    if job.backend is Backend.CRON:
        return next_occurrence(job.schedule.expression)
    return None


def _previous_unavailable(job: ScheduledJob) -> str:
    if job.backend is Backend.CRON:
        return "unavailable (cron does not provide per-job history)"
    return "unavailable"
