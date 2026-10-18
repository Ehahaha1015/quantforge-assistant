from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from _ledger_support import (
    OUTCOME_AT,
    RECOVER_AT,
    advance_to_dispatching,
    make_provenance,
    make_store,
    make_target,
    recover_store,
)
from _support import make_observation

from quantforge.broker import BrokerPreDispatchError, BrokerSubmissionOutcome
from quantforge.execution_ledger import (
    BrokerObservationStatus,
    ExecutionLedgerCorruptionError,
    ReconciliationConflict,
    ReconciliationReason,
    ReconciliationResult,
)
from quantforge.execution_ledger.ledger import DurableExecutionLedger


def _snapshot(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        tables = sorted(
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if str(row["name"]) != "sqlite_sequence"
        )
        rows = {
            table: [
                tuple(row)
                for row in connection.execute(
                    f"SELECT * FROM {table}"
                ).fetchall()
            ]
            for table in tables
        }
        sequences = [
            tuple(row)
            for row in connection.execute(
                "SELECT name, seq FROM sqlite_sequence ORDER BY name"
            ).fetchall()
        ]
        version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
    finally:
        connection.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "tables": tables,
        "rows": rows,
        "sequences": sequences,
        "user_version": version,
        "file_sha256": digest,
    }


def _completed_history(directory: Path, kind: str):
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    store = make_store(directory)
    recover_store(store, target, observed_at=RECOVER_AT)
    advance_to_dispatching(store, provenance, target)
    if kind == "acknowledged":
        store.persist_transport_outcome(
            BrokerSubmissionOutcome(
                submission_id=target.submission_id,
                status="ACCEPTED",
                broker_reference="broker-ref-1",
                broker_message=None,
            ),
            target,
            owner_id="worker-1",
            token="token-1",
            observed_at=OUTCOME_AT,
        )
    elif kind == "rejected":
        store.persist_transport_outcome(
            BrokerSubmissionOutcome(
                submission_id=target.submission_id,
                status="REJECTED",
                broker_reference="broker-ref-1",
                broker_message=None,
            ),
            target,
            owner_id="worker-1",
            token="token-1",
            observed_at=OUTCOME_AT,
        )
    elif kind == "no_effect":
        store.persist_pre_dispatch_no_effect(
            target.submission_id,
            target,
            BrokerPreDispatchError("pre-dispatch-proof-1"),
            owner_id="worker-1",
            token="token-1",
            observed_at=OUTCOME_AT,
        )
    else:
        raise ValueError(kind)
    return store, target


def test_oracle_c_reconciliation_conflict_zero_mutation(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "acknowledged",
            "ACKNOWLEDGED",
            BrokerObservationStatus.REJECTED,
            ReconciliationReason.CONFLICT_TERMINAL_ACCEPTED_VS_REJECTED,
        ),
        (
            "rejected",
            "REJECTED",
            BrokerObservationStatus.ACCEPTED,
            ReconciliationReason.CONFLICT_TERMINAL_REJECTED_VS_ACCEPTED,
        ),
        (
            "no_effect",
            "NO_EFFECT_CONFIRMED",
            BrokerObservationStatus.ACCEPTED,
            ReconciliationReason.CONFLICT_NO_EFFECT_VS_ACCEPTED,
        ),
    )
    for kind, expected_state, status, expected_reason in cases:
        directory = tmp_path / kind
        store, target = _completed_history(directory, kind)
        sidecar = directory / "ledger.sqlite3"
        ledger = DurableExecutionLedger(store, sidecar)
        try:
            record, _receipt = store.read(target.submission_id, target)
            observation = make_observation(
                submission_id=target.submission_id,
                target=target,
                generation=record.generation,
                recovery_epoch=record.recovery_epoch,
                status=status,
                broker_reference="broker-ref-2",
            )
            execstate_path = directory / "execstate.sqlite3"
            before_execstate = _snapshot(execstate_path)
            before_sidecar = _snapshot(sidecar)
            with pytest.raises(ReconciliationConflict) as first:
                ledger.record_observation(observation)
            assert not isinstance(
                first.value, ExecutionLedgerCorruptionError
            )
            assert first.value.assessment.result is (
                ReconciliationResult.CONFLICT
            )
            assert first.value.assessment.reason is expected_reason
            assert _snapshot(execstate_path) == before_execstate
            assert _snapshot(sidecar) == before_sidecar
            with pytest.raises(ReconciliationConflict) as second:
                ledger.record_observation(observation)
            assert second.value.assessment == first.value.assessment
            assert _snapshot(execstate_path) == before_execstate
            assert _snapshot(sidecar) == before_sidecar
            assert len(
                ledger.read_observations(
                    first.value.assessment.attempt_id
                )
            ) == 0
            assert store.read(target.submission_id, target)[0].state == (
                expected_state
            )
        finally:
            ledger.close()
            store.close()
