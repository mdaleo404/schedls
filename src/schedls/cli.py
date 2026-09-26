"""Command-line interface for schedls."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

from . import __version__, convenience
from .backends.base import SchedulerBackend
from .backends.cron import CronBackend
from .backends.systemd import SystemdBackend
from .errors import EXIT_FAILURE, EXIT_SUCCESS, NotFoundError, SchedlsError, UsageError
from .interact import Interaction
from .models import Backend, Command, JobSpec, ScheduledJob, Scope
from .operations import calendar as calendar_ops
from .operations import doctor as doctor_ops
from .operations import inspect as inspect_ops
from .operations import mutate as mutate_ops
from .output import Output, sanitize_text
from .prompt import Prompter
from .renderers import cron as cron_renderer
from .runner import CommandRunner
from .security import validate_name
from .timefmt import format_datetime

_ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_LOG_LINES = 1_000_000


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer") from None
    if number < 1 or number > _MAX_LOG_LINES:
        raise argparse.ArgumentTypeError(f"must be between 1 and {_MAX_LOG_LINES}")
    return number


@dataclass
class Context:
    runner: CommandRunner
    output: Output
    interaction: Interaction
    systemd: SystemdBackend
    cron: CronBackend

    @property
    def backends(self) -> list[SchedulerBackend]:
        return [self.systemd, self.cron]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schedls",
        description="Inspect and manage Linux scheduled jobs.",
    )
    parser.add_argument("--version", action="version", version=f"schedls {__version__}")
    parser.add_argument("--debug", action="store_true", help="enable debug output on stderr")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="emit JSON on stdout")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize human output (default: auto)",
    )
    parser.add_argument("--utc", action="store_true", help="display times in UTC")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--user", dest="scope_filter", action="store_const", const=Scope.USER, help="only user scope")
    scope.add_argument(
        "--system", dest="scope_filter", action="store_const", const=Scope.SYSTEM, help="only system scope"
    )
    parser.add_argument(
        "--backend",
        choices=("systemd", "cron"),
        dest="backend_filter",
        help="only show jobs from this backend",
    )
    managed = parser.add_mutually_exclusive_group()
    managed.add_argument(
        "--managed", dest="managed_filter", action="store_const", const=True, help="only show jobs created by schedls"
    )
    managed.add_argument(
        "--unmanaged",
        dest="managed_filter",
        action="store_const",
        const=False,
        help="only show jobs schedls does not manage",
    )
    enabled = parser.add_mutually_exclusive_group()
    enabled.add_argument(
        "--enabled", dest="enabled_filter", action="store_const", const=True, help="only show enabled jobs"
    )
    enabled.add_argument(
        "--disabled", dest="enabled_filter", action="store_const", const=False, help="only show disabled jobs"
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="list visible scheduled jobs")

    show = subparsers.add_parser("show", help="show one scheduled job")
    show.add_argument("name", help="job name")

    new = subparsers.add_parser("new", help="create a scheduled job")
    new.add_argument("name", nargs="?", help="job name (prompted for with --interactive)")
    backend = new.add_mutually_exclusive_group()
    backend.add_argument("--timer", action="store_true", help="create a systemd timer")
    backend.add_argument("--cron", action="store_true", help="create a cron job")
    _add_creation_options(new)
    _add_interactive_flag(new)

    edit = subparsers.add_parser("edit", help="modify a schedls-managed job")
    edit.add_argument("name", help="job name")
    _add_edit_options(edit)
    _add_interactive_flag(edit)

    rm = subparsers.add_parser("rm", help="remove a schedls-managed job")
    rm.add_argument("name", help="job name")
    _add_mutation_flags(rm)

    for action, help_text in (("enable", "enable a job"), ("disable", "disable a job")):
        command = subparsers.add_parser(action, help=help_text)
        command.add_argument("name", help="job name")
        _add_mutation_flags(command)

    logs = subparsers.add_parser("logs", help="show available execution logs")
    logs.add_argument("name", help="job name")
    logs.add_argument(
        "--lines",
        type=_positive_int,
        default=50,
        help="number of recent journal lines to show (default: 50)",
    )
    logs.add_argument("--since", help="only show entries since this time (systemd timers only)")

    calendar = subparsers.add_parser("calendar", help="validate a systemd calendar expression")
    calendar.add_argument("expression", help="OnCalendar expression to validate, e.g. 'Mon..Fri 02:30'")
    calendar.add_argument(
        "--next", type=int, default=5, dest="next_count", help="how many upcoming occurrences to show (default: 5)"
    )

    subparsers.add_parser("doctor", help="inspect scheduler capabilities")

    return parser


def _add_schedule_options(parser: argparse.ArgumentParser, *, creation: bool) -> None:
    parser.add_argument("--calendar", action="append", metavar="EXPR", help="native OnCalendar expression (repeatable)")
    parser.add_argument("--daily", metavar="TIME", help="daily at TIME (HH:MM)")
    parser.add_argument("--weekdays", metavar="TIME", help="Mon..Fri at TIME")
    parser.add_argument("--weekly", nargs=2, metavar=("DAY", "TIME"), help="weekly on DAY at TIME")
    parser.add_argument("--monthly", nargs=2, metavar=("DAY", "TIME"), help="monthly on DAY at TIME")
    parser.add_argument("--cron-expr", metavar="EXPR", help="five-field cron expression")
    parser.add_argument("--persistent", action="store_true", help="run missed events (systemd timers)")
    parser.add_argument("--jitter", metavar="DURATION", help="randomized delay, e.g. 5min (systemd timers)")
    parser.add_argument("--accuracy", metavar="DURATION", help="timer accuracy window, e.g. 1min (systemd timers)")
    parser.add_argument("--working-directory", metavar="PATH", help="run the command from PATH (systemd timers)")
    parser.add_argument("--env", action="append", metavar="KEY=VALUE", help="set an environment variable (repeatable)")
    parser.add_argument("--shell", metavar="SCRIPT", help="run SCRIPT through /bin/sh instead of an argv")
    if creation:
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument(
            "--user", dest="scope_group", action="store_const", const=Scope.USER, help="user scope (default)"
        )
        scope.add_argument(
            "--system", dest="scope_group", action="store_const", const=Scope.SYSTEM, help="system scope (root)"
        )


def _add_creation_options(parser: argparse.ArgumentParser) -> None:
    _add_schedule_options(parser, creation=True)
    parser.add_argument(
        "--run-as",
        metavar="USER",
        help="run a system cron job as USER (default: root)",
    )
    _add_mutation_flags(parser)


def _add_edit_options(parser: argparse.ArgumentParser) -> None:
    _add_schedule_options(parser, creation=False)
    parser.add_argument(
        "--command",
        action="store_true",
        dest="replace_command",
        help="replace the command using the text after '--'",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--no-persistent", action="store_true", help="disable Persistent")
    _add_mutation_flags(parser)


def _add_mutation_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="show what would change without doing it")
    parser.add_argument("--yes", action="store_true", help="assume yes; do not prompt")
    parser.add_argument("--show-files", action="store_true", help="include rendered files in previews")


def _add_interactive_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="fill missing fields with guided prompts (requires a terminal)",
    )


def split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1 :]
    return argv, []


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    head, tail = split_command(arguments)
    parser = build_parser()
    args = parser.parse_args(head)

    output = Output(json_mode=args.json_mode, color=args.color, utc=args.utc)
    runner = CommandRunner(debug=args.debug)
    interaction = Interaction(output=output, assume_yes=getattr(args, "yes", False))
    context = Context(
        runner=runner,
        output=output,
        interaction=interaction,
        systemd=SystemdBackend(runner),
        cron=CronBackend(runner),
    )

    try:
        return _dispatch(context, args, tail)
    except SchedlsError as exc:
        output.diagnostic(exc.render())
        return exc.exit_code
    except KeyboardInterrupt:
        output.diagnostic("Interrupted.")
        return EXIT_FAILURE
    except BrokenPipeError:
        return EXIT_FAILURE


def _dispatch(context: Context, args: argparse.Namespace, tail: list[str]) -> int:
    command = args.command or "list"
    if command == "list":
        return _cmd_list(context, args)
    if command == "show":
        return _cmd_show(context, args)
    if command == "new":
        return _cmd_new(context, args, tail)
    if command == "edit":
        return _cmd_edit(context, args, tail)
    if command == "rm":
        return _cmd_rm(context, args)
    if command in {"enable", "disable"}:
        return _cmd_set_enabled(context, args, command == "enable")
    if command == "logs":
        return _cmd_logs(context, args)
    if command == "calendar":
        return _cmd_calendar(context, args)
    if command == "doctor":
        return _cmd_doctor(context, args)
    raise UsageError(f"unknown command: {command}")


def _cmd_list(context: Context, args: argparse.Namespace) -> int:
    scopes = [args.scope_filter] if args.scope_filter else [Scope.USER, Scope.SYSTEM]
    jobs, warnings = inspect_ops.collect(context.backends, scopes)
    backend_filter = Backend(args.backend_filter) if args.backend_filter else None
    jobs = inspect_ops.filter_jobs(
        jobs,
        scope=args.scope_filter,
        backend=backend_filter,
        managed=args.managed_filter,
        enabled=args.enabled_filter,
    )
    inspect_ops.render_list(context.output, jobs, warnings)
    return EXIT_SUCCESS


def _cmd_show(context: Context, args: argparse.Namespace) -> int:
    _, job = _find_job(context, args.name)
    inspect_ops.render_show(context.output, job)
    return EXIT_SUCCESS


def _cmd_new(context: Context, args: argparse.Namespace, tail: list[str]) -> int:
    if args.interactive:
        _require_interactive(context)
        args, tail = _wizard_new(args, tail, _make_prompter(context))
    elif args.name is None:
        raise UsageError("no schedule name given.", hint="Pass a name, e.g. 'schedls new backup ...'.")
    elif not (args.timer or args.cron):
        raise UsageError("choose a backend.", hint="Pass --timer or --cron.")
    spec = _spec_for_new(args, tail)
    backend = context.systemd if spec.backend is Backend.SYSTEMD else context.cron
    plan = backend.plan_create(spec)
    result = mutate_ops.run_plan(
        backend,
        plan,
        context.output,
        context.interaction,
        dry_run=args.dry_run,
        show_files=args.show_files,
    )
    if result.changed and not result.dry_run:
        _report_created(context, spec)
    return EXIT_SUCCESS


def _cmd_edit(context: Context, args: argparse.Namespace, tail: list[str]) -> int:
    backend, job = _find_job(context, args.name)
    if not backend.capabilities.update:
        raise UsageError(f"editing {job.backend.value} jobs is not supported yet.")
    if args.interactive:
        _require_interactive(context)
        args, tail = _wizard_edit(job, args, tail, _make_prompter(context))
    spec = _spec_for_edit(job, args, tail)
    plan = backend.plan_update(job, spec)
    result = mutate_ops.run_plan(
        backend,
        plan,
        context.output,
        context.interaction,
        dry_run=args.dry_run,
        show_files=args.show_files,
    )
    if result.changed and not result.dry_run and not context.output.json_mode:
        context.output.line(f"Updated {job.name}.")
    return EXIT_SUCCESS


def _cmd_rm(context: Context, args: argparse.Namespace) -> int:
    backend, job = _find_job(context, args.name)
    plan = backend.plan_remove(job)
    result = mutate_ops.run_plan(
        backend,
        plan,
        context.output,
        context.interaction,
        dry_run=args.dry_run,
        show_files=args.show_files,
    )
    if result.changed and not result.dry_run and not context.output.json_mode:
        context.output.line(f"Removed {job.name}.")
    return EXIT_SUCCESS


def _cmd_set_enabled(context: Context, args: argparse.Namespace, enabled: bool) -> int:
    backend, job = _find_job(context, args.name)
    if not (backend.capabilities.enable if enabled else backend.capabilities.disable):
        raise UsageError(
            f"{job.backend.value} does not support enable/disable.",
            hint="Cron has no universal native enabled/disabled concept.",
        )
    plan = backend.plan_set_enabled(job, enabled)
    result = mutate_ops.run_plan(
        backend,
        plan,
        context.output,
        context.interaction,
        dry_run=args.dry_run,
        show_files=args.show_files,
    )
    if result.changed and not result.dry_run and not context.output.json_mode:
        state = "Enabled" if enabled else "Disabled"
        context.output.line(f"{state} {job.name}.")
    return EXIT_SUCCESS


def _cmd_logs(context: Context, args: argparse.Namespace) -> int:
    backend, job = _find_job(context, args.name)
    if not backend.capabilities.logs:
        message = (
            "Per-job logs are not available through the cron backend.\n\n"
            "Cron output may be delivered by mail, redirected by the command,\n"
            "or written to system logs depending on the local cron implementation."
        )
        if context.output.json_mode:
            context.output.emit_json(
                {
                    "schema_version": 1,
                    "name": job.name,
                    "backend": job.backend.value,
                    "content": None,
                    "message": message,
                }
            )
        else:
            context.output.line(message)
        return EXIT_SUCCESS
    text = backend.logs(job, lines=args.lines, since=args.since)
    if context.output.json_mode:
        context.output.emit_json(
            {
                "schema_version": 1,
                "name": job.name,
                "backend": job.backend.value,
                "unit": job.systemd.service_unit if job.systemd else None,
                "content": text,
            }
        )
        return EXIT_SUCCESS
    if context.output.stdout.isatty():
        text = sanitize_text(text)
    context.output.write(text)
    return EXIT_SUCCESS


def _cmd_calendar(context: Context, args: argparse.Namespace) -> int:
    calendar_ops.run_calendar(context.runner, context.output, args.expression, next_count=args.next_count)
    return EXIT_SUCCESS


def _cmd_doctor(context: Context, args: argparse.Namespace) -> int:
    doctor_ops.run_doctor(context.runner, context.output, context.systemd, context.cron)
    return EXIT_SUCCESS


def _find_job(context: Context, name: str) -> tuple[SchedulerBackend, ScheduledJob]:
    for backend in context.backends:
        if not backend.available():
            continue
        job = backend.find(name)
        if job is not None:
            return backend, job
    raise NotFoundError(
        f"schedule {name!r} not found.",
        hint="Run 'schedls' to list visible schedules.",
    )


def _resolve_scope(args: argparse.Namespace) -> Scope:
    chosen = getattr(args, "scope_group", None) or getattr(args, "scope_filter", None)
    return chosen or Scope.USER


def _resolve_command(args: argparse.Namespace, tail: list[str], *, required: bool) -> Command:
    if getattr(args, "shell", None):
        if tail:
            raise UsageError("cannot combine --shell with a command after '--'.")
        return Command(shell=True, raw=args.shell)
    if not tail and required:
        raise UsageError("no command given.", hint="Pass the command after '--'.")
    return Command(argv=tuple(tail))


def _parse_environment(entries: list[str] | None) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for entry in entries or []:
        key, sep, value = entry.partition("=")
        if not sep or not _ENV_NAME_RE.match(key):
            raise UsageError(f"invalid environment entry: {entry!r}", hint="Expected KEY=VALUE.")
        result.append((key, value))
    return tuple(result)


def _calendar_from_args(args: argparse.Namespace) -> list[str]:
    convenience_exprs: list[str] = []
    if args.daily:
        convenience_exprs.append(convenience.daily_calendar(args.daily))
    if args.weekdays:
        convenience_exprs.append(convenience.weekdays_calendar(args.weekdays))
    if args.weekly:
        convenience_exprs.append(convenience.weekly_calendar(*args.weekly))
    if args.monthly:
        convenience_exprs.append(convenience.monthly_calendar(*args.monthly))
    if args.calendar and convenience_exprs:
        raise UsageError("combine --calendar with convenience flags is not allowed.")
    return list(args.calendar or []) + convenience_exprs


def _cron_expression_from_args(args: argparse.Namespace) -> str | None:
    convenience_exprs: list[str] = []
    if args.daily:
        convenience_exprs.append(convenience.daily_cron(args.daily))
    if args.weekdays:
        convenience_exprs.append(convenience.weekdays_cron(args.weekdays))
    if args.weekly:
        convenience_exprs.append(convenience.weekly_cron(*args.weekly))
    if args.monthly:
        convenience_exprs.append(convenience.monthly_cron(*args.monthly))
    if args.cron_expr and convenience_exprs:
        raise UsageError("combine --cron-expr with convenience flags is not allowed.")
    if len(convenience_exprs) > 1:
        raise UsageError("cron supports a single schedule; specify one convenience flag.")
    if args.cron_expr:
        return cron_renderer.validate_expression(args.cron_expr)
    return convenience_exprs[0] if convenience_exprs else None


def _spec_for_new(args: argparse.Namespace, tail: list[str]) -> JobSpec:
    validate_name(args.name)
    scope = _resolve_scope(args)
    command = _resolve_command(args, tail, required=True)
    environment = _parse_environment(args.env)
    if args.timer:
        if args.cron_expr:
            raise UsageError("--cron-expr is a cron option; use --calendar for systemd timers.")
        if args.run_as:
            raise UsageError("--run-as is a system cron option.")
        calendar = _calendar_from_args(args)
        if not calendar:
            raise UsageError("no schedule given.", hint="Use --calendar or a convenience flag such as --daily.")
        return JobSpec(
            name=args.name,
            backend=Backend.SYSTEMD,
            scope=scope,
            command=command,
            calendar=tuple(calendar),
            persistent=args.persistent,
            jitter=args.jitter,
            accuracy=args.accuracy,
            working_directory=args.working_directory,
            environment=environment,
        )
    if args.calendar:
        raise UsageError("--calendar is a systemd option; use --cron-expr for cron.")
    if args.jitter or args.accuracy or args.persistent or args.working_directory:
        raise UsageError("--jitter/--accuracy/--persistent/--working-directory are systemd options.")
    if args.env:
        raise UsageError("--env is a systemd option; cron environment variables are not supported yet.")
    expression = _cron_expression_from_args(args)
    if not expression:
        raise UsageError("no schedule given.", hint="Use --cron-expr or a convenience flag such as --daily.")
    if scope is Scope.SYSTEM:
        return JobSpec(
            name=args.name,
            backend=Backend.CRON,
            scope=Scope.SYSTEM,
            command=command,
            cron_expression=expression,
            run_as=args.run_as or "root",
        )
    if args.run_as:
        raise UsageError("--run-as requires --system for cron jobs.", hint="Cron jobs run as the current user.")
    return JobSpec(
        name=args.name,
        backend=Backend.CRON,
        scope=Scope.USER,
        command=command,
        cron_expression=expression,
    )


def _spec_for_edit(job: ScheduledJob, args: argparse.Namespace, tail: list[str]) -> JobSpec:
    if tail and not args.replace_command:
        raise UsageError("to replace the command use --command before '--'.")
    command = job.command
    if args.replace_command:
        command = _resolve_command(args, tail, required=True)
    elif args.shell:
        command = Command(shell=True, raw=args.shell)

    if job.backend is Backend.SYSTEMD:
        calendar = _calendar_from_args(args)
        if args.cron_expr:
            raise UsageError("--cron-expr cannot be used for a systemd timer.")
        if not calendar:
            calendar = list(job.systemd.on_calendar if job.systemd else [])
        persistent = args.persistent or (job.systemd.persistent if job.systemd else False)
        if args.no_persistent:
            persistent = False
        return JobSpec(
            name=job.name,
            backend=Backend.SYSTEMD,
            scope=job.scope,
            command=command,
            calendar=tuple(calendar),
            persistent=persistent,
            jitter=args.jitter if args.jitter is not None else (job.systemd.jitter if job.systemd else None),
            accuracy=args.accuracy if args.accuracy is not None else (job.systemd.accuracy if job.systemd else None),
            working_directory=args.working_directory
            if args.working_directory is not None
            else (job.systemd.working_directory if job.systemd else None),
            environment=_parse_environment(args.env) if args.env else (job.systemd.environment if job.systemd else ()),
        )
    raise UsageError(f"editing {job.backend.value} jobs is not supported yet.")


_SCHEDULE_DESTS = ("daily", "weekdays", "weekly", "monthly", "calendar", "cron_expr")


def _require_interactive(context: Context) -> None:
    if context.output.json_mode:
        raise UsageError("--interactive cannot be combined with --json.")


def _make_prompter(context: Context) -> Prompter:
    return Prompter(output=context.output, calendar_validator=context.systemd.validate_calendar)


def _has_schedule(args: argparse.Namespace) -> bool:
    return any(getattr(args, dest, None) is not None for dest in _SCHEDULE_DESTS)


def _apply_schedule(args: argparse.Namespace, fragment: dict[str, object]) -> None:
    for dest in _SCHEDULE_DESTS:
        setattr(args, dest, fragment.get(dest))


def _wizard_backend(args: argparse.Namespace, prompter: Prompter) -> Backend:
    if args.timer:
        return Backend.SYSTEMD
    if args.cron:
        return Backend.CRON
    selected = prompter.choice(
        "Backend:",
        [("timer", "systemd timer"), ("cron", "cron job")],
        default="timer",
    )
    if selected == "timer":
        args.timer = True
        return Backend.SYSTEMD
    args.cron = True
    return Backend.CRON


def _wizard_scope(args: argparse.Namespace, backend: Backend, prompter: Prompter) -> Scope:
    provided = getattr(args, "scope_group", None) or getattr(args, "scope_filter", None)
    if provided is not None:
        return Scope(provided)
    if backend is Backend.SYSTEMD:
        options = [("user", "user timer"), ("system", "system timer (root)")]
    else:
        options = [
            ("user", "current user's crontab"),
            ("system", "system drop-in /etc/cron.d (root)"),
        ]
    selected = prompter.choice("Scope:", options, default="user")
    args.scope_group = Scope.SYSTEM if selected == "system" else Scope.USER
    return Scope(args.scope_group)


def _wizard_command(args: argparse.Namespace, tail: list[str], prompter: Prompter) -> list[str]:
    if args.shell or tail:
        return tail
    command = prompter.command()
    if command.shell:
        args.shell = command.raw
        return []
    return list(command.argv)


def _wizard_new(args: argparse.Namespace, tail: list[str], prompter: Prompter) -> tuple[argparse.Namespace, list[str]]:
    prompter.require_terminal()
    if args.name is None:
        args.name = prompter.text("Schedule name", validator=validate_name)
    backend = _wizard_backend(args, prompter)
    scope = _wizard_scope(args, backend, prompter)
    if backend is Backend.CRON and scope is Scope.SYSTEM and args.run_as is None:
        args.run_as = prompter.text("Run as user", default="root", validator=cron_renderer.validate_run_as)
    tail = _wizard_command(args, tail, prompter)
    if not _has_schedule(args):
        _apply_schedule(args, prompter.schedule(backend))
    if backend is Backend.SYSTEMD:
        _wizard_advanced(args, prompter)
    return args, tail


def _wizard_advanced(args: argparse.Namespace, prompter: Prompter) -> None:
    provided = (
        args.persistent
        or args.jitter is not None
        or args.accuracy is not None
        or args.working_directory is not None
        or bool(args.env)
    )
    if provided or not prompter.yes_no("Set advanced timer options?", default=False):
        return
    args.persistent = prompter.yes_no("Persistent (run missed events)?", default=False)
    jitter = prompter.optional_text("Randomized delay")
    if jitter is not None:
        args.jitter = jitter
    accuracy = prompter.optional_text("Timer accuracy")
    if accuracy is not None:
        args.accuracy = accuracy
    working_directory = prompter.optional_text("Working directory (absolute path)")
    if working_directory is not None:
        args.working_directory = working_directory
    if prompter.yes_no("Add environment variables?", default=False):
        entries = prompter.environment()
        if entries:
            args.env = entries


def _wizard_edit(
    job: ScheduledJob, args: argparse.Namespace, tail: list[str], prompter: Prompter
) -> tuple[argparse.Namespace, list[str]]:
    prompter.require_terminal()
    if (
        not args.replace_command
        and not args.shell
        and not tail
        and prompter.yes_no("Replace the command?", default=False)
    ):
        command = prompter.command()
        if command.shell:
            args.shell = command.raw
        else:
            args.replace_command = True
            tail = list(command.argv)
    if job.backend is Backend.SYSTEMD:
        if not _has_schedule(args) and prompter.yes_no("Change the schedule?", default=False):
            _apply_schedule(args, prompter.schedule(job.backend))
        _wizard_edit_advanced(job, args, prompter)
    return args, tail


def _wizard_edit_advanced(job: ScheduledJob, args: argparse.Namespace, prompter: Prompter) -> None:
    if not prompter.yes_no("Change advanced timer options?", default=False):
        return
    details = job.systemd
    persistent = prompter.yes_no(
        "Persistent (run missed events)?",
        default=bool(details and details.persistent),
    )
    args.persistent = persistent
    args.no_persistent = not persistent
    jitter = prompter.optional_text("Randomized delay", default=details.jitter if details else None)
    if jitter is not None:
        args.jitter = jitter
    accuracy = prompter.optional_text("Timer accuracy", default=details.accuracy if details else None)
    if accuracy is not None:
        args.accuracy = accuracy
    working_directory = prompter.optional_text(
        "Working directory (absolute path)",
        default=details.working_directory if details else None,
    )
    if working_directory is not None:
        args.working_directory = working_directory
    if prompter.yes_no("Replace environment variables?", default=False):
        args.env = prompter.environment()


def _report_created(context: Context, spec: JobSpec) -> None:
    output = context.output
    if output.json_mode:
        return
    output.line(f"Created {spec.name}.")
    if spec.backend is Backend.SYSTEMD and spec.calendar:
        try:
            occurrences = context.systemd.calendar_occurrences(spec.calendar[0], iterations=1)
        except SchedlsError:
            occurrences = []
        if occurrences:
            output.line()
            output.line("Next run:")
            output.line(f"  {format_datetime(occurrences[0], utc=output.utc)}")
    output.line()
    output.line("Inspect:")
    output.line(f"  schedls show {spec.name}")
    if spec.backend is Backend.SYSTEMD:
        output.line()
        output.line("Logs:")
        output.line(f"  schedls logs {spec.name}")


if __name__ == "__main__":
    sys.exit(main())
