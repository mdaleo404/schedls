from __future__ import annotations

from pathlib import Path

import pytest

from schedls.errors import SafetyRefusalError
from schedls.models import Backend, Command, JobSpec, Scope
from schedls.renderers import systemd as renderer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "systemd"

NASTY_ARGS = [
    "plain",
    "a b",
    "a\tb",
    'a"b',
    "a'b",
    "a\\b",
    "a\\\\b",
    "$HOME",
    "$$",
    "$(touch /tmp/pwned)",
    "`touch /tmp/pwned`",
    "%n",
    "%%",
    "%i",
    "",
    " leading",
    "trailing ",
    "semi;colon",
    "pipe|cmd",
    "new&line",
    "unicode-\u00e9\u4e2d\U0001f600",
    "-dash",
    "@at",
    "!bang",
]


def test_quote_basic() -> None:
    assert renderer.quote_systemd_arg("plain") == '"plain"'
    assert renderer.quote_systemd_arg("a b") == '"a b"'
    assert renderer.quote_systemd_arg("") == '""'
    assert renderer.quote_systemd_arg("$HOME") == '"$$HOME"'
    assert renderer.quote_systemd_arg("%n") == '"%%n"'
    assert renderer.quote_systemd_arg('a"b') == '"a\\"b"'
    assert renderer.quote_systemd_arg("a\\b") == '"a\\\\b"'


@pytest.mark.parametrize("arg", NASTY_ARGS)
def test_argv_round_trip(arg: str) -> None:
    command = Command(argv=(arg,))
    rendered = renderer.render_exec_start(command)
    value = rendered.split("=", 1)[1]
    parsed = renderer.parse_exec_start(value)
    assert parsed.argv == (arg,)


def test_multi_argv_round_trip() -> None:
    command = Command(argv=tuple(NASTY_ARGS))
    value = renderer.render_exec_start(command).split("=", 1)[1]
    assert renderer.parse_exec_start(value).argv == tuple(NASTY_ARGS)


def test_shell_mode_round_trip() -> None:
    command = Command(shell=True, raw="generate-report | gzip > /srv/report.gz")
    value = renderer.render_exec_start(command).split("=", 1)[1]
    parsed = renderer.parse_exec_start(value)
    assert parsed.shell is True
    assert parsed.raw == "generate-report | gzip > /srv/report.gz"


def test_shell_mode_quotes_script() -> None:
    command = Command(shell=True, raw="echo $HOME > /tmp/x")
    rendered = renderer.render_exec_start(command)
    assert rendered.startswith('ExecStart=/bin/sh -c "')
    assert "$$HOME" in rendered


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\x00b"])
def test_control_characters_rejected(bad: str) -> None:
    with pytest.raises(SafetyRefusalError):
        renderer.render_exec_start(Command(argv=(bad,)))


def test_golden_service() -> None:
    spec = JobSpec(
        name="backup",
        backend=Backend.SYSTEMD,
        scope=Scope.USER,
        command=Command(argv=("/usr/local/bin/backup", "/srv/data")),
        calendar=("*-*-* 02:00:00",),
        persistent=True,
    )
    files = renderer.render_units(spec, service_unit="schedls-backup.service", timer_unit="schedls-backup.timer")
    assert files["schedls-backup.service"] == (FIXTURES / "basic.service").read_text()
    assert files["schedls-backup.timer"] == (FIXTURES / "basic.timer").read_text()


def test_timer_multiple_calendars() -> None:
    spec = JobSpec(
        name="multi",
        backend=Backend.SYSTEMD,
        scope=Scope.USER,
        command=Command(argv=("/bin/true",)),
        calendar=("Mon..Fri 02:00:00", "Sat,Sun 04:00:00"),
    )
    timer = renderer.render_timer(spec, "schedls-multi.timer", "schedls-multi.service")
    assert "OnCalendar=Mon..Fri 02:00:00" in timer
    assert "OnCalendar=Sat,Sun 04:00:00" in timer
