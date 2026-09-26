from __future__ import annotations

import pytest

from schedls.cli import _parse_environment, _spec_for_new, build_parser, main, split_command
from schedls.errors import InvalidNameError, UsageError
from schedls.models import Backend, Scope


def test_split_command() -> None:
    head, tail = split_command(["new", "x", "--timer", "--", "/bin/true", "arg"])
    assert head == ["new", "x", "--timer"]
    assert tail == ["/bin/true", "arg"]
    head, tail = split_command(["show", "x"])
    assert head == ["show", "x"]
    assert tail == []


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
    for extra in (["--env", "A=1"], ["--working-directory", "/srv/data"], ["--persistent"], ["--jitter", "5min"]):
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
