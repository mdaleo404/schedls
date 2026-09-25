"""Security primitives: name validation, trusted paths and safe writes."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import tempfile

from .errors import InvalidNameError, SafetyRefusalError

NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
_NAME_RE = re.compile(rf"\A{NAME_PATTERN}\Z")
UNIT_PREFIX = "schedls-"
MANAGED_COMMENT = "# Managed by schedls"

_MANAGED_UNIT_RE = re.compile(rf"\A{UNIT_PREFIX}{NAME_PATTERN}\.(?:service|timer)\Z")
_SAFE_UNIT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9:_.@\\-]{0,254}\.(?:service|timer)\Z")

_HELPER_DIR_ALLOWED_MODE_MASK = stat.S_IWGRP | stat.S_IWOTH


def validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise InvalidNameError(
            f"invalid schedule name: {name!r}",
            hint=(
                "Names must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}: "
                "start alphanumeric, then letters, digits, '_', '.' or '-'."
            ),
        )
    return name


def unit_name(name: str, suffix: str) -> str:
    """Build a schedls-owned systemd unit name from a validated schedule name."""
    return f"{UNIT_PREFIX}{validate_name(name)}.{suffix}"


def validate_managed_unit_name(unit: str) -> str:
    """Accept only unit names schedls itself would create.

    Used before a unit name is turned into a filesystem path so a crafted
    ``Unit=`` value cannot escape the trusted unit directory.
    """
    if not _MANAGED_UNIT_RE.match(unit):
        raise SafetyRefusalError(f"refusing to use unexpected unit name: {unit!r}")
    return unit


def is_safe_unit_name(unit: str) -> bool:
    """Whether a discovered unit name is safe to pass to a helper command."""
    return bool(_SAFE_UNIT_RE.match(unit))


def is_managed_unit(unit: str) -> bool:
    return unit.startswith(UNIT_PREFIX)


def validate_absolute_path(path: str, *, what: str) -> str:
    if not path:
        raise SafetyRefusalError(f"{what} path is empty")
    if not os.path.isabs(path):
        raise SafetyRefusalError(f"{what} path must be absolute: {path!r}")
    if "\x00" in path:
        raise SafetyRefusalError(f"{what} path contains a NUL byte")
    return path


def has_unsafe_control_characters(value: str) -> bool:
    return any(ch in value for ch in ("\x00", "\n", "\r"))


def _owner_is_acceptable(uid: int) -> bool:
    if os.geteuid() == 0:
        return uid == 0
    return uid in {0, os.getuid()}


def _helper_file_ok(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if not _owner_is_acceptable(st.st_uid):
        return False
    return not st.st_mode & _HELPER_DIR_ALLOWED_MODE_MASK


def _helper_dir_ok(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if not _owner_is_acceptable(st.st_uid):
        return False
    return not st.st_mode & _HELPER_DIR_ALLOWED_MODE_MASK


def is_acceptable_helper(path: str) -> bool:
    """Check an executable path is safe to run.

    The file and its containing directory must be owned by root (when running
    as root) or by root or the current user, and must not be group- or
    other-writable.  Symlinks are resolved so the final target is checked too.
    """
    resolved = os.path.abspath(path)
    for candidate in {resolved, os.path.realpath(resolved)}:
        if not _helper_file_ok(candidate):
            return False
        if not _helper_dir_ok(os.path.dirname(candidate)):
            return False
    return True


def resolve_helper(name: str) -> str | None:
    """Resolve a critical helper executable using a controlled lookup.

    Returns an absolute path or ``None``.  A resolved helper that is not owned
    by an acceptable user, or lives in a group-/other-writable directory, is
    rejected to reduce PATH-hijacking risk.
    """
    resolved = shutil.which(name)
    if not resolved:
        return None
    resolved = os.path.abspath(resolved)
    if not is_acceptable_helper(resolved):
        return None
    return resolved


def _expected_owner_uid() -> int:
    return 0 if os.geteuid() == 0 else os.getuid()


def check_trusted_directory(path: str, *, expected_uid: int) -> None:
    """Ensure a destination directory is trusted: real, owned and not writable."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise SafetyRefusalError(f"trusted directory does not exist: {path}") from None
    if stat.S_ISLNK(st.st_mode):
        raise SafetyRefusalError(f"trusted directory must not be a symlink: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise SafetyRefusalError(f"trusted path is not a directory: {path}")
    if st.st_uid != expected_uid:
        raise SafetyRefusalError(f"trusted directory is owned by uid {st.st_uid} (expected {expected_uid}): {path}")
    if st.st_mode & _HELPER_DIR_ALLOWED_MODE_MASK:
        raise SafetyRefusalError(f"trusted directory is group- or other-writable: {path}")


def check_replaceable(path: str, *, expected_uid: int) -> None:
    """Reject unexpected symlinks and ownership before overwriting a file."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise SafetyRefusalError(f"refusing to replace symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise SafetyRefusalError(f"refusing to replace non-regular file: {path}")
    if st.st_uid != expected_uid:
        raise SafetyRefusalError(f"refusing to replace file owned by uid {st.st_uid} (expected {expected_uid}): {path}")


def atomic_write_text(
    path: str,
    content: str,
    *,
    mode: int = 0o644,
    expected_uid: int | None = None,
) -> None:
    """Write ``content`` to ``path`` atomically.

    The file is created in the same directory, flushed and fsynced before an
    atomic rename, so a partial write can never be observed at ``path``.
    """
    owner = _expected_owner_uid() if expected_uid is None else expected_uid
    directory = os.path.dirname(path) or "."
    check_trusted_directory(directory, expected_uid=owner)
    check_replaceable(path, expected_uid=owner)

    fd, tmp_path = tempfile.mkstemp(prefix=".schedls-", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, mode)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise
    _fsync_directory(directory)


def remove_file(path: str, *, expected_uid: int | None = None) -> bool:
    """Remove a regular file, refusing symlinks and ownership mismatches."""
    owner = _expected_owner_uid() if expected_uid is None else expected_uid
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        raise SafetyRefusalError(f"refusing to remove symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise SafetyRefusalError(f"refusing to remove non-regular file: {path}")
    if st.st_uid != owner:
        raise SafetyRefusalError(f"refusing to remove file owned by uid {st.st_uid} (expected {owner}): {path}")
    check_trusted_directory(os.path.dirname(path) or ".", expected_uid=owner)
    os.unlink(path)
    _fsync_directory(os.path.dirname(path) or ".")
    return True


def _fsync_directory(directory: str) -> None:
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
