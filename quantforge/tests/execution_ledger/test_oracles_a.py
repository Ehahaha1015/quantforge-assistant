from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
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

_WORKER = Path(__file__).with_name("_oracle_claim_worker.py")
_ACQUIRED_AT = datetime(2030, 1, 1, 12, 1, 58, tzinfo=UTC)
_FAIL_CLOSED = {
    "ExecutionStateBusyError",
    "ExecutionStateConflictError",
    "ExecutionStateDeniedError",
}


def _spawn(
    database: Path,
    target_json: str,
    submission_id: str,
    owner_id: str,
    token: str,
    ready: Path,
    other_ready: Path,
    sidecar: Path,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            str(_WORKER),
            str(database),
            target_json,
            submission_id,
            owner_id,
            token,
            RECOVER_RESCAN_AT.isoformat(),
            _ACQUIRED_AT.isoformat(),
            CLAIM_EXPIRES_AT.isoformat(),
            str(ready),
            str(other_ready),
            str(sidecar),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _prepare_round(directory: Path) -> tuple[object, object, Path]:
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    store = make_store(directory)
    recover_store(store, target, observed_at=RECOVER_AT)
    prepare_store(store, provenance, target)
    database = directory / "execstate.sqlite3"
    store.close()
    return provenance, target, database


def test_oracle_a_two_worker_authority_race(tmp_path: Path) -> None:
    for round_index in range(3):
        directory = tmp_path / f"round-{round_index}"
        _provenance, target, database = _prepare_round(directory)
        submission_id = target.submission_id
        sidecar = directory / "ledger.sqlite3"
        ready_a = directory / "ready-a"
        ready_b = directory / "ready-b"
        first = _spawn(
            database,
            target.model_dump_json(),
            submission_id,
            "worker-a",
            "token-a",
            ready_a,
            ready_b,
            sidecar,
        )
        second = _spawn(
            database,
            target.model_dump_json(),
            submission_id,
            "worker-b",
            "token-b",
            ready_b,
            ready_a,
            sidecar,
        )
        first_out, first_err = first.communicate(timeout=90)
        second_out, second_err = second.communicate(timeout=90)
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
        assert losers[0]["observation_id"] is None
        winner = winners[0]
        assert winner["claim_id"] == "execstate-claim-v1-1"
        assert winner["observation_id"] == "execstate-observation-v1-1"

        connection = sqlite3.connect(str(database))
        connection.row_factory = sqlite3.Row
        try:
            claims = connection.execute(
                "SELECT claim_id, owner_id, status FROM claims"
            ).fetchall()
            submission = connection.execute(
                "SELECT state, active_claim_id, generation, "
                "recovery_epoch FROM submissions"
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
            submission_id,
            target.model_dump_json(),
            int(submission["generation"]),
            int(submission["recovery_epoch"]),
        )
        sidecar_connection = sqlite3.connect(str(sidecar))
        sidecar_connection.row_factory = sqlite3.Row
        try:
            sidecar_tables = {
                str(row["name"])
                for row in sidecar_connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
                if str(row["name"]) != "sqlite_sequence"
            }
            observation_rows = sidecar_connection.execute(
                "SELECT observation_id, attempt_id, status, observation_kind "
                "FROM observations"
            ).fetchall()
        finally:
            sidecar_connection.close()
        assert sidecar_tables == {"metadata", "observations"}
        assert len(observation_rows) == 1
        assert observation_rows[0]["observation_id"] == (
            winner["observation_id"]
        )
        assert observation_rows[0]["attempt_id"] == expected_attempt
        assert observation_rows[0]["status"] == "ACCEPTED"
        assert observation_rows[0]["observation_kind"] == (
            "BROKER_OBSERVATION"
        )

        reopened = SQLiteExecutionStateStore(database)
        recover_store(reopened, target, observed_at=LATE_AT)
        ledger = DurableExecutionLedger(reopened, sidecar)
        try:
            history = ledger.read_attempt_history(
                submission_id, target
            )
            current = ledger.read_attempt(submission_id, target)
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
        assert current.active_claim_id is None
