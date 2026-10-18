from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quantforge.autolab.adapters.base import ProcessAdapter
from quantforge.autolab.adapters.codex import CodexAdapter
from quantforge.autolab.adapters.deepseek import DeepSeekAdapter
from quantforge.autolab.models import ReviewTimeouts, TransportStatus
from quantforge.autolab.transport import LauncherError, LauncherType, resolve_launcher


def _touch(path: Path) -> str:
    path.write_text("", encoding="utf-8")
    return str(path)


def test_launcher_classification_and_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    native = resolve_launcher(_touch(tmp_path / "reviewer.exe"))
    assert native.launcher_type == LauncherType.NATIVE
    assert native.argv(["--prompt"]) == [str((tmp_path / "reviewer.exe").resolve()), "--prompt"]

    script = _touch(tmp_path / "dsh.ps1")
    monkeypatch.setattr("quantforge.autolab.transport._resolve_pwsh", lambda: r"C:\pwsh.exe")
    powershell = resolve_launcher(script)
    assert powershell.launcher_type == LauncherType.POWERSHELL
    assert powershell.argv(["--profile", "headless", "中文"]) == [
        r"C:\pwsh.exe",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str((tmp_path / "dsh.ps1").resolve()),
        "--profile",
        "headless",
        "中文",
    ]


def test_cmd_and_node_launcher_classification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = _touch(tmp_path / "reviewer.cmd")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    resolved_cmd = resolve_launcher(cmd)
    assert resolved_cmd.launcher_type == LauncherType.CMD
    assert resolved_cmd.argv(["中文"])[:4] == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/s",
        "/c",
    ]

    js = _touch(tmp_path / "reviewer.js")
    monkeypatch.setattr("quantforge.autolab.transport._which", lambda candidates: r"C:\node.exe")
    resolved_node = resolve_launcher(js)
    assert resolved_node.launcher_type == LauncherType.NODE
    assert resolved_node.argv([]) == [r"C:\node.exe", str((tmp_path / "reviewer.js").resolve())]


def test_cmd_wrapper_executes_spaced_path_and_literal_metacharacters(tmp_path: Path) -> None:
    wrapper_dir = tmp_path / "reviewer tools"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "echo args.cmd"
    wrapper.write_text("@echo off\necho ARG1=[%~1]\necho ARG2=[%~2]\n", encoding="ascii")
    completed = ProcessAdapter(str(wrapper)).invoke(["hello world", "second"], str(tmp_path), 10)
    assert completed.returncode == 0
    assert "ARG1=[hello world]" in completed.stdout
    assert "ARG2=[second]" in completed.stdout


def test_cmd_wrapper_rejects_metacharacter_arguments(tmp_path: Path) -> None:
    wrapper = tmp_path / "echo.cmd"
    wrapper.write_text("@echo off\necho %1\n", encoding="ascii")
    with pytest.raises(LauncherError, match="unsafe CMD metacharacter"):
        ProcessAdapter(str(wrapper)).invoke(["a&b"], str(tmp_path), 10)
    with pytest.raises(LauncherError, match="unsafe CMD metacharacter"):
        ProcessAdapter(str(wrapper)).invoke(["line1\nline2"], str(tmp_path), 10)


def test_unsupported_launcher_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(LauncherError, match="unsupported reviewer launcher"):
        resolve_launcher(_touch(tmp_path / "reviewer.sh"))


def test_codex_does_not_fall_back_to_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", r"C:\does-not-exist")
    with pytest.raises(LauncherError, match="reviewer executable not found"):
        resolve_launcher("codex")


def test_invalid_comspec_falls_back_to_valid_cmd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMSPEC", r"relative-cmd.exe")
    wrapper = tmp_path / "qf-test-wrapper.cmd"
    launcher = resolve_launcher(_touch(wrapper))
    assert Path(launcher.prefix[0]).is_absolute()


def test_deepseek_cmd_preflight_requires_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = tmp_path / "dsh.cmd"
    wrapper.write_text("@echo off\n", encoding="ascii")
    import quantforge.autolab.adapters.deepseek as deepseek_module

    original = deepseek_module._which
    monkeypatch.setattr(
        deepseek_module,
        "_which",
        lambda candidates: None if candidates == ("node.exe",) else original(candidates),
    )
    with pytest.raises(LauncherError, match="node.exe"):
        DeepSeekAdapter(executable=str(wrapper)).preflight()


def test_codex_rejects_wrapper_launcher(tmp_path: Path) -> None:
    wrapper = tmp_path / "codex.cmd"
    wrapper.write_text("@echo off\n", encoding="ascii")
    with pytest.raises(LauncherError, match="native executable"):
        CodexAdapter(executable=str(wrapper)).preflight()


