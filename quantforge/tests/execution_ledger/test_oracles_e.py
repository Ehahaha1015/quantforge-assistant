from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from _ledger_support import (
    LATE_AT,
    OUTCOME_AT,
    RECOVER_AT,
    advance_to_dispatching,
    make_provenance,
    make_store,
    make_target,
    recover_store,
)
from _support import make_observation

from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.execution_ledger import AttemptPhase, BrokerObservationStatus
from quantforge.execution_ledger.ledger import DurableExecutionLedger

_READER_WORKER = Path(__file__).with_name("_oracle_reader_worker.py")


def _select(view: dict[str, object]) -> dict[str, object]:
    return {
        "attempt_id": view["attempt_id"],
        "generation": view["generation"],
        "recovery_epoch": view["recovery_epoch"],
        "phase": view["phase"],
        "phase_reason": view["phase_reason"],
        "execstate_state": view["execstate_state"],
        "active_claim_id": view["active_claim_id"],
        "observation_ids": view["observation_ids"],
    }


def _run_reader(
    database: Path,
    target,
    sidecar: Path,
    moment,
    output: Path,
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


def test_oracle_e_historical_reconstruction_fresh_process(
    tmp_path: Path,
) -> None:
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    directory = tmp_path / "state"
    store = make_store(directory)
    recover_store(store, target, observed_at=RECOVER_AT)
    advance_to_dispatching(store, provenance, target)
    database = directory / "execstate.sqlite3"
    sidecar = tmp_path / "ledger.sqlite3"
    first_ledger = DurableExecutionLedger(store, sidecar)
    record, _receipt = store.read(target.submission_id, target)
    first_observation = make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
        observed_at=LATE_AT,
        broker_reference="broker-ref-late",
    )
    first = first_ledger.record_observation(first_observation)
    assert first.observation_id == "execstate-observation-v1-1"
    first_ledger.close()
    store.close()
    second_store = SQLiteExecutionStateStore(database)
    recover_store(second_store, target, observed_at=LATE_AT)
    second_ledger = DurableExecutionLedger(second_store, sidecar)
    second_record, _receipt = second_store.read(
        target.submission_id, target
    )
    second_observation = make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=second_record.generation,
        recovery_epoch=second_record.recovery_epoch,
        status=BrokerObservationStatus.UNKNOWN,
        observed_at=OUTCOME_AT,
        broker_reference="broker-ref-early",
    )
    second = second_ledger.record_observation(second_observation)
    assert second.observation_id == "execstate-observation-v1-2"
    second_ledger.close()
    second_store.close()

    parent_store = SQLiteExecutionStateStore(database)
    recover_store(
        parent_store, target, observed_at=LATE_AT + timedelta(hours=1)
    )
    parent_ledger = DurableExecutionLedger(parent_store, sidecar)
    try:
        parent_history = parent_ledger.read_attempt_history(
            target.submission_id, target
        )
        parent_current = parent_ledger.read_attempt(
            target.submission_id, target
        )
    finally:
        parent_ledger.close()
        parent_store.close()
    assert parent_history[0].recovery_epoch == record.recovery_epoch
    assert parent_history[1].recovery_epoch == second_record.recovery_epoch
    assert parent_history[0].execstate_state == "DISPATCHING"
    assert parent_history[0].phase is AttemptPhase.OBSERVED
    assert parent_history[0].observation_ids == (first.observation_id,)
    assert parent_history[1].execstate_state == "OUTCOME_UNKNOWN"
    assert parent_history[1].phase is AttemptPhase.UNKNOWN
    assert parent_history[1].observation_ids == (second.observation_id,)
    assert parent_current.execstate_state == "OUTCOME_UNKNOWN"
    assert parent_current.phase is AttemptPhase.UNKNOWN
    prefix = [
        _select(
            {
                "attempt_id": view.attempt_id,
                "generation": view.generation,
                "recovery_epoch": view.recovery_epoch,
                "phase": view.phase.value,
                "phase_reason": view.phase_reason,
                "execstate_state": view.execstate_state,
                "active_claim_id": view.active_claim_id,
                "observation_ids": list(view.observation_ids),
            }
        )
        for view in parent_history[:2]
    ]

    reader_one = _run_reader(
        database,
        target,
        sidecar,
        LATE_AT + timedelta(hours=2),
        tmp_path / "reader-one.json",
    )
    reader_two = _run_reader(
        database,
        target,
        sidecar,
        LATE_AT + timedelta(hours=3),
        tmp_path / "reader-two.json",
    )
    assert [_select(view) for view in reader_one["history"][:2]] == prefix
    assert [_select(view) for view in reader_two["history"][:2]] == prefix
    for reader in (reader_one, reader_two):
        assert reader["current"]["phase"] == "UNKNOWN"
        assert reader["current"]["execstate_state"] == "OUTCOME_UNKNOWN"
        assert reader["current"]["observation_ids"] == []
    connection = sqlite3.connect(str(sidecar), isolation_level=None)
    try:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert count == 2
    assert parent_history[0].observation_ids == ("execstate-observation-v1-1",)
    assert parent_history[1].observation_ids == ("execstate-observation-v1-2",)
