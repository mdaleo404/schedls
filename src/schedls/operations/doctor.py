"""``schedls doctor`` — diagnostic capability report. Never fixes anything."""

from __future__ import annotations

import platform
from typing import Any

from .. import __version__
from ..backends.cron import CronBackend
from ..backends.systemd import SystemdBackend
from ..models import Scope
from ..output import Output
from ..runner import CommandRunner


def run_doctor(
    runner: CommandRunner,
    output: Output,
    systemd_backend: SystemdBackend,
    cron_backend: CronBackend,
) -> None:
    systemd = _systemd_report(runner, systemd_backend)
    cron = _cron_report(runner, cron_backend)
    usable = systemd["available"] or cron["crontab_available"] or cron["file_sources"]

    if output.json_mode:
        output.emit_json(
            {
                "schema_version": 1,
                "schedls": {"version": __version__, "python": platform.python_version()},
                "systemd": systemd,
                "cron": cron,
                "result": "usable" if usable else "unusable",
            }
        )
        return

    schedls_details = [("version", __version__), ("Python", platform.python_version())]
    systemd_details = [
        ("available", _yes_no(systemd["available"])),
        ("system manager", _reachable(systemd["system_manager"])),
        ("user manager", _reachable(systemd["user_manager"])),
        ("systemd-analyze", _yes_no(systemd["analyze"])),
        ("calendar validation", _yes_no(systemd["calendar_validation"])),
        ("user lingering", _lingering(systemd["user_lingering"])),
    ]
    cron_details = [
        ("crontab", _yes_no(cron["crontab_available"])),
        ("system cron files", _yes_no(cron["file_sources"])),
        ("implementation", cron["implementation"] or "unknown"),
        ("current user allowed", _yes_no(cron["user_allowed"])),
        ("syntax validation", _yes_no(cron["validation"])),
    ]
    width = max(len(key) for key, _ in [*schedls_details, *systemd_details, *cron_details])

    output.heading("schedls")
    output.key_values(schedls_details, width=width)
    output.line()
    output.heading("systemd")
    output.key_values(systemd_details, width=width)
    output.line()
    output.heading("cron")
    output.key_values(cron_details, width=width)
    output.line()
    output.heading("Result")
    output.line(f"  {'usable' if usable else 'unusable'}")


def _systemd_report(runner: CommandRunner, systemd_backend: SystemdBackend) -> dict[str, Any]:
    analyze = runner.has("systemd-analyze")
    user_manager = systemd_backend.manager_ok(Scope.USER)
    system_manager = systemd_backend.manager_ok(Scope.SYSTEM)
    available = runner.has("systemctl") and (user_manager or system_manager)
    return {
        "available": available,
        "system_manager": system_manager,
        "user_manager": user_manager,
        "analyze": analyze,
        "calendar_validation": analyze and available,
        "user_lingering": systemd_backend.user_lingering(),
    }


def _cron_report(runner: CommandRunner, cron_backend: CronBackend) -> dict[str, Any]:
    crontab_available = cron_backend.crontab_available()
    file_sources = cron_backend.file_sources_available()
    if not cron_backend.available():
        return {
            "crontab_available": False,
            "file_sources": file_sources,
            "implementation": None,
            "user_allowed": False,
            "validation": False,
        }
    info = cron_backend.capabilities_info()
    allowed = False
    if crontab_available:
        completed = runner.run(["crontab", "-l"], env_policy="identity", check=False)
        allowed = completed.returncode == 0 or "no crontab" in (completed.stderr + completed.stdout).lower()
    return {
        "crontab_available": crontab_available,
        "file_sources": file_sources,
        "implementation": info.implementation,
        "user_allowed": allowed,
        "validation": info.supports_validation,
    }


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _reachable(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "reachable" if value else "unreachable"


def _lingering(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "enabled" if value else "disabled"
