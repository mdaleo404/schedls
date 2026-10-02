from __future__ import annotations

import io
import json
from datetime import UTC, datetime

from schedls.models import (
    Backend,
    Command,
    CronDetails,
    JobSource,
    Schedule,
    ScheduledJob,
    ScheduleKind,
    Scope,
    SystemdDetails,
)
from schedls.output import Output, job_to_dict, jobs_document, sanitize_text


class _TTYStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def _systemd_job() -> ScheduledJob:
    return ScheduledJob(
        name="backup",
        backend=Backend.SYSTEMD,
        scope=Scope.USER,
        managed=True,
        enabled=True,
        schedule=Schedule(ScheduleKind.CALENDAR, "*-*-* 02:00:00", ("*-*-* 02:00:00",)),
        command=Command(argv=("/usr/local/bin/backup", "/srv/data")),
        source=JobSource("systemd user timer", path="/home/u/.config/systemd/user/schedls-backup.timer"),
        next_run=datetime(2026, 9, 25, 2, 0, tzinfo=UTC),
        systemd=SystemdDetails(
            timer_unit="schedls-backup.timer",
            service_unit="schedls-backup.service",
            persistent=True,
        ),
    )


def _cron_job() -> ScheduledJob:
    return ScheduledJob(
        name="cron-3",
        backend=Backend.CRON,
        scope=Scope.USER,
        managed=False,
        enabled=None,
        schedule=Schedule(ScheduleKind.CRON, "0 4 * * 0"),
        command=Command(raw="/usr/local/bin/cleanup"),
        source=JobSource("current user's crontab", line=3),
        cron=CronDetails(expression="0 4 * * 0", line=3),
    )


def test_job_to_dict_systemd() -> None:
    data = job_to_dict(_systemd_job())
    assert data["name"] == "backup"
    assert data["backend"] == "systemd"
    assert data["managed"] is True
    assert data["next_run"].endswith("+00:00")
    assert data["systemd"]["persistent"] is True


def test_job_to_dict_cron_missing_data_is_null() -> None:
    data = job_to_dict(_cron_job())
    assert data["next_run"] is None
    assert data["last_run"] is None
    assert data["enabled"] is None


def test_jobs_document_schema_and_no_ansi() -> None:
    document = jobs_document([_systemd_job(), _cron_job()], ["a warning"])
    assert document["schema_version"] == 1
    assert len(document["jobs"]) == 2
    dumped = json.dumps(document)
    assert "\x1b" not in dumped
    assert "backup" in dumped


def test_output_table_and_key_values() -> None:
    stream = io.StringIO()
    output = Output(json_mode=False, color="never", stdout=stream)
    output.table(["NAME", "BACKEND"], [["backup", "systemd"], ["cleanup", "cron"]])
    output.key_values([("Status", "active")])
    text = stream.getvalue()
    assert "NAME" in text
    assert "backup" in text
    assert "\x1b" not in text


def test_key_values_colour_labels_but_not_values() -> None:
    stream = io.StringIO()
    output = Output(color="always", stdout=stream)

    output.key_values([("S", "active"), ("Status", "waiting")], width=8)

    assert stream.getvalue() == ("\033[1m\033[36mS       \033[0m  active\n\033[1m\033[36mStatus  \033[0m  waiting\n")


def test_key_values_honours_requested_width() -> None:
    stream = io.StringIO()
    output = Output(color="never", stdout=stream)

    output.key_values([("Key", "value")], width=6)

    assert stream.getvalue() == "Key     value\n"


def test_key_values_honours_no_color(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    stream = _TTYStream()
    output = Output(color="auto", stdout=stream)

    output.key_values([("Status", "active")])

    assert "\x1b" not in stream.getvalue()


def test_output_json_mode() -> None:
    stream = io.StringIO()
    output = Output(json_mode=True, color="never", stdout=stream)
    output.emit_json({"schema_version": 1, "jobs": []})
    assert json.loads(stream.getvalue())["schema_version"] == 1


def test_sanitize_text_escapes_control_characters() -> None:
    text = "a\x1b[31mred\x1b[0m\nb\tc\x00d\x7f"
    assert sanitize_text(text) == "a\\x1b[31mred\\x1b[0m\nb\tc\\x00d\\x7f"


def test_output_write_is_verbatim() -> None:
    stream = io.StringIO()
    output = Output(color="never", stdout=stream)
    output.write("line one\nline two")
    assert stream.getvalue() == "line one\nline two"
