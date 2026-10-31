from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys

import pytest

from schedls.backends.cron import CronBackend
from schedls.backends.systemd import SystemdBackend
from schedls.cli import (
    Context,
    _cmd_set_enabled,
    _parse_environment,
    _spec_for_edit,
    _spec_for_new,
    build_parser,
    main,
    split_command,
)
from schedls.errors import InvalidNameError, UsageError
from schedls.interact import Interaction
from schedls.models import Backend, Command, CronDetails, JobSource, Schedule, ScheduledJob, ScheduleKind, Scope
from schedls.output import Output

from ..conftest import FakeRunner


def test_split_command() -> None:
    head, tail = split_command(["new", "x", "--timer", "--", "/bin/true", "arg"])
    assert head == ["new", "x", "--timer"]
    assert tail == ["/bin/true", "arg"]
    head, tail = split_command(["show", "x"])
    assert head == ["show", "x"]
    assert tail == []


def test_main_activates_argcomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr("schedls.cli.argcomplete.autocomplete", lambda parser, **kwargs: calls.append((parser, kwargs)))

    with pytest.raises(SystemExit):
        main(["--version"])

    assert len(calls) == 1
    assert calls[0][1]["default_completer"].__class__.__name__ == "SuppressCompleter"


def _complete(tmp_path, line: str) -> list[str]:
    completions = tmp_path / "completions"
    environment = {
        **os.environ,
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_STDOUT_FILENAME": str(completions),
        "COMP_LINE": line,
        "COMP_POINT": str(len(line)),
        "COMP_WORDBREAKS": " \t\n\"'><=;|&(:,",
        "PATH": "",
    }

    completed = subprocess.run(
        [sys.executable, "-m", "schedls"],
        check=False,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    return [item.strip() for item in completions.read_text().split("\v") if item.strip()]


def test_argcomplete_only_completes_static_parser_info(tmp_path) -> None:
    (tmp_path / "candidate").touch()

    assert "show" in _complete(tmp_path, "schedls sh")
    assert _complete(tmp_path, "schedls show c") == []


def test_edit_command_dest_does_not_shadow_subcommand() -> None:
    args = build_parser().parse_args(["edit", "backup", "--command"])
    assert args.command == "edit"
    assert args.replace_command is True


def test_spec_for_new_systemd() -> None:
    args = build_parser().parse_args(["new", "backup", "--timer", "--daily", "02:00", "--persistent"])
    spec = _spec_for_new(args, ["/usr/local/bin/backup", "/srv/data"])
    assert spec.backend is Backend.SYSTEMD
    assert spec.scope is Scope.USER
    assert spec.calendar == ("*-*-* 02:00:00",)
    assert spec.command.argv == ("/usr/local/bin/backup", "/srv/data")
    assert spec.persistent is True


def test_spec_for_new_cron() -> None:
    args = build_parser().parse_args(["new", "cleanup", "--cron", "--cron-expr", "0 4 * * 0"])
    spec = _spec_for_new(args, ["/usr/local/bin/cleanup"])
    assert spec.backend is Backend.CRON
    assert spec.cron_expression == "0 4 * * 0"


def test_spec_for_new_cron_accepts_environment() -> None:
    args = build_parser().parse_args(
        ["new", "cleanup", "--cron", "--cron-expr", "0 4 * * 0", "--env", "GREETING=hello world", "--env", "RATE=100%"]
    )

    spec = _spec_for_new(args, ["/usr/local/bin/cleanup"])

    assert spec.environment == (("GREETING", "hello world"), ("RATE", "100%"))


def test_spec_for_new_system_scope() -> None:
    args = build_parser().parse_args(["new", "x", "--timer", "--daily", "02:00", "--system"])
    spec = _spec_for_new(args, ["/bin/true"])
    assert spec.scope is Scope.SYSTEM


def test_spec_for_new_invalid_name() -> None:
    args = build_parser().parse_args(["new", "../evil", "--timer", "--daily", "02:00"])
    with pytest.raises(InvalidNameError):
        _spec_for_new(args, ["/bin/true"])


def test_spec_for_new_requires_schedule() -> None:
    args = build_parser().parse_args(["new", "x", "--timer"])
    with pytest.raises(UsageError):
        _spec_for_new(args, ["/bin/true"])


def test_new_without_backend_is_usage_error() -> None:
    assert main(["new", "x", "--daily", "02:00"]) == 2


def test_new_without_name_is_usage_error() -> None:
    assert main(["new", "--timer", "--daily", "02:00"]) == 2


def test_interactive_conflicts_with_json() -> None:
    assert main(["--json", "new", "x", "-i", "--timer", "--daily", "02:00"]) == 2


def test_interactive_requires_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", _FakeStdin(isatty=False))
    assert main(["new", "x", "-i", "--timer", "--daily", "02:00"]) == 3


class _FakeStdin:
    def __init__(self, *, isatty: bool) -> None:
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty


def test_spec_for_new_shell_mode() -> None:
    args = build_parser().parse_args(["new", "x", "--timer", "--daily", "02:00", "--shell", "echo hi | cat"])
    spec = _spec_for_new(args, [])
    assert spec.command.shell is True
    assert spec.command.raw == "echo hi | cat"


def test_spec_for_new_rejects_systemd_flags_for_cron() -> None:
    for extra in (["--working-directory", "/srv/data"], ["--persistent"], ["--jitter", "5min"]):
        args = build_parser().parse_args(["new", "x", "--cron", "--daily", "02:00", *extra])
        with pytest.raises(UsageError):
            _spec_for_new(args, ["/bin/true"])


def test_spec_for_new_rejects_cron_expr_for_timer() -> None:
    args = build_parser().parse_args(["new", "x", "--timer", "--cron-expr", "0 4 * * *"])
    with pytest.raises(UsageError):
        _spec_for_new(args, ["/bin/true"])


def test_spec_for_new_system_cron() -> None:
    args = build_parser().parse_args(["new", "x", "--cron", "--daily", "02:00", "--system"])
    spec = _spec_for_new(args, ["/bin/true"])
    assert spec.scope is Scope.SYSTEM
    assert spec.run_as == "root"


def test_spec_for_new_system_cron_run_as() -> None:
    args = build_parser().parse_args(["new", "x", "--cron", "--daily", "02:00", "--system", "--run-as", "www-data"])
    spec = _spec_for_new(args, ["/bin/true"])
    assert spec.run_as == "www-data"


def test_spec_for_new_rejects_run_as_for_user_cron() -> None:
    args = build_parser().parse_args(["new", "x", "--cron", "--daily", "02:00", "--run-as", "www-data"])
    with pytest.raises(UsageError):
        _spec_for_new(args, ["/bin/true"])


def test_spec_for_new_rejects_run_as_for_timer() -> None:
    args = build_parser().parse_args(["new", "x", "--timer", "--daily", "02:00", "--run-as", "www-data"])
    with pytest.raises(UsageError):
        _spec_for_new(args, ["/bin/true"])


def _cron_job(*, environment: tuple[tuple[str, str], ...] = ()) -> ScheduledJob:
    return ScheduledJob(
        name="cleanup",
        backend=Backend.CRON,
        scope=Scope.USER,
        managed=True,
        enabled=None,
        schedule=Schedule(ScheduleKind.CRON, "0 2 * * *"),
        command=Command(raw="/usr/local/bin/cleanup"),
        source=JobSource("current user's crontab", line=1),
        cron=CronDetails(expression="0 2 * * *", line=1, environment=environment),
    )


def test_spec_for_edit_cron_preserves_command_and_changes_schedule() -> None:
    args = build_parser().parse_args(["edit", "cleanup", "--daily", "03:00"])
    job = _cron_job(environment=(("OLD", "value"),))

    spec = _spec_for_edit(job, args, [])

    assert spec.backend is Backend.CRON
    assert spec.cron_expression == "0 3 * * *"
    assert spec.command == job.command
    assert spec.environment == (("OLD", "value"),)
    assert spec.replace_environment is False


def test_spec_for_edit_cron_replaces_command() -> None:
    args = build_parser().parse_args(["edit", "cleanup", "--command"])

    spec = _spec_for_edit(_cron_job(), args, ["/usr/local/bin/cleanup", "--full"])

    assert spec.command.argv == ("/usr/local/bin/cleanup", "--full")


def test_spec_for_edit_cron_replaces_environment() -> None:
    args = build_parser().parse_args(["edit", "cleanup", "--env", "NEW=value", "--env", "EMPTY="])

    spec = _spec_for_edit(_cron_job(environment=(("OLD", "value"),)), args, [])

    assert spec.environment == (("NEW", "value"), ("EMPTY", ""))
    assert spec.replace_environment is True


def test_spec_for_edit_cron_clears_environment() -> None:
    args = build_parser().parse_args(["edit", "cleanup", "--clear-env"])

    spec = _spec_for_edit(_cron_job(environment=(("OLD", "value"),)), args, [])

    assert spec.environment == ()
    assert spec.replace_environment is True


def test_edit_environment_options_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["edit", "cleanup", "--env", "NEW=value", "--clear-env"])


