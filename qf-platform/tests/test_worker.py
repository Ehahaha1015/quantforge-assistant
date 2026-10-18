import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from quantforge.product.storage import writer_lock

from qf_platform.storage import PlatformError
from qf_platform.tasks import TaskService
from qf_platform.worker import Worker, WorkerLease


def test_expiry_generations_and_old_owner_release_are_fenced(tmp_path: Path) -> None:
    first = WorkerLease(tmp_path, "assistant-tasks", ttl=3)
    second = WorkerLease(tmp_path, "assistant-tasks", ttl=3)
    at = datetime.now(UTC)
    assert first.renew(now=at)
    assert not second.renew(now=at + timedelta(seconds=2))
    assert second.renew(now=at + timedelta(seconds=4))
    assert second.status()["generation"] == 2  # type: ignore[index]
    first.release()
    assert not first.renew(now=at + timedelta(seconds=5))
    assert second.status()["owner"] == second.owner  # type: ignore[index]
    with pytest.raises(PlatformError, match="NAIVE_TIME"):
        second.renew(now=datetime(2020, 1, 1))


def test_os_fence_blocks_expired_owner_concurrent_effects(tmp_path: Path) -> None:
    effects: list[int] = []
    worker = Worker(tmp_path, "assistant-tasks", tick=lambda: not effects.append(1))
    with writer_lock(worker.fence):
        assert not worker.run_once()
    assert not effects
    assert worker.run_once() and effects == [1]
    worker.stop.set()
    assert not worker.run_once() and effects == [1]


def test_worker_does_not_steal_live_lease(tmp_path: Path) -> None:
    first = Worker(tmp_path, "assistant-tasks", tick=lambda: True)
    other = Worker(tmp_path, "assistant-tasks", tick=lambda: True)
    assert first.run_once()
    assert not other.run_once()
    first.lease.release()
    assert other.run_once()


def test_separate_process_resumes_pure_checkpoint(tmp_path: Path) -> None:
    app = TaskService(tmp_path)
    task = app.create(
        {
            "mode": "OFFLINE_DEMO",
            "dataset": "synthetic-regimes-v1",
            "goal": "Synthetic subprocess checkpoint test only.",
            "max_experiments": 2,
            "analysis": "cost_sensitivity",
        },
        "subprocess-checkpoint",
    )
    command = [
        sys.executable,
        "-m",
        "qf_platform.worker",
        "--root",
        str(tmp_path),
        "--domain",
        "assistant-tasks",
        "--once",
    ]
    for completed in (1, 2):
        subprocess.run(command, check=True, capture_output=True, timeout=30)
        snapshot = app.get(task["id"])
        assert snapshot["completed_steps"] == completed
        assert snapshot["paid_model_calls"] == 0
        status = WorkerLease(tmp_path, "assistant-tasks").status()
        assert status is not None and status["pid"] != os.getpid()
    assert len(app.get(task["id"])["evidence"]) == 2
