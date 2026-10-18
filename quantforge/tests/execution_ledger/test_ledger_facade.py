from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from _ledger_support import (
    LATE_AT,
    RECOVER_AT,
    advance_to_acknowledged,
    advance_to_dispatching,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)
from _support import make_observation

from quantforge.execstate import (
    ExecutionStateRecoveryRequiredError,
    SQLiteExecutionStateStore,
)
from quantforge.execution_ledger import (
    AttemptPhase,
    BrokerObservationStatus,
    ExecutionLedgerDeniedError,
    ExecutionLedgerNotFoundError,
    ReconciliationConflict,
    ReconciliationReason,
    ReconciliationResult,
)
from quantforge.execution_ledger.ledger import DurableExecutionLedger


def _composed(tmp_path: Path, stage: str):
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path / "state")
    recover_store(store, target, observed_at=RECOVER_AT)
    if stage == "prepared":
        prepare_store(store, provenance, target)
    elif stage == "dispatching":
        advance_to_dispatching(store, provenance, target)
    elif stage == "acknowledged":
        advance_to_acknowledged(store, provenance, target)
    elif stage != "recovered":
        raise ValueError(stage)
    ledger = DurableExecutionLedger(store, tmp_path / "ledger.sqlite3")
    return store, provenance, target, ledger


def _record(store: SQLiteExecutionStateStore, target) -> tuple:
    return store.read(target.submission_id, target)


def _observation_rows(path: Path) -> int:
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_prepared_observation_conflicts_without_mutation(tmp_path: Path) -> None:
    store, _provenance, target, ledger = _composed(tmp_path, "prepared")
    record, _receipt = _record(store, target)
    observation = make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
    )
    assessment = ledger.assess_observation(observation)
    assert assessment.result is ReconciliationResult.CONFLICT
    assert assessment.reason is (
        ReconciliationReason.CONFLICT_NO_DISPATCH_RECORD
    )
    assert assessment.recorded is False
    with pytest.raises(ReconciliationConflict):
        ledger.record_observation(observation)
    assert _observation_rows(tmp_path / "ledger.sqlite3") == 0
    assert ledger.read_attempt(target.submission_id, target).phase is (
        AttemptPhase.PREPARED
    )
    ledger.close()
    store.close()


def test_dispatch_observation_records_and_projects(tmp_path: Path) -> None:
    store, _provenance, target, ledger = _composed(
        tmp_path, "dispatching"
    )
    record, _receipt = _record(store, target)
    observation = make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
    )
    assessment = ledger.assess_observation(observation)
    assert assessment.result is ReconciliationResult.COMPATIBLE
    assert assessment.reason is (
        ReconciliationReason.COMPATIBLE_ACCEPTED_ON_DISPATCHING
    )
    recorded = ledger.record_observation(observation)
    assert recorded.recorded is True
    assert recorded.observation_id == "execstate-observation-v1-1"
    view = ledger.read_attempt(target.submission_id, target)
    assert view.phase is AttemptPhase.OBSERVED
    assert view.phase_reason == "OBSERVED_DISPATCHING_WITH_OBSERVATION"
    assert view.execstate_state == "DISPATCHING"
    assert view.observation_ids == ("execstate-observation-v1-1",)
    assert view.observation_count == 1
    assert ledger.read_attempt_history(target.submission_id, target) == (
        view,
    )
    history = ledger.read_observations(view.attempt_id)
    assert len(history) == 1
    assert history[0] == dataclasses.replace(
        observation, observation_id="execstate-observation-v1-1"
    )
    replay = ledger.record_observation(history[0])
    assert replay.observation_id == "execstate-observation-v1-2"
    assert len(ledger.read_observations(view.attempt_id)) == 2
    assert _observation_rows(tmp_path / "ledger.sqlite3") == 2
    ledger.close()
    store.close()