def test_disable_managed_cron_job_through_cli() -> None:
    state = {"text": ("# schedls:begin name=backup\n0 2 * * * /usr/bin/backup\n# schedls:end name=backup\n")}

    def crontab(argv, input_text):
        if argv == ["crontab", "-l"]:
            return state["text"], ""
        if argv == ["crontab", "-"]:
            state["text"] = input_text or ""
            return "", ""
        return 1, "", "unsupported"

    runner = FakeRunner(handlers={"crontab": crontab}, available={"crontab"})
    stream = io.StringIO()
    output = Output(color="never", stdout=stream)
    context = Context(
        runner=runner,
        output=output,
        interaction=Interaction(output=output, assume_yes=True),
        systemd=SystemdBackend(runner),
        cron=CronBackend(runner),
    )
    dry_run_args = argparse.Namespace(name="backup", dry_run=True, show_files=False)
    args = argparse.Namespace(name="backup", dry_run=False, show_files=False)

    assert _cmd_set_enabled(context, dry_run_args, False) == 0
    assert state["text"] == ("# schedls:begin name=backup\n0 2 * * * /usr/bin/backup\n# schedls:end name=backup\n")
    assert _cmd_set_enabled(context, args, False) == 0
    assert "# schedls:disabled 0 2 * * * /usr/bin/backup" in state["text"]
    assert "Disabled backup." in stream.getvalue()


@pytest.mark.parametrize("extra", [["--calendar", "daily"], ["--persistent"]])
def test_spec_for_edit_cron_rejects_systemd_options(extra: list[str]) -> None:
    args = build_parser().parse_args(["edit", "cleanup", *extra])

    with pytest.raises(UsageError):
        _spec_for_edit(_cron_job(), args, [])


@pytest.mark.parametrize("value", ["0", "-1", "1000001", "abc"])
def test_logs_lines_must_be_a_bounded_positive_integer(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["logs", "x", "--lines", value])


def test_parse_environment() -> None:
    assert _parse_environment(["A=1", "B=x=y"]) == (("A", "1"), ("B", "x=y"))
    with pytest.raises(UsageError):
        _parse_environment(["NOVALUE"])
    with pytest.raises(UsageError):
        _parse_environment(["1BAD=1"])
