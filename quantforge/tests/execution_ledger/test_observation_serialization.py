"""Reviewer A CH-A: one serialized writer transaction per append.

Contract 7.3.3 / 9.3 / 13 / I9 require the assessment that authorizes an
append to be computed from the authoritative sidecar state while holding the
same `BEGIN IMMEDIATE` writer transaction that performs the append, so a
concurrent writer can never slip opposing evidence into the gap.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from _ledger_support import (
    LATE_AT,
    RECOVER_AT,
    advance_to_dispatching,
    make_provenance,
    make_store,
    make_target,
    recover_store,
)
from _support import OBSERVED_AT, make_observation

from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.execution_ledger import (
    BrokerObservationStatus,
    ExecutionLedgerCorruptionError,
    ReconciliationConflict,
)
from quantforge.execution_ledger.ledger import DurableExecutionLedger
from quantforge.execution_ledger.sidecar import SQLiteObservationStore


def _composed(tmp_path: Path):
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    state_dir = tmp_path / "state"
    store = make_store(state_dir)
    recover_store(store, target, observed_at=RECOVER_AT)
    advance_to_dispatching(store, provenance, target)
    ledger = DurableExecutionLedger(store, tmp_path / "ledger.sqlite3")
    return (
        store,
        target,
        ledger,
        state_dir / "execstate.sqlite3",
        tmp_path / "ledger.sqlite3",
    )


def _observation(store, target, status, reference):
    record, _receipt = store.read(target.submission_id, target)
    return make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
        status=status,
        broker_reference=reference,
        observed_at=OBSERVED_AT,
    )


def _rows(sidecar: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(str(sidecar))
    try:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT observation_sequence, observation_id, "
                "observation_kind, status, broker_reference "
                "FROM observations ORDER BY observation_sequence"
            )
        ]
    finally:
        connection.close()


def _execstate_snapshot(database: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(str(database))
    try:
        submission = connection.execute(
            "SELECT state, active_claim_id, generation, recovery_epoch "
            "FROM submissions"
        ).fetchone()
        claims = int(
            connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        )
        audits = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_events"
            ).fetchone()[0]
        )
        return (submission, claims, audits)
    finally:
        connection.close()


def test_assessment_runs_inside_the_writer_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, target, ledger, _execstate, _sidecar = _composed(tmp_path)
    try:
        first = ledger.record_observation(
            _observation(
                store,
                target,
                BrokerObservationStatus.ACCEPTED,
                "broker-ref-1",
            )
        )
        seen: dict[str, object] = {}
        original = DurableExecutionLedger._assess_recorded

        def spy(self, observation, recorded):
            seen["in_transaction"] = bool(
                self._sidecar._connection.in_transaction
            )
            seen["recorded_ids"] = tuple(
                item.observation_id for item in recorded
            )
            return original(self, observation, recorded)

        monkeypatch.setattr(DurableExecutionLedger, "_assess_recorded", spy)
        second = ledger.record_observation(
            _observation(
                store,
                target,
                BrokerObservationStatus.ACCEPTED,
                "broker-ref-2",
            )
        )
        # The authorizing assessment observed the writer transaction and the
        # authoritative recorded evidence, not a stale snapshot.
        assert seen["in_transaction"] is True
        assert seen["recorded_ids"] == (first.observation_id,)
        assert second.observation_id == "execstate-observation-v1-2"
    finally:
        ledger.close()
        store.close()


def test_serialized_opposing_observation_conflicts_with_zero_writes(
    tmp_path: Path,
) -> None:
    store, target, ledger, execstate_path, sidecar = _composed(tmp_path)
    try:
        first = ledger.record_observation(
            _observation(
                store,
                target,
                BrokerObservationStatus.ACCEPTED,
                "broker-ref-a",
            )
        )
        before_rows = _rows(sidecar)
        before_execstate = _execstate_snapshot(execstate_path)
        with pytest.raises(ReconciliationConflict) as raised:
            ledger.record_observation(
                _observation(
                    store,
                    target,
                    BrokerObservationStatus.REJECTED,
                    "broker-ref-b",
                )
            )
        assert raised.value.assessment.observation_id is None
        assert _rows(sidecar) == before_rows
        assert len(_rows(sidecar)) == 1
        assert _rows(sidecar)[0][1] == first.observation_id
        assert _execstate_snapshot(execstate_path) == before_execstate
        # No retry: a repeated attempt fails identically and appends nothing.
        with pytest.raises(ReconciliationConflict):
            ledger.record_observation(
                _observation(
                    store,
                    target,
                    BrokerObservationStatus.REJECTED,
                    "broker-ref-b",
                )
            )
        assert _rows(sidecar) == before_rows
        assert _execstate_snapshot(execstate_path) == before_execstate
    finally:
        ledger.close()
        store.close()

    # Reopening discovers no corruption introduced by the losing writer.
    reopened = SQLiteExecutionStateStore(execstate_path)
    reopened_ledger = None
    try:
        recover_store(reopened, target, observed_at=LATE_AT)
        reopened_ledger = DurableExecutionLedger(reopened, sidecar)
        history = reopened_ledger.read_attempt_history(
            target.submission_id, target
        )
    finally:
        if reopened_ledger is not None:
            reopened_ledger.close()
        reopened.close()
    recorded = [view for view in history if view.observation_ids]
    assert len(recorded) == 1
    assert recorded[0].observation_ids == (first.observation_id,)


def test_contradictory_recorded_evidence_blocks_append_as_corruption(
    tmp_path: Path,
) -> None:
    store, target, ledger, execstate_path, sidecar = _composed(tmp_path)
    try:
        # Legacy or tampered evidence written through the raw store surface.
        raw = SQLiteObservationStore(sidecar)
        try:
            raw.append(
                _observation(
                    store,
                    target,
                    BrokerObservationStatus.ACCEPTED,
                    "broker-ref-a",
                )
            )
            raw.append(
                _observation(
                    store,
                    target,
                    BrokerObservationStatus.REJECTED,
                    "broker-ref-b",
                )
            )
        finally:
            raw.close()
        before_rows = _rows(sidecar)
        before_execstate = _execstate_snapshot(execstate_path)
        # Internal contradiction stays in the corruption family, keeps
        # external-disagreement semantics distinct, and writes nothing.
        with pytest.raises(ExecutionLedgerCorruptionError) as raised:
            ledger.record_observation(
                _observation(
                    store,
                    target,
                    BrokerObservationStatus.ACCEPTED,
                    "broker-ref-c",
                )
            )
        assert not isinstance(raised.value, ReconciliationConflict)
        assert _rows(sidecar) == before_rows
        assert _execstate_snapshot(execstate_path) == before_execstate
    finally:
        ledger.close()
        store.close()
