from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from _ledger_support import (
    CLAIM_AT,
    CLAIM_EXPIRES_AT,
    LATE_AT,
    PREPARE_AT,
    RECOVER_AT,
    RECOVER_RESCAN_AT,
    make_provenance,
    make_target,
    recover_store,
)

from quantforge.execstate import (
    ExecutionStateDeniedError,
    SQLiteExecutionStateStore,
)
from quantforge.execution_ledger import AttemptPhase
from quantforge.execution_ledger.ledger import DurableExecutionLedger

_CRASH_WORKER = Path(__file__).with_name("_oracle_crash_worker.py")
_READER_WORKER = Path(__file__).with_name("_oracle_reader_worker.py")


def _observation_rows(sidecar: Path) -> int:
    if not sidecar.exists():
        return 0
    connection = sqlite3.connect(str(sidecar), isolation_level=None)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _run_reader(
    database: Path, target, sidecar: Path, moment: datetime, output: Path
) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(_READER_WORKER),
            str(database),
            target.model_dump_json(),
            str(sidecar),
            moment.isoformat(),
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def test_oracle_b_dispatching_process_death_restart(tmp_path: Path) -> None:
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    directory = tmp_path / "state"
    directory.mkdir()
    database = directory / "execstate.sqlite3"
    sidecar = tmp_path / "ledger.sqlite3"
    completed = subprocess.run(
        [
            sys.executable,
            str(_CRASH_WORKER),
            str(database),
            target.model_dump_json(),
            target.submission_id,
            RECOVER_AT.isoformat(),
            PREPARE_AT.isoformat(),
            CLAIM_AT.isoformat(),
            CLAIM_EXPIRES_AT.isoformat(),
            RECOVER_RESCAN_AT.isoformat(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT state, active_claim_id FROM submissions"
        ).fetchone()
        claims = int(
            connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        )
    finally:
        connection.close()
    assert row["state"] == "DISPATCHING"
    assert row["active_claim_id"] == "execstate-claim-v1-1"
    assert claims == 1
    assert _observation_rows(sidecar) == 0

    store = SQLiteExecutionStateStore(database)
    recover_store(store, target, observed_at=LATE_AT)
    ledger = DurableExecutionLedger(store, sidecar)
    try:
        current = ledger.read_attempt(target.submission_id, target)
        history = ledger.read_attempt_history(
            target.submission_id, target
        )
        with pytest.raises(ExecutionStateDeniedError):
            store.claim(
                target.submission_id,
                target,
                owner_id="new-worker",
                token="new-token",
                acquired_at=LATE_AT + timedelta(minutes=1),
                expires_at=LATE_AT + timedelta(minutes=2),
            )
    finally:
        ledger.close()
        store.close()
    assert current.execstate_state == "OUTCOME_UNKNOWN"
    assert current.phase is AttemptPhase.UNKNOWN
    assert current.active_claim_id is None
    assert history[0].execstate_state == "DISPATCHING"
    assert history[0].active_claim_id == "execstate-claim-v1-1"
    reopen = SQLiteExecutionStateStore(database)
    recover_store(reopen, target, observed_at=LATE_AT + timedelta(hours=1))
    reopened_ledger = DurableExecutionLedger(reopen, sidecar)
    try:
        reopened_view = reopened_ledger.read_attempt(
            target.submission_id, target
        )
    finally:
        reopened_ledger.close()
        reopen.close()
    assert reopened_view.phase is AttemptPhase.UNKNOWN
    assert reopened_view.execstate_state == "OUTCOME_UNKNOWN"
    reader = _run_reader(
        database,
        target,
        sidecar,
        LATE_AT + timedelta(hours=2),
        tmp_path / "reader.json",
    )
    assert reader["current"]["phase"] == "UNKNOWN"
    assert reader["current"]["execstate_state"] == "OUTCOME_UNKNOWN"
    assert reader["current"]["observation_ids"] == []
    assert _observation_rows(sidecar) == 0
