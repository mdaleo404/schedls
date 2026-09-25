from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "schedls"


def test_no_shell_true_in_production_code() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        if "subprocess" in text and "shell=True" in text:
            offenders.append(str(path))
    assert offenders == []


def test_subprocess_confined_to_runner() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if "import subprocess" in path.read_text() and path.name != "runner.py":
            offenders.append(str(path))
    assert offenders == []


def test_runner_explicitly_disables_shell() -> None:
    assert "shell=False" in (SRC / "runner.py").read_text()


def test_no_os_system_or_popen() -> None:
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        assert "os.system(" not in text, path
        assert "os.popen(" not in text, path