def test_unknown_observation_is_sticky_in_projection(tmp_path: Path) -> None:
    store, _provenance, target, ledger = _composed(
        tmp_path, "dispatching"
    )
    record, _receipt = _record(store, target)
    unknown = make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
        status=BrokerObservationStatus.UNKNOWN,
    )
    assessment = ledger.assess_observation(unknown)
    assert assessment.result is ReconciliationResult.UNKNOWN
    assert assessment.reason is ReconciliationReason.UNKNOWN_BROKER_UNKNOWN
    ledger.record_observation(unknown)
    view = ledger.read_attempt(target.submission_id, target)
    assert view.phase is AttemptPhase.UNKNOWN
    assert view.phase_reason == "UNKNOWN_RECORDED_OBSERVATION"
    accepted = make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
        broker_reference="broker-ref-2",
    )
    ledger.record_observation(accepted)
    assert ledger.read_attempt(target.submission_id, target).phase is (
        AttemptPhase.UNKNOWN
    )
    ledger.close()
    store.close()


def test_acknowledged_conflict_has_zero_mutation(tmp_path: Path) -> None:
    store, _provenance, target, ledger = _composed(
        tmp_path, "acknowledged"
    )
    record, _receipt = _record(store, target)
    rejected = make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
        status=BrokerObservationStatus.REJECTED,
        broker_reference="broker-ref-2",
    )
    assessment = ledger.assess_observation(rejected)
    assert assessment.result is ReconciliationResult.CONFLICT
    assert assessment.reason is (
        ReconciliationReason.CONFLICT_TERMINAL_ACCEPTED_VS_REJECTED
    )
    with pytest.raises(ReconciliationConflict):
        ledger.record_observation(rejected)
    assert _observation_rows(tmp_path / "ledger.sqlite3") == 0
    assert _record(store, target)[0].state == "ACKNOWLEDGED"
    ledger.close()
    store.close()


def test_historical_attempt_assessment_after_epoch_rebind(
    tmp_path: Path,
) -> None:
    store, _provenance, target, ledger = _composed(
        tmp_path, "dispatching"
    )
    record, _receipt = _record(store, target)
    first = make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
    )
    stored = ledger.record_observation(first)
    ledger.close()
    store.close()
    execstate_path = tmp_path / "state" / "execstate.sqlite3"
    reopened_store = SQLiteExecutionStateStore(execstate_path)
    recover_store(reopened_store, target, observed_at=LATE_AT)
    history_ledger = DurableExecutionLedger(
        reopened_store, tmp_path / "ledger.sqlite3"
    )
    try:
        views = history_ledger.read_attempt_history(
            target.submission_id, target
        )
        assert len(views) == 2
        assert views[0].recovery_epoch == record.recovery_epoch
        assert views[1].recovery_epoch == record.recovery_epoch + 1
        assert views[0].phase is AttemptPhase.OBSERVED
        assert views[1].phase is AttemptPhase.UNKNOWN
        assert views[0].observation_ids == (stored.observation_id,)
        assessment = history_ledger.assess_observation(first)
        assert assessment.result is ReconciliationResult.COMPATIBLE
        assert assessment.reason is (
            ReconciliationReason.COMPATIBLE_ACCEPTED_ON_DISPATCHING
        )
    finally:
        history_ledger.close()
        reopened_store.close()


def test_not_found_mapping_and_empty_observations(tmp_path: Path) -> None:
    store, provenance, _target, ledger = _composed(tmp_path, "prepared")
    unknown_target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id,
        account_reference="acct-other",
    )
    with pytest.raises(ExecutionLedgerNotFoundError):
        ledger.read_attempt(unknown_target.submission_id, unknown_target)
    assert (
        ledger.read_observations("execstate-attempt-v1-" + "0" * 32) == ()
    )
    ledger.close()
    store.close()


def test_unrecovered_store_fails_closed(tmp_path: Path) -> None:
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    store = make_store(tmp_path / "state")
    ledger = DurableExecutionLedger(store, tmp_path / "ledger.sqlite3")
    observation = make_observation(
        submission_id=target.submission_id, target=target
    )
    with pytest.raises(ExecutionStateRecoveryRequiredError):
        ledger.assess_observation(observation)
    assert _observation_rows(tmp_path / "ledger.sqlite3") == 0
    ledger.close()
    store.close()


def test_close_does_not_close_execstate(tmp_path: Path) -> None:
    store, _provenance, target, ledger = _composed(
        tmp_path, "dispatching"
    )
    ledger.close()
    record, _receipt = _record(store, target)
    assert record.state == "DISPATCHING"
    with pytest.raises(ExecutionLedgerDeniedError):
        ledger.read_attempt(target.submission_id, target)
    store.close()
