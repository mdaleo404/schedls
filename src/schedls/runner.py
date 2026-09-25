"""Centralized, shell-free process execution.

Every helper process launched by schedls goes through :class:`CommandRunner`.
This is the single place that guarantees ``shell=False``, bounded timeouts,
controlled environments and debug logging.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

from .errors import CommandTimeoutError, DependencyMissingError, OperationalError, SafetyRefusalError
from .security import is_acceptable_helper, resolve_helper

DEFAULT_TIMEOUT = 10.0
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_BASE_ENV = {
    "LC_ALL": "C",
    "LANG": "C",
    "SYSTEMD_COLORS": "0",
    "SYSTEMD_PAGER": "cat",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
}

# Preserved for user-scoped systemd/dbus operations.
_SESSION_ENV_KEYS = (
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_ID",
    "XDG_SESSION_TYPE",
)

# Preserved for crontab, which consults identity variables.
_IDENTITY_ENV_KEYS = ("HOME", "USER", "LOGNAME", "SHELL", "TERM")

_SECRET_RE = re.compile(r"(?i)(pass|secret|token|key|credential)")


@dataclass
class Completed:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class _RunResult:
    completed: Completed
    timed_out: bool = False
    missing: str | None = None
    detail: str = ""


@dataclass
class CommandRunner:
    debug: bool = False
    timeout: float = DEFAULT_TIMEOUT
    log: list[_RunResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._resolved: dict[str, str | None] = {}

    def resolve(self, name: str) -> str | None:
        if os.sep in name:
            if not os.path.isabs(name) or not is_acceptable_helper(name):
                return None
            return name
        if name not in self._resolved:
            self._resolved[name] = resolve_helper(name)
        return self._resolved[name]

    def has(self, name: str) -> bool:
        return self.resolve(name) is not None

    def run(
        self,
        argv: Sequence[str],
        *,
        env_policy: str = "minimal",
        timeout: float | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> Completed:
        if not argv:
            raise OperationalError("attempted to run an empty command")
        if os.sep not in argv[0]:
            resolved = self.resolve(argv[0])
            if resolved is None:
                raise DependencyMissingError(f"required command not found: {argv[0]}")
            executable = resolved
        else:
            if not os.path.isabs(argv[0]) or not is_acceptable_helper(argv[0]):
                raise SafetyRefusalError(f"refusing to run untrusted executable: {argv[0]!r}")
            executable = argv[0]

        env = self._build_env(env_policy)
        effective = list(argv)
        effective[0] = executable
        self._debug(f"run {' '.join(self._redact(effective))}")

        try:
            proc = subprocess.run(  # noqa: S603 - argv form, shell disabled
                effective,
                shell=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout if timeout is not None else self.timeout,
                input=input_text,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DependencyMissingError(f"required command not found: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(
                f"{argv[0]} did not respond within {timeout if timeout is not None else self.timeout:g} seconds."
            ) from exc

        completed = Completed(
            argv=tuple(effective),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
        self._debug(f"exit {completed.returncode}")
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise OperationalError(
                f"command failed ({completed.returncode}): {' '.join(effective)}",
                hint=detail or None,
            )
        return completed

    def try_run(
        self,
        argv: Sequence[str],
        *,
        env_policy: str = "minimal",
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> Completed | None:
        """Run a command, returning ``None`` when the helper is unavailable."""
        if os.sep not in argv[0] and self.resolve(argv[0]) is None:
            return None
        try:
            return self.run(argv, env_policy=env_policy, timeout=timeout, input_text=input_text)
        except OperationalError:
            return None

    def _build_env(self, policy: str) -> dict[str, str]:
        env = dict(_BASE_ENV)
        env["PATH"] = DEFAULT_PATH
        if policy == "minimal":
            return env
        keys: tuple[str, ...]
        if policy == "systemd":
            keys = _SESSION_ENV_KEYS
        elif policy == "identity":
            keys = _IDENTITY_ENV_KEYS
        elif policy == "passthrough":
            return {**os.environ, **_BASE_ENV}
        else:
            raise OperationalError(f"unknown environment policy: {policy}")
        for key in keys:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        return env

    def _redact(self, argv: Sequence[str]) -> tuple[str, ...]:
        redacted = []
        for arg in argv:
            if "=" in arg and _SECRET_RE.search(arg.split("=", 1)[0]):
                key, _, _ = arg.partition("=")
                redacted.append(f"{key}=<redacted>")
            else:
                redacted.append(arg)
        return tuple(redacted)

    def _debug(self, message: str) -> None:
        if self.debug:
            print(f"[schedls] {message}", file=sys.stderr)
