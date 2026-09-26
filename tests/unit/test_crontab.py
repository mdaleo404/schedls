from __future__ import annotations

import pytest

from schedls.backends.cron import CrontabDocument
from schedls.errors import ConflictError, SafetyRefusalError

SAMPLE = (
    "MAILTO=admin@example.net\n"
    "SHELL=/bin/bash\n"
    "PATH=/usr/local/bin:/usr/bin\n"
    "\n"
    "# personal job\n"
    "15 7 * * * ~/bin/foo\n"
    "@daily /usr/local/bin/nightly\n"
    "\n"
    "# schedls:begin name=backup\n"
    "0 2 * * * /usr/local/bin/backup /srv/data\n"
    "# schedls:end name=backup\n"
)


def test_lossless_round_trip() -> None:
    document = CrontabDocument(SAMPLE)
    assert document.render() == SAMPLE


def test_managed_block_detected() -> None:
    document = CrontabDocument(SAMPLE)
    blocks = document.managed_blocks()
    assert list(blocks) == ["backup"]
    assert blocks["backup"].job_lines[0].expression == "0 2 * * *"
    assert blocks["backup"].job_lines[0].command == "/usr/local/bin/backup /srv/data"
    assert not document.has_malformed_markers()


def test_environment_and_nickname_parsed() -> None:
    document = CrontabDocument(SAMPLE)
    kinds = [entry.kind for entry in document.entries]
    assert kinds.count("env") == 3
    assert kinds.count("comment") == 1
    assert kinds.count("managed_begin") == 1
    assert kinds.count("managed_end") == 1
    jobs = [entry for entry in document.entries if entry.kind == "job"]
    assert jobs[0].expression == "15 7 * * *"
    assert jobs[1].expression == "@daily"


def test_with_block_preserves_unrelated_content() -> None:
    document = CrontabDocument(SAMPLE)
    updated = document.with_block("report", ["30 6 * * * /usr/local/bin/report"])
    assert "MAILTO=admin@example.net" in updated
    assert "15 7 * * * ~/bin/foo" in updated
    assert "0 2 * * * /usr/local/bin/backup /srv/data" in updated
    assert "# schedls:begin name=report" in updated
    reparsed = CrontabDocument(updated)
    assert set(reparsed.managed_blocks()) == {"backup", "report"}


def test_with_block_conflict() -> None:
    document = CrontabDocument(SAMPLE)
    with pytest.raises(ConflictError):
        document.with_block("backup", ["0 3 * * * /bin/true"])


def test_without_block_removes_only_block() -> None:
    document = CrontabDocument(SAMPLE)
    updated = document.without_block("backup")
    assert "backup" not in updated
    assert "MAILTO=admin@example.net" in updated
    assert "15 7 * * * ~/bin/foo" in updated
    assert "@daily /usr/local/bin/nightly" in updated


def test_without_unknown_block_refused() -> None:
    document = CrontabDocument(SAMPLE)
    with pytest.raises(SafetyRefusalError):
        document.without_block("nope")


def test_malformed_marker_blocks_mutation() -> None:
    document = CrontabDocument("# schedls:begin name=broken\n0 2 * * * /bin/true\n")
    assert document.has_malformed_markers()
    with pytest.raises(SafetyRefusalError):
        document.with_block("new", ["0 3 * * * /bin/true"])
    with pytest.raises(SafetyRefusalError):
        document.without_block("broken")


def test_empty_crontab() -> None:
    document = CrontabDocument("")
    assert document.render() == ""
    updated = document.with_block("x", ["0 0 * * * /bin/true"])
    assert updated.endswith("\n")
    assert "name=x" in updated


SYSTEM_SAMPLE = (
    "SHELL=/bin/sh\n"
    "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin\n"
    "\n"
    "# system jobs\n"
    "17 * * * * root cd / && run-parts --report /etc/cron.hourly\n"
    "@daily www-data /usr/local/bin/report\n"
    "\n"
    "# schedls:begin name=cleanup\n"
    "30 3 * * 0 root /usr/local/bin/cleanup\n"
    "# schedls:end name=cleanup\n"
)


def test_system_crontab_parses_user_field() -> None:
    document = CrontabDocument(SYSTEM_SAMPLE, system=True)
    jobs = [entry for entry in document.entries if entry.kind == "job"]
    assert jobs[0].expression == "17 * * * *"
    assert jobs[0].user == "root"
    assert jobs[0].command == "cd / && run-parts --report /etc/cron.hourly"
    assert jobs[1].expression == "@daily"
    assert jobs[1].user == "www-data"
    assert jobs[1].command == "/usr/local/bin/report"


def test_system_crontab_detects_managed_block() -> None:
    document = CrontabDocument(SYSTEM_SAMPLE, system=True)
    block = document.find_block("cleanup")
    assert block is not None
    assert block.job_lines[0].user == "root"
    assert document.render() == SYSTEM_SAMPLE


def test_user_crontab_still_parses_five_fields() -> None:
    document = CrontabDocument("15 7 * * * ~/bin/foo\n", system=False)
    assert document.entries[0].expression == "15 7 * * *"
    assert document.entries[0].user is None
    assert document.entries[0].command == "~/bin/foo"


def test_system_entry_without_user_is_not_a_job() -> None:
    document = CrontabDocument("15 7 * * * ~/bin/foo\n", system=True)
    assert document.entries[0].kind == "comment"
