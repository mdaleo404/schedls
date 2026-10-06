from __future__ import annotations

import io

import pytest

from schedls.cli import _spec_for_edit, _spec_for_new, _wizard_edit, _wizard_new, build_parser
from schedls.errors import ConfirmationRequiredError, InvalidScheduleError
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
from schedls.output import Output
from schedls.prompt import Prompter


class FakeInput:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, label: str) -> str:
        self.prompts.append(label)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class FakeStdin:
    def __init__(self, *, isatty: bool) -> None:
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty


def make_prompter(answers: list[str], *, calendar_validator=None) -> tuple[Prompter, FakeInput]:
    fake = FakeInput(answers)
    output = Output(stdout=io.StringIO(), stderr=io.StringIO())
    return Prompter(output=output, input_fn=fake, calendar_validator=calendar_validator), fake


def _systemd_job() -> ScheduledJob:
    return ScheduledJob(
        name="backup",
        backend=Backend.SYSTEMD,
        scope=Scope.USER,
        managed=True,
        enabled=True,
        schedule=Schedule(ScheduleKind.CALENDAR, "*-*-* 02:00:00"),
        command=Command(argv=("/usr/local/bin/backup",)),
        source=JobSource(detail="systemd user timer"),
        systemd=SystemdDetails(on_calendar=("*-*-* 02:00:00",)),
    )


def _cron_job() -> ScheduledJob:
    return ScheduledJob(
        name="cleanup",
        backend=Backend.CRON,
        scope=Scope.USER,
        managed=True,
        enabled=None,
        schedule=Schedule(ScheduleKind.CRON, "0 2 * * *"),
        command=Command(raw="/usr/local/bin/cleanup"),
        source=JobSource(detail="current user's crontab"),
        cron=CronDetails(expression="0 2 * * *"),
    )


def test_text_requires_a_value() -> None:
    prompter, fake = make_prompter(["", "backup"])
    assert prompter.text("Name") == "backup"
    assert len(fake.prompts) == 2


def test_text_uses_default_on_blank() -> None:
    prompter, _ = make_prompter([""])
    assert prompter.text("Name", default="backup") == "backup"


def test_optional_text() -> None:
    prompter, _ = make_prompter(["", ""])
    assert prompter.optional_text("Jitter") is None
    assert prompter.optional_text("Jitter", default="5min") == "5min"


def test_yes_no_rejects_unknown_answers() -> None:
    prompter, _ = make_prompter(["maybe", "yes"])
    assert prompter.yes_no("Continue?") is True


def test_choice_by_number_and_default() -> None:
    prompter, _ = make_prompter(["9", "2", ""])
    options = [("daily", "Daily"), ("weekly", "Weekly")]
    assert prompter.choice("Schedule:", options, default="daily") == "weekly"
    assert prompter.choice("Schedule:", options, default="daily") == "daily"


def test_command_builds_argv() -> None:
    prompter, _ = make_prompter(["n", "/usr/bin/echo hello world"])
    command = prompter.command()
    assert command.shell is False
    assert command.argv == ("/usr/bin/echo", "hello", "world")


def test_command_shell_mode() -> None:
    prompter, _ = make_prompter(["y", "echo hi | cat"])
    command = prompter.command()
    assert command.shell is True
    assert command.raw == "echo hi | cat"


def test_environment_collects_valid_entries() -> None:
    prompter, _ = make_prompter(["A=1", "1BAD=2", "B=x=y", ""])
    assert prompter.environment() == ["A=1", "B=x=y"]


def test_schedule_daily_reprompts_on_bad_time() -> None:
    prompter, _ = make_prompter(["", "25:00", "02:30"])
    assert prompter.schedule(Backend.SYSTEMD) == {"daily": "02:30"}


def test_schedule_weekly_and_monthly() -> None:
    prompter, _ = make_prompter(["3", "Mon", "02:00"])
    assert prompter.schedule(Backend.SYSTEMD) == {"weekly": ["Mon", "02:00"]}
    prompter, _ = make_prompter(["4", "15", "06:00"])
    assert prompter.schedule(Backend.SYSTEMD) == {"monthly": ["15", "06:00"]}


def test_schedule_custom_systemd_validates() -> None:
    def validator(expression: str) -> None:
        if expression != "daily":
            raise InvalidScheduleError("bad expression")

    prompter, _ = make_prompter(["5", "bogus", "daily"], calendar_validator=validator)
    assert prompter.schedule(Backend.SYSTEMD) == {"calendar": ["daily"]}


def test_schedule_custom_cron() -> None:
    prompter, _ = make_prompter(["5", "0 4 * * *"])
    assert prompter.schedule(Backend.CRON) == {"cron_expr": "0 4 * * *"}


def test_require_terminal_rejects_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=False))
    prompter, _ = make_prompter([])
    with pytest.raises(ConfirmationRequiredError):
        prompter.require_terminal()


def test_eof_raises_confirmation_required() -> None:
    prompter, _ = make_prompter([])
    with pytest.raises(ConfirmationRequiredError):
        prompter.text("Name")


def test_wizard_new_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    args = build_parser().parse_args(["new", "-i"])
    answers = ["backup", "", "", "n", "/usr/local/bin/backup /srv/data", "", "02:00", "n"]
    prompter, _ = make_prompter(answers)
    args, tail = _wizard_new(args, [], prompter)
    spec = _spec_for_new(args, tail)
    assert spec.name == "backup"
    assert spec.backend is Backend.SYSTEMD
    assert spec.scope is Scope.USER
    assert spec.calendar == ("*-*-* 02:00:00",)
    assert spec.command.argv == ("/usr/local/bin/backup", "/srv/data")


