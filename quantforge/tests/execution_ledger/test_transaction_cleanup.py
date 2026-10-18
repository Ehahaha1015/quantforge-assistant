"""Reviewer A CH-E: pre-COMMIT failures must release the writer transaction.

Once `_write_observation` has entered `BEGIN IMMEDIATE`, every exit path
before a successful COMMIT must roll back before propagating, so no writer
lock is stranded and no later call is misclassified as internal corruption.
Commit uncertainty is a separate boundary and keeps its poison/fail-closed
semantics (see `test_sidecar_commit_unknown.py`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from _ledger_support import (
    RECOVER_AT,
    advance_to_dispatching,
    make_provenance,
    make_store,
    make_target,
    recover_store,
)
from _support import OBSERVED_AT, make_observation

from quantforge.execstate import ExecutionStateBusyError
from quantforge.execution_ledger import BrokerObservationStatus
from quantforge.execution_ledger.ledger import DurableExecutionLedger


def _composed(tmp_path: Path):
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    state_dir = tmp_path / "state"
    store = make_store(state_dir)
    recover_store(store, target, observed_at=RECOVER_AT)
    advance_to_dispatching(store, provenance, target)
    sidecar = tmp_path / "ledger.sqlite3"
    ledger = DurableExecutionLedger(store, sidecar)
    return store, target, ledger, state_dir / "execstate.sqlite3", sidecar


def _observation(store, target):
    record, _receipt = store.read(target.submission_id, target)
    return make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
        status=BrokerObservationStatus.ACCEPTED,
        broker_reference="broker-ref-1",
        observed_at=OBSERVED_AT,
    )


def _rows(sidecar: Path) -> int:
    connection = sqlite3.connect(str(sidecar))
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
        )
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


def _require_writer_available(sidecar: Path) -> None:
    """An independent connection must not be blocked by a stranded lock."""

    connection = sqlite3.connect(
        str(sidecar), timeout=0.0, isolation_level=None
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:  # pragma: no cover
        pytest.fail(f"writer lock was stranded: {exc}")
    finally:
        connection.close()


def test_execstate_failure_rolls_back_the_writer_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, target, ledger, execstate_path, sidecar = _composed(tmp_path)
    try:
        observation = _observation(store, target)
        before_execstate = _execstate_snapshot(execstate_path)
        original_read = store.read
        state = {"fail": True}

        def flaky_read(*args, **kwargs):
            if state["fail"]:
                state["fail"] = False
                raise ExecutionStateBusyError(
                    "simulated EXECSTATE contention"
                )
            return original_read(*args, **kwargs)

        monkeypatch.setattr(store, "read", flaky_read)
        with pytest.raises(ExecutionStateBusyError):
            ledger.record_observation(observation)
        # The original failure propagated, nothing was persisted, and the
        # writer transaction was released.
        assert _rows(sidecar) == 0
        assert ledger._sidecar._connection.in_transaction is False
        assert _execstate_snapshot(execstate_path) == before_execstate
        _require_writer_available(sidecar)
        # After the transient condition clears the normal contract path runs:
        # no stale-transaction corruption, exactly one canonical row.
        monkeypatch.undo()
        stored = ledger.record_observation(observation)
        assert stored.observation_id == "execstate-observation-v1-1"
        assert _rows(sidecar) == 1
    finally:
        ledger.close()
        store.close()


def test_unexpected_callback_failure_rolls_back_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, target, ledger, execstate_path, sidecar = _composed(tmp_path)
    try:
        observation = _observation(store, target)
        before_execstate = _execstate_snapshot(execstate_path)

        def exploding_assessment(self, candidate, recorded):
            raise RuntimeError("assessment exploded")

        monkeypatch.setattr(
            DurableExecutionLedger,
            "_assess_recorded",
            exploding_assessment,
        )
        with pytest.raises(RuntimeError, match="assessment exploded"):
            ledger.record_observation(observation)
        assert _rows(sidecar) == 0
        assert ledger._sidecar._connection.in_transaction is False
        assert _execstate_snapshot(execstate_path) == before_execstate
        _require_writer_available(sidecar)
        monkeypatch.undo()
        assert ledger.record_observation(observation).observation_id == (
            "execstate-observation-v1-1"
        )
    finally:
        ledger.close()
        store.close()


def test_interrupt_is_not_converted_and_still_releases_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, target, ledger, _execstate, sidecar = _composed(tmp_path)
    try:
        observation = _observation(store, target)

        def interrupted_assessment(self, candidate, recorded):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            DurableExecutionLedger,
            "_assess_recorded",
            interrupted_assessment,
        )
        with pytest.raises(KeyboardInterrupt):
            ledger.record_observation(observation)
        assert _rows(sidecar) == 0
        assert ledger._sidecar._connection.in_transaction is False
        _require_writer_available(sidecar)
        monkeypatch.undo()
        assert ledger.record_observation(observation).observation_id == (
            "execstate-observation-v1-1"
        )
    finally:
        ledger.close()
        store.close()


def test_valid_untouched_ledger_still_appends_once(tmp_path: Path) -> None:
    store, target, ledger, _execstate, sidecar = _composed(tmp_path)
    try:
        observation = _observation(store, target)
        assert ledger.record_observation(observation).observation_id == (
            "execstate-observation-v1-1"
        )
        assert _rows(sidecar) == 1
    finally:
        ledger.close()
        store.close()
