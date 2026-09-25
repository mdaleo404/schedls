"""Shared test fixtures and a controllable fake command runner."""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest

from schedls.errors import DependencyMissingError, OperationalError
from schedls.runner import Completed


class FakeRunner:
    """A duck-typed stand-in for :class:`schedls.runner.CommandRunner`."""

    def __init__(
        self,
        handlers: dict[str, Callable] | None = None,
        available: Iterable[str] = (),
    ) -> None:
        self.handlers = handlers or {}
        self.available = set(available)
        self.calls: list[tuple[list[str], str, str | None]] = []

    def has(self, name: str) -> bool:
        return name in self.available

    def resolve(self, name: str) -> str | None:
        return name if name in self.available else None

    def run(self, argv, *, env_policy="minimal", timeout=None, check=True, input_text=None):
        args = list(argv)
        self.calls.append((args, env_policy, input_text))
        handler = self.handlers.get(args[0])
        if handler is None:
            if args[0] not in self.available:
                raise DependencyMissingError(f"required command not found: {args[0]}")
            completed = Completed(tuple(args), 0, "", "")
        else:
            completed = _coerce(args, handler(args, input_text))
        if check and completed.returncode != 0:
            raise OperationalError(
                f"command failed ({completed.returncode}): {' '.join(args)}",
                hint=completed.stderr or completed.stdout or None,
            )
        return completed

    def try_run(self, argv, *, env_policy="minimal", timeout=None, input_text=None):
        if argv[0] not in self.available and argv[0] not in self.handlers:
            return None
        try:
            return self.run(argv, env_policy=env_policy, input_text=input_text)
        except OperationalError:
            return None


def _coerce(args: list[str], result) -> Completed:
    if isinstance(result, Completed):
        return result
    if isinstance(result, str):
        return Completed(tuple(args), 0, result, "")
    if isinstance(result, tuple):
        if len(result) == 3:
            return Completed(tuple(args), *result)
        if len(result) == 2:
            return Completed(tuple(args), 0, result[0], result[1])
        raise TypeError(f"unsupported fake result: {result!r}")
    raise TypeError(f"unsupported fake result: {result!r}")


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()