def test_wizard_new_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    args = build_parser().parse_args(["new", "-i"])
    answers = ["cleanup", "2", "", "n", "/usr/local/bin/cleanup", "", "04:00"]
    prompter, _ = make_prompter(answers)
    args, tail = _wizard_new(args, [], prompter)
    spec = _spec_for_new(args, tail)
    assert spec.backend is Backend.CRON
    assert spec.scope is Scope.USER
    assert spec.cron_expression == "0 4 * * *"
    assert spec.run_as is None


def test_wizard_new_skips_provided_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    args = build_parser().parse_args(["new", "backup", "-i", "--timer", "--daily", "02:00", "--user"])
    prompter, fake = make_prompter(["n"])
    args, tail = _wizard_new(args, ["/bin/true"], prompter)
    spec = _spec_for_new(args, tail)
    assert spec.calendar == ("*-*-* 02:00:00",)
    assert fake.prompts == ["Set advanced timer options? [y/N] "]


def test_wizard_new_scope_prompt_defaults_to_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    args = build_parser().parse_args(["new", "cleanup", "-i", "--cron", "--daily", "04:00"])
    prompter, fake = make_prompter(["", "n", "/usr/local/bin/cleanup"])
    args, tail = _wizard_new(args, [], prompter)
    spec = _spec_for_new(args, tail)
    assert spec.scope is Scope.USER
    assert spec.run_as is None
    assert any(prompt.startswith("Scope:") for prompt in fake.prompts)
    assert not any(prompt.startswith("Run as user") for prompt in fake.prompts)


def test_wizard_new_system_cron_prompts_run_as(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    args = build_parser().parse_args(["new", "report", "-i", "--cron", "--daily", "04:00"])
    prompter, _ = make_prompter(["2", "www-data", "n", "/usr/local/bin/report"])
    args, tail = _wizard_new(args, [], prompter)
    spec = _spec_for_new(args, tail)
    assert spec.scope is Scope.SYSTEM
    assert spec.run_as == "www-data"


def test_wizard_new_system_cron_run_as_defaults_to_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    args = build_parser().parse_args(["new", "report", "-i", "--cron", "--daily", "04:00"])
    prompter, _ = make_prompter(["2", "", "n", "/usr/local/bin/report"])
    args, tail = _wizard_new(args, [], prompter)
    spec = _spec_for_new(args, tail)
    assert spec.scope is Scope.SYSTEM
    assert spec.run_as == "root"


def test_wizard_new_system_cron_run_as_reprompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    args = build_parser().parse_args(["new", "report", "-i", "--cron", "--daily", "04:00"])
    prompter, fake = make_prompter(["2", "bad user", "www-data", "n", "/usr/local/bin/report"])
    args, tail = _wizard_new(args, [], prompter)
    spec = _spec_for_new(args, tail)
    assert spec.run_as == "www-data"
    assert fake.prompts.count("Run as user [root]: ") == 2


def test_wizard_new_provided_scope_and_run_as_are_not_reasked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    args = build_parser().parse_args(
        ["new", "report", "-i", "--cron", "--system", "--run-as", "root", "--daily", "04:00"]
    )
    prompter, fake = make_prompter(["n", "/usr/local/bin/report"])
    args, tail = _wizard_new(args, [], prompter)
    spec = _spec_for_new(args, tail)
    assert spec.scope is Scope.SYSTEM
    assert spec.run_as == "root"
    assert all("Scope:" not in prompt and "Run as user" not in prompt for prompt in fake.prompts)


def test_wizard_new_systemd_scope_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    args = build_parser().parse_args(["new", "backup", "-i", "--timer", "--daily", "02:00"])
    prompter, _ = make_prompter(["2", "n", "/usr/local/bin/backup", "n"])
    args, tail = _wizard_new(args, [], prompter)
    spec = _spec_for_new(args, tail)
    assert spec.scope is Scope.SYSTEM
    assert spec.calendar == ("*-*-* 02:00:00",)


def test_wizard_edit_changes_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    job = _systemd_job()
    args = build_parser().parse_args(["edit", "backup", "-i"])
    answers = ["n", "y", "", "03:00", "n"]
    prompter, _ = make_prompter(answers)
    args, tail = _wizard_edit(job, args, [], prompter)
    spec = _spec_for_edit(job, args, tail)
    assert spec.calendar == ("*-*-* 03:00:00",)


def test_wizard_edit_changes_cron_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    args = build_parser().parse_args(["edit", "cleanup", "-i"])
    prompter, _ = make_prompter(["n", "y", "", "03:00"])
    args, tail = _wizard_edit(_cron_job(), args, [], prompter)
    spec = _spec_for_edit(_cron_job(), args, tail)

    assert spec.cron_expression == "0 3 * * *"


def test_wizard_edit_keeps_changes_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(isatty=True))
    job = _systemd_job()
    args = build_parser().parse_args(["edit", "backup", "-i"])
    prompter, _ = make_prompter(["n", "n", "n"])
    args, tail = _wizard_edit(job, args, [], prompter)
    spec = _spec_for_edit(job, args, tail)
    assert spec.calendar == ("*-*-* 02:00:00",)
    assert tail == []
