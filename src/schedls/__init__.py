"""schedls — one interface for the things Linux runs later."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version_from_pyproject() -> str | None:
    for directory in Path(__file__).resolve().parents:
        candidate = directory / "pyproject.toml"
        if not candidate.is_file():
            continue
        with candidate.open("rb") as handle:
            data = tomllib.load(handle)
        project = data.get("project")
        if isinstance(project, dict) and isinstance(project.get("version"), str):
            return str(project["version"])
    return None


def _detect_version() -> str:
    found = _version_from_pyproject()
    if found is not None:
        return found
    try:
        return version("schedls")
    except PackageNotFoundError:
        return "0.0.0"


__version__ = _detect_version()

__all__ = ["__version__"]
