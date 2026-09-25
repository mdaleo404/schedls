"""Exception hierarchy and stable exit codes for schedls.

Exit codes are part of the public interface and must remain stable:

* ``0`` success
* ``1`` operational failure
* ``2`` invalid command-line input or invalid schedule
* ``3`` safety refusal / conflict
"""

from __future__ import annotations

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3


class SchedlsError(Exception):
    """Base class for all expected schedls failures."""

    exit_code = EXIT_FAILURE

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        text = f"Error: {self.message}"
        if self.hint:
            text = f"{text}\n\n{self.hint}"
        return text


class UsageError(SchedlsError):
    """The caller supplied invalid input."""

    exit_code = EXIT_USAGE


class InvalidNameError(UsageError):
    """A schedule name does not match the restricted grammar."""


class InvalidScheduleError(UsageError):
    """A schedule expression could not be validated."""


class NotFoundError(SchedlsError):
    """The requested schedule does not exist."""


class OperationalError(SchedlsError):
    """A helper command or filesystem operation failed."""


class DependencyMissingError(OperationalError):
    """A required native helper executable is unavailable."""


class CommandTimeoutError(OperationalError):
    """A helper command exceeded its allowed execution time."""


class SafetyRefusalError(SchedlsError):
    """schedls declined to perform a risky operation."""

    exit_code = EXIT_REFUSED


class ConflictError(SafetyRefusalError):
    """A destination already exists or an unmanaged object was targeted."""


class ConfirmationRequiredError(SafetyRefusalError):
    """Confirmation was required but cannot be obtained non-interactively."""
