from __future__ import annotations

import json
import shlex
import subprocess
import sys

import pytest

from schedls.errors import InvalidScheduleError, SafetyRefusalError
from schedls.models import Command
from schedls.renderers import cron as renderer

NASTY_ARGS = [
    "plain",
    "a b",
    "a\tb",
    'a"b',
    "a'b",
    "a\\b",
    "a\\\\b",
    "$HOME",
    "$(touch /tmp/pwned)",
    "`touch /tmp/pwned`",
    "%",
    "100%",
    "a%b",
    "a\\%b",
    "a\\\\%b",
    "",
    " leading",
    "trailing ",
    "semi;colon",
    "pipe|cmd",
    "new&line",
    ">out",
    "unicode-\u00e9\u4e2d\U0001f600",
]


def cron_resolve(text: str) -> str:
    """Emulate Cronie's ``%`` processing of a command field."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] == "%":
            out.append("%")
            index += 2
            continue
        if char == "%":
            out.append("\n")
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def test_escape_percent() -> None:
    assert renderer.escape_percent("100%") == "100\\%"
    assert renderer.escape_percent("a\\%b") == "a\\\\%b"


def test_render_command_argv() -> None:
    rendered = renderer.render_command(Command(argv=("/usr/local/bin/backup", "/srv/My Data")))
    assert rendered == "/usr/local/bin/backup '/srv/My Data'"


def test_render_command_shell() -> None:
    rendered = renderer.render_command(Command(shell=True, raw="echo 50% > /tmp/x"))
    assert rendered == "echo 50\\% > /tmp/x"


def test_expression_percent_not_double_escaped() -> None:
    line = renderer.render_line("0 2 * * *", Command(argv=("/bin/echo", "a%b")))
    assert line == "0 2 * * * /bin/echo a\\%b"


@pytest.mark.parametrize(
    "bad",
    ["", "0 2 * *", "0 2 * * * *", "60 2 * * *", "0 25 * * *", "0 2 32 * *", "@bogus", "a b c d e"],
)
def test_invalid_expressions(bad: str) -> None:
    with pytest.raises(InvalidScheduleError):
        renderer.validate_expression(bad)


@pytest.mark.parametrize(
    "good",
    ["* * * * *", "0 2 * * *", "*/5 * * * *", "0 2 * * 1-5", "0 2 1,15 * 5", "@daily", "@reboot"],
)
def test_valid_expressions(good: str) -> None:
    assert renderer.validate_expression(good)


@pytest.mark.parametrize("arg", NASTY_ARGS)
def test_argv_round_trip_through_shell(tmp_path, arg: str) -> None:
    dump = tmp_path / "dump.py"
    dump.write_text("import json,sys\nopen(sys.argv[1],'w').write(json.dumps(sys.argv[2:]))\n")
    out = tmp_path / "out.json"
    command = Command(argv=(sys.executable, str(dump), str(out), arg))
    serialized = renderer.render_command(command)
    resolved = cron_resolve(serialized)
    subprocess.run(["/bin/sh", "-c", resolved], check=True)
    received = json.loads(out.read_text())
    assert received == [arg]


def test_no_command_injection(tmp_path) -> None:
    marker = tmp_path / "pwned"
    command = Command(argv=("/bin/true", f"x; touch {marker}", f"$(touch {marker})", f"`touch {marker}`"))
    resolved = cron_resolve(renderer.render_command(command))
    subprocess.run(["/bin/sh", "-c", resolved], check=True)
    assert not marker.exists()


def test_newline_rejected() -> None:
    with pytest.raises(SafetyRefusalError):
        renderer.render_command(Command(argv=("/bin/echo", "a\nb")))


def test_managed_block() -> None:
    block = renderer.render_block("backup", ["0 2 * * * /bin/true"])
    assert block == ("# schedls:begin name=backup\n0 2 * * * /bin/true\n# schedls:end name=backup\n")


def test_render_system_line() -> None:
    line = renderer.render_system_line("0 2 * * *", Command(argv=("/bin/echo", "100%")), "www-data")
    assert line == "0 2 * * * www-data /bin/echo 100\\%"


def test_render_system_file_has_marker() -> None:
    content = renderer.render_system_file("backup", ["0 2 * * * root /bin/true"])
    assert content.startswith("# Managed by schedls\n# schedls:begin name=backup\n")
    assert content.endswith("# schedls:end name=backup\n")


@pytest.mark.parametrize("name", ["backup", "my-job", "job_1"])
def test_system_name_accepts(name: str) -> None:
    assert renderer.validate_system_name(name) == name


@pytest.mark.parametrize("name", ["my.job", ".hidden", "-leading", "with space", ""])
def test_system_name_rejects(name: str) -> None:
    with pytest.raises(InvalidScheduleError):
        renderer.validate_system_name(name)


@pytest.mark.parametrize("user", ["root", "www-data", "nginx", "user.name", "_svc"])
def test_run_as_accepts(user: str) -> None:
    assert renderer.validate_run_as(user) == user


@pytest.mark.parametrize("user", ["", "-root", "a b", "a;b", "a$b", "x" * 33])
def test_run_as_rejects(user: str) -> None:
    with pytest.raises(InvalidScheduleError):
        renderer.validate_run_as(user)


def test_shlex_reference() -> None:
    assert shlex.quote("a b") == "'a b'"
