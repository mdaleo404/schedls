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


def test_with_updated_job_preserves_all_other_content() -> None:
    document = CrontabDocument(SAMPLE)
    updated = document.with_updated_job("backup", "30 3 * * * /usr/local/bin/backup /srv/data")

    assert updated == SAMPLE.replace(
        "0 2 * * * /usr/local/bin/backup /srv/data",
        "30 3 * * * /usr/local/bin/backup /srv/data",
    )


def test_toggling_managed_job_preserves_escaped_command_text() -> None:
    original = "# schedls:begin name=backup\n0 2 * * * /usr/bin/printf '50\\%s\\n'\n# schedls:end name=backup\n"

    disabled = CrontabDocument(original).with_toggled_enabled("backup", False)

    assert "# schedls:disabled 0 2 * * * /usr/bin/printf '50\\%s\\n'" in disabled
    assert CrontabDocument(disabled).with_toggled_enabled("backup", True) == original


def test_toggling_managed_job_preserves_absent_trailing_newline() -> None:
    original = "# schedls:begin name=backup\n0 2 * * * /usr/bin/backup\n# schedls:end name=backup"

    disabled = CrontabDocument(original).with_toggled_enabled("backup", False)

    assert not disabled.endswith("\n")
    assert CrontabDocument(disabled).with_toggled_enabled("backup", True) == original


def test_with_updated_job_requires_exactly_one_job() -> None:
    document = CrontabDocument(
        "# schedls:begin name=backup\n"
        "0 2 * * * /usr/local/bin/backup\n"
        "0 3 * * * /usr/local/bin/other\n"
        "# schedls:end name=backup\n"
    )

    with pytest.raises(SafetyRefusalError, match="exactly one job"):
        document.with_updated_job("backup", "30 3 * * * /usr/local/bin/backup")


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
    with pytest.raises(SafetyRefusalError):
        document.with_updated_job("broken", "0 3 * * * /bin/true")


def test_malformed_disabled_marker_blocks_mutation() -> None:
    document = CrontabDocument(
        "# schedls:begin name=backup\n# schedls:disabled not a cron job\n# schedls:end name=backup\n"
    )

    assert document.has_malformed_markers()
    assert "line 2" in document.warnings[0]
    with pytest.raises(SafetyRefusalError):
        document.with_toggled_enabled("backup", True)


def test_indented_disabled_marker_inside_managed_block_blocks_mutation() -> None:
    document = CrontabDocument(
        "# schedls:begin name=backup\n  # schedls:disabled 0 2 * * * /usr/bin/backup\n# schedls:end name=backup\n"
    )

    assert document.has_malformed_markers()
    with pytest.raises(SafetyRefusalError):
        document.with_toggled_enabled("backup", True)


@pytest.mark.parametrize("marker", ["# schedls:disabled", "# schedls:disabled "])
def test_empty_disabled_marker_inside_managed_block_blocks_mutation(marker: str) -> None:
    document = CrontabDocument(f"# schedls:begin name=backup\n{marker}\n# schedls:end name=backup\n")

    assert document.has_malformed_markers()
    with pytest.raises(SafetyRefusalError):
        document.with_toggled_enabled("backup", True)


def test_malformed_disabled_marker_outside_managed_block_does_not_block_mutation() -> None:
    document = CrontabDocument("# schedls:disabled not a cron job\n")

    assert not document.has_malformed_markers()
    assert "name=backup" in document.with_block("backup", ["0 2 * * * /usr/bin/backup"])


def test_unsafe_environment_prefix_inside_managed_block_blocks_mutation() -> None:
    document = CrontabDocument(
        "# schedls:begin name=backup\n0 2 * * * VALUE=bad\x00value /usr/bin/backup\n# schedls:end name=backup\n"
    )

    assert document.has_malformed_markers()
    with pytest.raises(SafetyRefusalError):
        document.with_updated_job("backup", "30 3 * * * /usr/bin/backup")


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
