"""Delivery tooling must work from a fresh monorepo and retain prior evidence."""

import hashlib
import importlib.util
import json
import socket
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "governance-tools"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ci_repeated_runs_retain_separate_complete_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci = load("ci_showcase")
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    tests = tmp_path / "qf-platform/tests"
    tests.mkdir(parents=True)
    (tests / "test_chart_example.py").touch()
    monkeypatch.setattr(sys, "argv", ["ci_showcase.py"])
    commands = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(ci.subprocess, "run", run)
    ci.main()
    ci.main()
    directories = list((tmp_path / "local-smoke").glob("ci-*"))
    assert len(directories) == 2
    assert len(commands) == 18
    for directory in directories:
        summary = json.loads((directory / "ci-summary.json").read_text())
        assert summary["passed"] and len(summary["checks"]) == 9
        assert (
            str(directory / "ci-contract-evaluation.json")
            in summary["checks"][7]["command"]
        )
        assert (directory / "ci-platform-coverage.json").is_file()


def test_ci_records_failure_and_does_not_run_later_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci = load("ci_showcase")
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    tests = tmp_path / "qf-platform/tests"
    tests.mkdir(parents=True)
    (tests / "test_chart_example.py").touch()
    output = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", ["ci_showcase.py", "--output-dir", str(output)])
    monkeypatch.setattr(
        ci.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 7),
    )
    with pytest.raises(subprocess.CalledProcessError):
        ci.main()
    summary = json.loads((output / "ci-summary.json").read_text())
    assert summary["passed"] is False
    assert len(summary["checks"]) == 1
    assert summary["checks"][0]["returncode"] == 7
    with pytest.raises(FileExistsError):
        ci.main()
    assert json.loads((output / "ci-summary.json").read_text()) == summary


def test_export_monorepo_includes_current_readme_launcher_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = load("export_showcase")
    monkeypatch.setattr(exporter, "ROOT", tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in exporter.ROOT_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture " + name, encoding="utf-8")
    for component in exporter.REPOS:
        directory = tmp_path / component
        (directory / "src").mkdir(parents=True)
        (directory / "src/example.py").write_text("# source", encoding="utf-8")
        (directory / ".env").write_text("PRIVATE_FIXTURE", encoding="utf-8")
        (directory / "unrelated.txt").write_text("PRIVATE_FIXTURE", encoding="utf-8")
    readme = "# Current README\n当前运行说明\n".encode()
    (tmp_path / "README.md").write_bytes(readme)
    output = tmp_path / "local-smoke/source.zip"
    exporter.export(output)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("README.md") == readme
        assert "governance-tools/local_showcase.py" in archive.namelist()
        assert "AGENTS.md" in archive.namelist()
        manifest = json.loads(archive.read("source-manifest.json"))
        for name, sha in manifest["files"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == sha
        assert not any(
            ".env" in name or "unrelated" in name for name in archive.namelist()
        )


def test_launcher_refuses_an_occupied_port(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = load("local_showcase")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        monkeypatch.setattr(launcher, "API_PORT", occupied.getsockname()[1])
        with pytest.raises(RuntimeError, match="in use"):
            launcher.require_free_ports()
