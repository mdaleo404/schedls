from __future__ import annotations

import os
import stat

import pytest

from schedls.errors import InvalidNameError, SafetyRefusalError
from schedls.runner import CommandRunner
from schedls.security import (
    atomic_write_text,
    check_replaceable,
    check_trusted_directory,
    is_managed_unit,
    is_safe_unit_name,
    remove_file,
    resolve_helper,
    unit_name,
    validate_managed_unit_name,
    validate_name,
)


@pytest.mark.parametrize(
    "name",
    ["backup", "postgres-backup", "report.daily", "sync_home", "a", "A1._-", "x" * 64],
)
def test_valid_names(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "../foo", "foo/bar", "name with spaces", "$(command)", ".hidden", "-lead", "x" * 65, "foo\nbar", "foo\n"],
)
def test_invalid_names(name: str) -> None:
    with pytest.raises(InvalidNameError):
        validate_name(name)


def test_unit_name() -> None:
    assert unit_name("backup", "timer") == "schedls-backup.timer"
    assert is_managed_unit("schedls-backup.timer")
    assert not is_managed_unit("certbot.timer")
    with pytest.raises(InvalidNameError):
        unit_name("../evil", "timer")


def test_atomic_write_and_remove(tmp_path) -> None:
    target = tmp_path / "file.txt"
    atomic_write_text(str(target), "hello\n", mode=0o600)
    assert target.read_text() == "hello\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert remove_file(str(target)) is True
    assert not target.exists()
    assert remove_file(str(target)) is False


def test_atomic_write_refuses_symlink(tmp_path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("secret")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    with pytest.raises(SafetyRefusalError):
        atomic_write_text(str(link), "overwrite")
    assert real.read_text() == "secret"


def test_remove_refuses_symlink(tmp_path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("secret")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    with pytest.raises(SafetyRefusalError):
        remove_file(str(link))


def test_check_replaceable_ownership(tmp_path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x")
    check_replaceable(str(target), expected_uid=os.getuid())
    with pytest.raises(SafetyRefusalError):
        check_replaceable(str(target), expected_uid=os.getuid() + 99999)


def test_resolve_helper_rejects_missing() -> None:
    assert resolve_helper("definitely-not-a-real-helper-xyz") is None


def test_resolve_helper_rejects_world_writable_dir(tmp_path, monkeypatch) -> None:
    helper_dir = tmp_path / "bin"
    helper_dir.mkdir()
    helper = helper_dir / "evilhelper"
    helper.write_text("#!/bin/sh\ntrue\n")
    helper.chmod(0o755)
    helper_dir.chmod(0o777)
    monkeypatch.setenv("PATH", str(helper_dir))
    assert resolve_helper("evilhelper") is None


def test_resolve_helper_rejects_group_writable_file(tmp_path, monkeypatch) -> None:
    helper_dir = tmp_path / "bin"
    helper_dir.mkdir()
    helper = helper_dir / "evilhelper"
    helper.write_text("#!/bin/sh\ntrue\n")
    helper.chmod(0o775)
    monkeypatch.setenv("PATH", str(helper_dir))
    assert resolve_helper("evilhelper") is None


def test_resolve_helper_rejects_user_owned_dir_when_root(tmp_path, monkeypatch) -> None:
    helper_dir = tmp_path / "bin"
    helper_dir.mkdir()
    helper = helper_dir / "evilhelper"
    helper.write_text("#!/bin/sh\ntrue\n")
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", str(helper_dir))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert resolve_helper("evilhelper") is None


def test_resolve_helper_accepts_own_dir_when_not_root(tmp_path, monkeypatch) -> None:
    helper_dir = tmp_path / "bin"
    helper_dir.mkdir()
    helper = helper_dir / "goodhelper"
    helper.write_text("#!/bin/sh\ntrue\n")
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", str(helper_dir))
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    assert resolve_helper("goodhelper") == str(helper)


def test_runner_refuses_untrusted_absolute_helper(tmp_path, monkeypatch) -> None:
    helper = tmp_path / "evil"
    helper.write_text("#!/bin/sh\ntrue\n")
    helper.chmod(0o755)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(SafetyRefusalError):
        CommandRunner().run([str(helper)])


def test_runner_refuses_relative_executable() -> None:
    with pytest.raises(SafetyRefusalError):
        CommandRunner().run(["bin/evil"])


def test_check_trusted_directory_owner_and_mode(tmp_path) -> None:
    check_trusted_directory(str(tmp_path), expected_uid=os.getuid())
    with pytest.raises(SafetyRefusalError):
        check_trusted_directory(str(tmp_path), expected_uid=os.getuid() + 99999)
    tmp_path.chmod(0o777)
    with pytest.raises(SafetyRefusalError):
        check_trusted_directory(str(tmp_path), expected_uid=os.getuid())


def test_validate_managed_unit_name() -> None:
    assert validate_managed_unit_name("schedls-backup.timer") == "schedls-backup.timer"
    assert validate_managed_unit_name("schedls-a_b.service") == "schedls-a_b.service"
    for unit in (
        "../../etc/passwd",
        "schedls-../x.timer",
        "certbot.timer",
        "schedls-x.timer/../y",
        "schedls-x.timer\n",
    ):
        with pytest.raises(SafetyRefusalError):
            validate_managed_unit_name(unit)


def test_is_safe_unit_name() -> None:
    assert is_safe_unit_name("certbot.service")
    assert is_safe_unit_name("foo@bar.timer")
    assert not is_safe_unit_name("--output=json")
    assert not is_safe_unit_name("../../evil.service")
    assert not is_safe_unit_name("unit with spaces.service")
