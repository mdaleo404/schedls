"""Interactive confirmation handling."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .errors import ConfirmationRequiredError
from .output import Output


@dataclass
class Interaction:
    output: Output
    assume_yes: bool = False

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        if self.assume_yes:
            return True
        self.output.stdout.flush()
        if not sys.stdin.isatty():
            raise ConfirmationRequiredError(
                "confirmation required but stdin is not interactive.",
                hint="Re-run with --yes after reviewing the command.",
            )
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            answer = input(f"{prompt} {suffix} ").strip().lower()
        except EOFError:
            raise ConfirmationRequiredError(
                "confirmation required but no input was available.",
                hint="Re-run with --yes after reviewing the command.",
            ) from None
        if not answer:
            return default
        return answer in {"y", "yes"}
