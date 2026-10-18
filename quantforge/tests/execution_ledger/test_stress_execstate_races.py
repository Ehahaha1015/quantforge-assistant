"""Slice 8 bounded stress over the frozen EXECSTATE concurrency contract.

Two real-OS-process contention points are repeated a bounded number of
times: recovery run-key allocation and claim authority. Every observed
outcome must be either the contracted success or a contracted fail-closed
error; anything else is a genuine race and fails the round.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from _ledger_support import (
    CLAIM_EXPIRES_AT,
    LATE_AT,
    RECOVER_AT,
    RECOVER_RESCAN_AT,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)

from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.execution_ledger import AttemptPhase
from quantforge.execution_ledger.identity import derive_attempt_id
from quantforge.execution_ledger.ledger import DurableExecutionLedger

_RUN_KEY_WORKER = Path(__file__).with_name("_stress_run_key_worker.py")
_CLAIM_WORKER = Path(__file__).with_name("_stress_claim_worker.py")

_RUN_KEY_ROUNDS = 4
_RUN_KEY_WORKERS = 3
_CLAIM_ROUNDS = 4
_CLAIM_WORKERS = 2

_RUN_KEY_FAIL_CLOSED = {
    "ExecutionStateBusyError",
    "ExecutionStateDeniedError",
}
_CLAIM_FAIL_CLOSED = {
    "ExecutionStateBusyError",
    "ExecutionStateConflictError",
    "ExecutionStateDeniedError",
}
_CLAIM_ACQUIRED_AT = datetime(2030, 1, 1, 12, 1, 58, tzinfo=UTC)


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    return connection


def _run_keys(database: Path) -> list[int]:
    connection = _connect(database)
    try:
        return [
            int(row[0])
            for row in connection.execute(
                "SELECT run_sequence FROM recovery_runs "
                "ORDER BY run_sequence"
            )
        ]
    finally:
        connection.close()


def _sqlite_sequence(database: Path, table: str) -> int:
    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        connection.close()


def test_stress_run_key_allocation_race(tmp_path: Path) -> None:
    classification: Counter[str] = Counter()
    startup_busy_retries = 0
    for round_index in range(_RUN_KEY_ROUNDS):
        directory = tmp_path / f"run-key-{round_index}"
        provenance = make_provenance()
        target = make_target(
            provenance.pre_dispatch_decision.envelope.submission_id
        )
        store = make_store(directory)
        try:
            recover_store(store, target, observed_at=RECOVER_AT)
            prepare_store(store, provenance, target)
        finally:
            store.close()
        database = directory / "execstate.sqlite3"
        moment = LATE_AT.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result_paths = [
            directory / f"result-{index}.json"
            for index in range(_RUN_KEY_WORKERS)
        ]
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(_RUN_KEY_WORKER),
                    str(database),
                    target.model_dump_json(),
                    moment,
                    str(result_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for result_path in result_paths
        ]
        for process in processes:
            _output, error = process.communicate(timeout=120)
            assert process.returncode == 0, error
        payloads = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in result_paths
        ]
        classification.update(str(item["status"]) for item in payloads)
        startup_busy_retries += sum(
            int(item["setup_retries"]) for item in payloads
        )
        assert set(classification) <= {"ok"} | _RUN_KEY_FAIL_CLOSED
        successes = [item for item in payloads if item["status"] == "ok"]
        assert successes
        evidence_ids = {str(item["evidence_id"]) for item in successes}
        assert len(evidence_ids) == len(successes)
        # Run keys are allocated exactly once per committed recovery run and
        # remain contiguous; a losing process commits nothing.
        keys = _run_keys(database)
        assert keys == list(range(1, len(keys) + 1))
        assert keys == list(range(1, len(successes) + 2))
        assert _sqlite_sequence(database, "recovery_runs") == keys[-1]
    print(
        json.dumps(
            {
                "run_key": dict(sorted(classification.items())),
                "startup_busy_retries": startup_busy_retries,
            },
            sort_keys=True,
        )
    )


def test_stress_claim_authority_race(tmp_path: Path) -> None:
    classification: Counter[str] = Counter()
    startup_busy_retries = 0
    for round_index in range(_CLAIM_ROUNDS):
        directory = tmp_path / f"claim-{round_index}"
        provenance = make_provenance()
        target = make_target(
            provenance.pre_dispatch_decision.envelope.submission_id
        )
        store = make_store(directory)
        try:
            recover_store(store, target, observed_at=RECOVER_AT)
            prepare_store(store, provenance, target)
        finally:
            store.close()
        database = directory / "execstate.sqlite3"
        sidecar = directory / "ledger.sqlite3"
        ready_a = directory / "ready-a"
        ready_b = directory / "ready-b"
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(_CLAIM_WORKER),
                    str(database),
                    target.model_dump_json(),
                    target.submission_id,
                    owner,
                    f"token-{owner}",
                    RECOVER_RESCAN_AT.isoformat(),
                    _CLAIM_ACQUIRED_AT.isoformat(),
                    CLAIM_EXPIRES_AT.isoformat(),
                    str(ready),
                    str(other_ready),
                    str(sidecar),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for owner, ready, other_ready in (
                ("worker-a", ready_a, ready_b),
                ("worker-b", ready_b, ready_a),
            )
        ]
        outputs = []
        for process in processes:
            stdout, error = process.communicate(timeout=120)
            assert process.returncode == 0, error
            outputs.extend(
                json.loads(line)
                for line in stdout.splitlines()
                if line.startswith("{")
            )
        assert len(outputs) == _CLAIM_WORKERS
        classification.update(
            "ACQUIRED" if item["result"] == "ACQUIRED" else "FAIL_CLOSED"
            for item in outputs
        )
        startup_busy_retries += sum(
            int(item["setup_retries"]) for item in outputs
        )
        winners = [
            item for item in outputs if item["result"] == "ACQUIRED"
        ]
        losers = [
            item for item in outputs if item["result"] != "ACQUIRED"
        ]
        assert len(winners) == 1
        assert len(losers) == 1
        assert losers[0]["result"] in _CLAIM_FAIL_CLOSED
        assert losers[0]["observation_id"] is None
        winner = winners[0]
        assert winner["claim_id"] == "execstate-claim-v1-1"
        assert winner["observation_id"] == "execstate-observation-v1-1"

        connection = _connect(database)
        try:
            claims = connection.execute(
                "SELECT claim_id, owner_id, status FROM claims"
            ).fetchall()
            submission = connection.execute(
                "SELECT state, active_claim_id, generation, recovery_epoch "
                "FROM submissions"
            ).fetchone()
        finally:
            connection.close()
        assert len(claims) == 1
        assert claims[0]["claim_id"] == winner["claim_id"]
        assert claims[0]["owner_id"].startswith("worker-")
        assert claims[0]["status"] == "ACQUIRED"
        assert submission["state"] == "DISPATCHING"
        assert submission["active_claim_id"] == winner["claim_id"]
        expected_attempt = derive_attempt_id(
            target.submission_id,
            target.model_dump_json(),
            int(submission["generation"]),
            int(submission["recovery_epoch"]),
        )
        sidecar_connection = _connect(sidecar)
        try:
            sidecar_rows = sidecar_connection.execute(
                "SELECT observation_id, attempt_id FROM observations"
            ).fetchall()
        finally:
            sidecar_connection.close()
        assert len(sidecar_rows) == 1
        assert sidecar_rows[0]["observation_id"] == (
            winner["observation_id"]
        )
        assert sidecar_rows[0]["attempt_id"] == expected_attempt

        reopened = SQLiteExecutionStateStore(database)
        recover_store(reopened, target, observed_at=LATE_AT)
        ledger = DurableExecutionLedger(reopened, sidecar)
        try:
            history = ledger.read_attempt_history(
                target.submission_id, target
            )
            current = ledger.read_attempt(target.submission_id, target)
        finally:
            ledger.close()
            reopened.close()
        winner_view = next(
            view for view in history if view.attempt_id == expected_attempt
        )
        assert winner_view.execstate_state == "DISPATCHING"
        assert winner_view.phase is AttemptPhase.OBSERVED
        assert winner_view.active_claim_id == winner["claim_id"]
        assert winner_view.observation_ids == (winner["observation_id"],)
        assert current.execstate_state == "OUTCOME_UNKNOWN"
        assert current.phase is AttemptPhase.UNKNOWN
    print(
        json.dumps(
            {
                "claim": dict(sorted(classification.items())),
                "startup_busy_retries": startup_busy_retries,
            },
            sort_keys=True,
        )
    )
