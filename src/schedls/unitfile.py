"""Minimal, read-only systemd unit-file parser.

Configuration is treated strictly as data.  Values are never executed and only
the keys schedls needs are interpreted.
"""

from __future__ import annotations

from .security import MANAGED_COMMENT


def has_managed_marker(path: str, *, name: str) -> bool:
    """Whether a unit file carries the schedls ownership marker for ``name``."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    lines = {line.strip() for line in head.splitlines()}
    return MANAGED_COMMENT in lines and f"# Name: {name}" in lines


def read_units(path: str) -> dict[str, list[str]] | None:
    """Return section -> list of raw ``key=value`` lines, or ``None``."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            raw_lines = handle.read().splitlines()
    except OSError:
        return None

    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def values(sections: dict[str, list[str]] | None, section: str, key: str) -> list[str]:
    if not sections:
        return []
    result = []
    prefix = f"{key}="
    for line in sections.get(section, []):
        if line.startswith(prefix):
            result.append(line[len(prefix) :])
    return result


def first(sections: dict[str, list[str]] | None, section: str, key: str) -> str | None:
    found = values(sections, section, key)
    return found[-1] if found else None


def read_fragment(path: str) -> dict[str, list[str]] | None:
    return read_units(path)
