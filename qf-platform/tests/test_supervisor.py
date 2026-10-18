import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from qf_platform.processes import python_process
from qf_platform.storage import PlatformError
from qf_platform.supervisor import WorkerSupervisor
from qf_platform.tasks import TaskService
from qf_platform.worker import Worker, WorkerLease


def wait_for(check, *, timeout: float = 15) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("Bounded process lifecycle condition was not observed")


def test_native_python_pid_and_venv_are_both_preserved() -> None:
    command, environment = python_process(
        [
            "-c",
            "import os,sys,json,qf_platform; "
            "print(json.dumps([os.getpid(),sys.prefix,sys.executable]))",
        ]
    )
    with subprocess.Popen(command, env=environment, stdout=subprocess.PIPE) as process:
        result = json.loads(process.communicate(timeout=10)[0])
        assert process.returncode == 0 and result[0] == process.pid
        assert Path(result[1]).resolve() == Path(sys.prefix).resolve()
        assert Path(result[2]).resolve() == Path(sys.executable).resolve()


def test_pipe_eof_stops_after_current_effect_not_next_step(tmp_path: Path) -> None:
    started, finish = threading.Event(), threading.Event()
    effects = []

    def tick() -> bool:
        started.set()
        assert finish.wait(5)
        effects.append("TEST_ONLY_EFFECT")
        return True

    worker = Worker(tmp_path, "assistant-tasks", tick=tick)
    reader, writer = os.pipe()
    control = worker.watch_parent(reader)
    thread = threading.Thread(target=worker.run)
    thread.start()
    try:
        assert started.wait(5)
        os.close(writer)
        assert worker.stop.wait(5)
        finish.set()
        thread.join(timeout=5)
        assert not thread.is_alive() and effects == ["TEST_ONLY_EFFECT"]
        assert worker.lease.status()["activity"]["state"] == "STOPPED"
    finally:
        finish.set()
        worker.stop.set()
        thread.join(timeout=5)
        control.join(timeout=5)
        os.close(reader)


def test_step_io_failure_is_not_silently_lock_contention(tmp_path: Path) -> None:
    def broken() -> bool:
        raise OSError("TEST ONLY failure text must not be persisted")

    worker = Worker(tmp_path, "assistant-tasks", tick=broken)
    with pytest.raises(PlatformError, match="REPEATED_STEP_FAILURE"):
        worker.run()
    activity = worker.lease.status()["activity"]
    assert activity["state"] == "STEP_FAILED"
    assert activity["reason"] == "STEP_REQUIRES_REVIEW"
    assert "TEST ONLY" not in str(activity)


def test_managed_process_closes_without_forced_termination(tmp_path: Path) -> None:
    host = WorkerSupervisor(tmp_path, domains=("assistant-tasks",))
    host.start()
    process = host.children["assistant-tasks"]
    try:
        wait_for(lambda: host.snapshot()["assistant-tasks"]["state"] == "IDLE")
    finally:
        host.close()
    assert process.returncode == 0
    status = WorkerLease(tmp_path, "assistant-tasks").status()
    assert status["activity"]["state"] == "STOPPED"
    assert datetime.fromisoformat(status["expires"]) <= datetime.now(UTC)


def test_restart_limit_is_explicit_and_does_not_interrupt_peer(tmp_path: Path) -> None:
    host = WorkerSupervisor(
        tmp_path, domains=("assistant-tasks", "simulation"), restart_limit=1
    )
    host.start()
    first = host.children["assistant-tasks"]
    peer = host.children["simulation"]
    try:
        first.kill()
        first.wait(timeout=5)
        wait_for(lambda: host.children["assistant-tasks"].pid != first.pid)
        replacement = host.children["assistant-tasks"]
        replacement.kill()
        replacement.wait(timeout=5)
        wait_for(
            lambda: (
                host.snapshot()["assistant-tasks"]["state"] == "RESTART_LIMIT_REACHED"
            )
        )
        state = host.snapshot()
        assert state["assistant-tasks"]["restarts"] == 1
        assert state["assistant-tasks"]["last_exit_code"] is not None
        assert state["simulation"]["pid"] == peer.pid and peer.poll() is None
        host.poll()
        assert host.children["assistant-tasks"].pid == replacement.pid
    finally:
        host.close()


def test_corrupt_task_journal_has_bounded_restarts_not_fake_success(
    tmp_path: Path,
) -> None:
    app = TaskService(tmp_path)
    app.create(
        {
            "mode": "OFFLINE_DEMO",
            "dataset": "synthetic-regimes-v1",
            "goal": "Synthetic corrupt journal supervisor fixture only.",
            "max_experiments": 2,
            "analysis": "cost_sensitivity",
        },
        "corrupt-journal",
    )
    with sqlite3.connect(app.path) as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET hash='forged'")
    host = WorkerSupervisor(tmp_path, domains=("assistant-tasks",), restart_limit=1)
    host.start()
    try:
        wait_for(
            lambda: (
                host.snapshot()["assistant-tasks"]["state"] == "RESTART_LIMIT_REACHED"
            )
        )
        with pytest.raises(PlatformError, match="INTEGRITY_FAILED"):
            app.list()
        assert (
            host.observers["assistant-tasks"].status()["activity"]["state"]
            == "STEP_FAILED"
        )
    finally:
        host.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows parent-handle crash assertion")
def test_windows_parent_crash_closes_child_control_pipe(tmp_path: Path) -> None:
    import ctypes

    command, environment = python_process(
        [
            str(Path(__file__).parent / "fixtures/worker_parent.py"),
            str(tmp_path),
        ]
    )
    parent = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    handle = None
    try:
        assert parent.stdout is not None
        pid = int(parent.stdout.readline())
        handle = kernel.OpenProcess(0x101001, False, pid)
        assert handle
        lease = WorkerLease(tmp_path, "assistant-tasks")
        wait_for(lambda: (lease.status() or {}).get("pid") == pid)
        parent.kill()
        parent.wait(timeout=5)
        assert kernel.WaitForSingleObject(handle, 10000) == 0
        assert lease.status()["activity"]["state"] == "STOPPED"
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        if handle:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        if parent.stdout is not None:
            parent.stdout.close()
