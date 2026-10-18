from __future__ import annotations

from pathlib import Path

from _ledger_support import (
    RECOVER_AT,
    advance_to_dispatching,
    make_provenance,
    make_store,
    make_target,
    recover_store,
)
from _support import BrokerObservationStatus, make_observation

from quantforge.execution_ledger.ledger import DurableExecutionLedger


def _composed(tmp_path: Path):
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    store = make_store(tmp_path / "state")
    recover_store(store, target, observed_at=RECOVER_AT)
    advance_to_dispatching(store, provenance, target)
    ledger = DurableExecutionLedger(store, tmp_path / "ledger.sqlite3")
    return store, target, ledger


def _observation(store, target, *, status=BrokerObservationStatus.ACCEPTED):
    record, _receipt = store.read(target.submission_id, target)
    return make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
        status=status,
    )


def test_reopen_reconstructs_identical_views(tmp_path: Path) -> None:
    store, target, ledger = _composed(tmp_path)
    ledger.record_observation(_observation(store, target))
    ledger.record_observation(
        _observation(
            store,
            target,
            status=BrokerObservationStatus.UNKNOWN,
        )
    )
    view_before = ledger.read_attempt(target.submission_id, target)
    history_before = ledger.read_attempt_history(
        target.submission_id, target
    )
    ledger.close()
    reopened = DurableExecutionLedger(store, tmp_path / "ledger.sqlite3")
    try:
        assert (
            reopened.read_attempt(target.submission_id, target)
            == view_before
        )
        assert (
            reopened.read_attempt_history(target.submission_id, target)
            == history_before
        )
        assert reopened.read_observations(view_before.attempt_id)
    finally:
        reopened.close()
        store.close()


def test_abandoned_ledger_reconstructs_committed_evidence(
    tmp_path: Path,
) -> None:
    store, target, ledger = _composed(tmp_path)
    ledger.record_observation(_observation(store, target))
    view_before = ledger.read_attempt(target.submission_id, target)
    replacement = DurableExecutionLedger(
        store, tmp_path / "ledger.sqlite3"
    )
    try:
        assert (
            replacement.read_attempt(target.submission_id, target)
            == view_before
        )
    finally:
        replacement.close()
        ledger.close()
        store.close()


def test_repeated_reads_are_deterministic(tmp_path: Path) -> None:
    store, target, ledger = _composed(tmp_path)
    ledger.record_observation(_observation(store, target))
    first = ledger.read_attempt(target.submission_id, target)
    second = ledger.read_attempt(target.submission_id, target)
    third = ledger.read_attempt(target.submission_id, target)
    assert first == second == third
    assert first is not second
    ledger.close()
    store.close()
