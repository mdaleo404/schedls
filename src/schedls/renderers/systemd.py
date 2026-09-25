"""Render systemd ``.service`` and ``.timer`` units.

``ExecStart=`` is not ordinary shell syntax.  Arguments are serialized with a
dedicated quoting routine that accounts for whitespace, quotes, backslashes,
literal ``$`` and ``%`` characters.  Every argument is always quoted so an
argument that begins with a systemd prefix character (``-@:+!``) cannot change
the meaning of the command line.
"""

from __future__ import annotations

from ..errors import SafetyRefusalError
from ..models import Command, JobSpec
from ..security import MANAGED_COMMENT, has_unsafe_control_characters


def quote_systemd_arg(arg: str) -> str:
    """Quote one argument using systemd unit-file quoting rules."""
    if has_unsafe_control_characters(arg):
        raise SafetyRefusalError("command arguments must not contain NUL or newlines")
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("$", "$$").replace("%", "%%")
    return f'"{escaped}"'


def render_exec_start(command: Command) -> str:
    if command.shell:
        raw = command.raw or ""
        return "ExecStart=/bin/sh -c " + quote_systemd_arg(raw)
    tokens = [quote_systemd_arg(arg) for arg in command.argv]
    return "ExecStart=" + " ".join(tokens)


def render_service(spec: JobSpec, unit: str) -> str:
    lines = [
        MANAGED_COMMENT,
        f"# Name: {spec.name}",
        "",
        "[Unit]",
        f"Description=schedls job {spec.name}",
        "",
        "[Service]",
        "Type=oneshot",
        render_exec_start(spec.command),
    ]
    if spec.working_directory:
        lines.append(f"WorkingDirectory={quote_systemd_arg(spec.working_directory)}")
    for key, value in spec.environment:
        lines.append(f"Environment={quote_systemd_arg(f'{key}={value}')}")
    lines.append("")
    return "\n".join(lines)


def render_timer(spec: JobSpec, unit: str, service_unit: str) -> str:
    lines = [
        MANAGED_COMMENT,
        f"# Name: {spec.name}",
        "",
        "[Unit]",
        f"Description=schedls job {spec.name} (timer)",
        "",
        "[Timer]",
    ]
    for expression in spec.schedule().all_expressions():
        lines.append(f"OnCalendar={expression}")
    if spec.persistent:
        lines.append("Persistent=true")
    if spec.jitter:
        lines.append(f"RandomizedDelaySec={spec.jitter}")
    if spec.accuracy:
        lines.append(f"AccuracySec={spec.accuracy}")
    lines.append(f"Unit={service_unit}")
    lines.extend(
        [
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return "\n".join(lines)


def render_units(spec: JobSpec, *, service_unit: str, timer_unit: str) -> dict[str, str]:
    return {
        service_unit: render_service(spec, service_unit),
        timer_unit: render_timer(spec, timer_unit, service_unit),
    }


def unquote_systemd_args(value: str) -> list[str]:
    """Inverse of :func:`quote_systemd_arg` for a full ExecStart value."""
    tokens: list[str] = []
    current: list[str] = []
    started = False
    in_quotes = False
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char == "\\" and index + 1 < length:
            current.append(value[index + 1])
            started = True
            index += 2
            continue
        if char == '"':
            in_quotes = not in_quotes
            started = True
            index += 1
            continue
        if char.isspace() and not in_quotes:
            if started:
                tokens.append("".join(current))
                current = []
                started = False
            index += 1
            continue
        current.append(char)
        started = True
        index += 1
    if started:
        tokens.append("".join(current))
    return [_collapse_specials(token) for token in tokens]


def _collapse_specials(token: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(token):
        char = token[index]
        if char in "$%" and index + 1 < len(token) and token[index + 1] == char:
            result.append(char)
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def parse_exec_start(value: str, *, shell: str | None = None) -> Command:
    """Rebuild a :class:`Command` from a native ExecStart value."""
    stripped = value.strip()
    if not stripped:
        return Command()
    if stripped[0] in "-@:+!":
        return Command(raw=stripped)
    prefix = shell or "/bin/sh"
    shell_prefix = f"{prefix} -c "
    if stripped.startswith(shell_prefix):
        remainder = stripped[len(shell_prefix) :]
        parsed = unquote_systemd_args(remainder)
        return Command(shell=True, raw=parsed[0] if parsed else "")
    return Command(argv=tuple(unquote_systemd_args(stripped)))