def test_spawn_failure_records_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = ProcessAdapter(sys.executable)

    def fail(*args, **kwargs):
        raise OSError("spawn denied")

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(OSError):
        adapter.invoke(["-c", "pass"], ".", 1)
    assert adapter.last_telemetry is not None
    assert adapter.last_telemetry.transport_status == "spawn_error"
    assert adapter.last_telemetry.exit_code is None


def test_process_inherits_environment_and_captures_utf8(tmp_path: Path) -> None:
    child = tmp_path / "emit.py"
    child.write_text(
        "import os, sys\n"
        "import json\n"
        "sys.stdout.buffer.write(json.dumps({'message':'中文 → 😀', 'path':os.environ['PATH']}, ensure_ascii=False).encode('utf-8'))\n"
        "sys.stderr.buffer.write('错误'.encode('utf-8'))\n",
        encoding="utf-8",
    )
    previous = os.environ.get("QF_TRANSPORT_TEST")
    os.environ["QF_TRANSPORT_TEST"] = "inherited"
    try:
        adapter = ProcessAdapter(sys.executable)
        completed = adapter.invoke([str(child)], str(tmp_path), 10)
    finally:
        if previous is None:
            os.environ.pop("QF_TRANSPORT_TEST", None)
        else:
            os.environ["QF_TRANSPORT_TEST"] = previous
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["message"] == "中文 → 😀"
    assert payload["path"] == os.environ["PATH"]
    assert completed.stderr == "错误"
    assert adapter.last_telemetry is not None


def test_invalid_utf8_is_replaced(tmp_path: Path) -> None:
    child = tmp_path / "invalid.py"
    child.write_text("import sys; sys.stdout.buffer.write(b'good\\xff')", encoding="utf-8")
    completed = ProcessAdapter(sys.executable).invoke([str(child)], str(tmp_path), 10)
    assert completed.stdout == "good�"


def test_nonzero_exit_is_preserved(tmp_path: Path) -> None:
    child = tmp_path / "fail.py"
    child.write_text("raise SystemExit(7)", encoding="utf-8")
    completed = ProcessAdapter(sys.executable).invoke([str(child)], str(tmp_path), 10)
    assert completed.returncode == 7


def test_spawn_failure_is_distinct_from_process_error() -> None:
    adapter = CodexAdapter(executable="definitely-missing-reviewer")
    result, attempt = adapter.review("probe", ".", "sha", 1)
    assert result is None
    assert attempt.status == TransportStatus.SPAWN_ERROR


def test_process_tree_cleanup_on_timeout(tmp_path: Path) -> None:
    child = tmp_path / "tree.py"
    pid_file = tmp_path / "grandchild.pid"
    child.write_text(
        "import pathlib, subprocess, sys, time\n"
        f"p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    with pytest.raises(subprocess.TimeoutExpired):
        ProcessAdapter(sys.executable).invoke([str(child)], str(tmp_path), 0.5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and pid_file.exists():
        pid = int(pid_file.read_text())
        if os.name == "nt":
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            alive = bool(handle)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
            if not alive:
                break
        else:
            try:
                os.kill(pid, 0)
            except OSError:
                break
        time.sleep(0.1)
    if pid_file.exists():
        pid = int(pid_file.read_text())
        if os.name == "nt":
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
            assert not handle
        else:
            with pytest.raises(OSError):
                os.kill(pid, 0)


def test_readiness_is_transport_only_with_explicit_timeout() -> None:
    class Runner:
        def __call__(self, *args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, "READY 中文", "")

    timeouts = ReviewTimeouts(readiness_timeout_seconds=3, review_hard_timeout_seconds=9)
    deepseek = DeepSeekAdapter(executable=sys.executable, runner=Runner(), timeouts=timeouts)
    _, attempt = deepseek.readiness("probe", ".")
    assert attempt.status == TransportStatus.SUCCESS
    assert attempt.stdout == "READY 中文"
    assert attempt.telemetry is not None
    assert attempt.telemetry.reviewer == "deepseek"
    assert attempt.telemetry.transport_status == "success"


def test_readiness_timeout_is_not_semantic_result() -> None:
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output="部分", stderr="错误")

    adapter = CodexAdapter(executable=sys.executable, runner=runner)
    result, attempt = adapter.readiness("probe", ".")
    assert result is None
    assert attempt.status == TransportStatus.READINESS_TIMEOUT
    assert attempt.stdout == "部分"
    assert attempt.stderr == "错误"


def test_review_timeout_uses_hard_timeout_classification() -> None:
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    adapter = CodexAdapter(executable=sys.executable, runner=runner)
    result, attempt = adapter.review("probe", ".", "sha", 1)
    assert result is None
    assert attempt.status == TransportStatus.HARD_TIMEOUT
