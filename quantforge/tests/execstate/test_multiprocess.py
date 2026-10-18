from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from _helpers import (
    CLAIM_EXPIRES_AT,
    RECOVER_RESCAN_AT,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)

from quantforge.execstate import (
    ExecutionStateBusyError,
    ExecutionStateConflictError,
    ExecutionStateDeniedError,
    ExecutionStateError,
)

_FAIL_CLOSED = {
    ExecutionStateBusyError.__name__,
    ExecutionStateConflictError.__name__,
    ExecutionStateDeniedError.__name__,
}


def _prepare_database(tmp_path):
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    database = store._path
    store.close()
    return database, envelope.submission_id, target


def _run_worker(
    worker: Path,
    database: Path,
    submission_id: str,
    target_json: str,
    owner_id: str,
    token: str,
    ready: Path,
    other_ready: Path,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            str(worker),
            str(database),
            target_json,
            submission_id,
            owner_id,
            token,
            RECOVER_RESCAN_AT.isoformat(),
            datetime(2030, 1, 1, 12, 1, 58, tzinfo=UTC).isoformat(),
            CLAIM_EXPIRES_AT.isoformat(),
            str(ready),
            str(other_ready),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_e_two_processes_compete_for_one_claim(tmp_path) -> None:
    database, submission_id, target = _prepare_database(tmp_path)
    worker = Path(__file__).with_name("_claim_worker.py")
    ready_a = tmp_path / "ready-a"
    ready_b = tmp_path / "ready-b"
    target_json = target.model_dump_json()
    first = _run_worker(
        worker,
        database,
        submission_id,
        target_json,
        "worker-a",
        "token-a",
        ready_a,
        ready_b,
    )
    second = _run_worker(
        worker,
        database,
        submission_id,
        target_json,
        "worker-b",
        "token-b",
        ready_b,
        ready_a,
    )
    first_out, first_err = first.communicate(timeout=60)
    second_out, second_err = second.communicate(timeout=60)
    assert first.returncode == 0, first_err
    assert second.returncode == 0, second_err
    results = [
        json.loads(line)
        for line in (first_out + second_out).splitlines()
        if line.startswith("{")
    ]
    assert len(results) == 2
    winners = [item for item in results if item["result"] == "ACQUIRED"]
    losers = [item for item in results if item["result"] != "ACQUIRED"]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0]["result"] in _FAIL_CLOSED


def test_al_two_threads_compete_for_one_claim(tmp_path) -> None:
    _database, submission_id, target = _prepare_database(tmp_path)
    barrier = threading.Barrier(2)
    recover_lock = threading.Lock()
    results: list[tuple[str, object]] = []
    lock = threading.Lock()

    def worker(owner_id: str, token: str) -> None:
        store = None
        try:
            with recover_lock:
                store = make_store(tmp_path)
                store.recover(target, observed_at=RECOVER_RESCAN_AT)
            barrier.wait(timeout=30)
            claim = store.claim(
                submission_id,
                target,
                owner_id=owner_id,
                token=token,
                acquired_at=datetime(2030, 1, 1, 12, 1, 58, tzinfo=UTC),
                expires_at=CLAIM_EXPIRES_AT,
            )
            with lock:
                results.append(("ACQUIRED", claim))
        except ExecutionStateError as exc:
            with lock:
                results.append((type(exc).__name__, str(exc)))
        except threading.BrokenBarrierError as exc:
            with lock:
                results.append(("BARRIER_BROKEN", str(exc)))
        finally:
            if store is not None:
                store.close()

    threads = [
        threading.Thread(target=worker, args=("worker-a", "token-a")),
        threading.Thread(target=worker, args=("worker-b", "token-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sum(1 for result, _ in results if result == "ACQUIRED") == 1
    assert sum(1 for result, _ in results if result in _FAIL_CLOSED) == 1
