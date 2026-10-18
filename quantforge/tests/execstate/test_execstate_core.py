from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _helpers import (
    CLAIM_AT,
    CLAIM_EXPIRES_AT,
    DISPATCH_AT,
    OUTCOME_AT,
    PREPARE_AT,
    build_eligibility,
    claim_store,
    make_provenance,
    make_store,
    make_target,
    prepare_claim_recover,
    prepare_store,
    recover_store,
)

from quantforge.broker import BrokerSubmissionOutcome
from quantforge.execstate import (
    ExecutionStateCommitUnknownError,
    ExecutionStateConflictError,
    ExecutionStateDeniedError,
    ExecutionStateNotFoundError,
    SQLiteExecutionStateStore,
)
from quantforge.risk import RiskLimits


def test_a_create_commit_prepared(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, receipt = prepare_store(store, provenance, target)

    assert record.state == "PREPARED"
    assert record.generation == 1
    assert record.recovery_epoch == 1
    assert record.created_at == PREPARE_AT
    assert record.updated_at == PREPARE_AT
    assert receipt.committed is True
    assert receipt.committed_at == PREPARE_AT
    assert receipt.generation == 1
    assert receipt.recovery_epoch == 1
    stored_record, stored_receipt = store.read(record.submission_id, target)
    assert stored_record == record
    assert stored_receipt == receipt
    store.close()


def test_b_duplicate_prepare_replays(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    first_record, first_receipt = prepare_store(store, provenance, target)
    audit_before = store.read_audit(first_record.submission_id, target)

    second_record, second_receipt = prepare_store(store, provenance, target)
    audit_after = store.read_audit(first_record.submission_id, target)

    assert second_record == first_record
    assert second_receipt == first_receipt
    assert len(audit_after) == len(audit_before) + 1
    assert '"reason":"DUPLICATE_PREPARE_REPLAY"' in audit_after[-1]
    store.close()


def test_b_contradictory_duplicate_prepare_is_conflict(tmp_path) -> None:
    """Oracle B: a contradictory duplicate prepare must be a conflict."""
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, receipt = prepare_store(store, provenance, target)

    with pytest.raises(ExecutionStateConflictError):
        store.prepare(
            envelope,
            provenance,
            target,
            prepared_at=PREPARE_AT + timedelta(seconds=1),
        )

    contradictory = make_provenance(
        limits=RiskLimits(
            max_order_notional=Decimal("999.00"),
            max_position_notional=Decimal("1000.0"),
            max_gross_exposure=Decimal("2000.000"),
        )
    )
    assert contradictory.provenance_id != provenance.provenance_id
    assert contradictory.pre_dispatch_decision.eligible is True
    with pytest.raises(ExecutionStateConflictError):
        store.prepare(envelope, contradictory, target, prepared_at=PREPARE_AT)

    stored, stored_receipt = store.read(envelope.submission_id, target)
    assert stored == record
    assert stored_receipt == receipt
    assert stored.generation == 1
    assert store._connection.execute(
        "SELECT COUNT(*) FROM commit_receipts"
    ).fetchone()[0] == 1
    reasons = [
        json.loads(value)["reason"]
        for value in store.read_audit(envelope.submission_id, target)
    ]
    assert reasons == ["NEW_PREPARE"]
    store.close()


def test_b_prepare_after_dispatch_is_denied(tmp_path) -> None:
    """Oracle B: a byte-identical prepare never resets a non-PREPARED row."""
    (
        store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
        envelope,
    ) = prepare_claim_recover(tmp_path)
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")

    with pytest.raises(ExecutionStateDeniedError):
        store.prepare(envelope, provenance, target, prepared_at=PREPARE_AT)

    stored, _stored_receipt = store.read(envelope.submission_id, target)
    assert stored.state == "DISPATCHING"
    assert stored.generation == record.generation
    assert store._connection.execute(
        "SELECT COUNT(*) FROM submissions"
    ).fetchone()[0] == 1
    store.close()


class _ControlledCommitStore(SQLiteExecutionStateStore):
    def __init__(self, path) -> None:
        self.fail_next_commit = False
        super().__init__(path)

    def _commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise ExecutionStateCommitUnknownError("forced unknown commit")
        super()._commit()


def test_c_rollback_and_uncommitted_transaction(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = _ControlledCommitStore(tmp_path / "execstate.sqlite3")
    recover_store(store, target)
    store.fail_next_commit = True
    with pytest.raises(ExecutionStateCommitUnknownError):
        prepare_store(store, provenance, target)
    store.close()

    reopened = make_store(tmp_path)
    recover_store(reopened, target)
    with pytest.raises(ExecutionStateNotFoundError):
        reopened.read(provenance.pre_dispatch_decision.envelope.submission_id, target)
    reopened.close()


def test_d_first_claim(tmp_path) -> None:
    (
        store,
        _provenance,
        target,
        _record,
        _receipt,
        _recovery,
        claim,
        envelope,
    ) = prepare_claim_recover(tmp_path)
    assert claim.status == "ACQUIRED"
    assert claim.submission_id == envelope.submission_id
    assert claim.target == target
    assert claim.generation == 1
    assert claim.recovery_epoch == 1
    assert claim.owner_id == "worker-1"
    assert claim.token == "token-1"
    assert claim.acquired_at == CLAIM_AT
    assert claim.expires_at == CLAIM_EXPIRES_AT
    store.close()


def test_f_wrong_target_is_denied(tmp_path) -> None:
    (
        store,
        _provenance,
        _target,
        _record,
        _receipt,
        _recovery,
        _claim,
        envelope,
    ) = prepare_claim_recover(tmp_path)
    wrong_target = make_target(envelope.submission_id, account_reference="acct-8")
    with pytest.raises(ExecutionStateNotFoundError):
        store.claim(
            envelope.submission_id,
            wrong_target,
            owner_id="worker-2",
            token="token-2",
            acquired_at=CLAIM_AT,
            expires_at=CLAIM_EXPIRES_AT,
        )
    store.close()


def test_g_wrong_generation_is_denied(tmp_path) -> None:
    (
        store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
        _envelope,
    ) = prepare_claim_recover(tmp_path)
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    wrong_claim = type(claim)(
        **{
            **{
                name: getattr(claim, name)
                for name in type(claim).model_fields
            },
            "generation": claim.generation + 1,
        }
    )
    from quantforge.frontier import evaluate_frontier_eligibility

    wrong_generation = evaluate_frontier_eligibility(
        provenance,
        DISPATCH_AT,
        target,
        price_evidence=decision.price_evidence,
        account_evidence=decision.account_evidence,
        durable_record=record,
        commit_receipt=receipt,
        recovery_evidence=recovery,
        operator_evidence=decision.operator_evidence,
        capability=decision.capability,
        claim=wrong_claim,
    )
    assert wrong_generation.eligible is False
    with pytest.raises(ExecutionStateDeniedError):
        store.mark_dispatching(wrong_generation, owner_id="worker-1", token="token-1")
    store.close()


def test_h_stale_epoch_is_denied(tmp_path) -> None:
    (
        store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
        _envelope,
    ) = prepare_claim_recover(tmp_path)
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    store.close()
    reopened = make_store(tmp_path)
    recover_store(reopened, target, observed_at=OUTCOME_AT)
    with pytest.raises(ExecutionStateDeniedError):
        reopened.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    reopened.close()


def test_i_expired_lease_never_redispatches(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    claim_store(
        store,
        envelope.submission_id,
        target,
        acquired_at=CLAIM_AT,
        expires_at=DISPATCH_AT,
    )
    with pytest.raises(ExecutionStateDeniedError):
        claim_store(
            store,
            envelope.submission_id,
            target,
            owner_id="worker-2",
            token="token-2",
            acquired_at=CLAIM_AT,
            expires_at=CLAIM_EXPIRES_AT,
        )
    store.close()

    reopened = make_store(tmp_path)
    new_recovery = recover_store(reopened, target, observed_at=OUTCOME_AT)
    new_record, new_receipt = reopened.read(envelope.submission_id, target)
    new_claim = reopened.claim(
        envelope.submission_id,
        target,
        owner_id="worker-2",
        token="token-2",
        acquired_at=OUTCOME_AT,
        expires_at=CLAIM_EXPIRES_AT,
    )
    assert new_claim.owner_id == "worker-2"
    assert new_claim.recovery_epoch == new_record.recovery_epoch
    assert new_recovery.recovery_epoch == new_record.recovery_epoch
    assert new_receipt.recovery_epoch == new_record.recovery_epoch
    reopened.close()


def test_j_prepared_survives_restart(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, _receipt = prepare_store(store, provenance, target)
    store.close()

    reopened = make_store(tmp_path)
    recovery = recover_store(reopened, target, observed_at=OUTCOME_AT)
    new_record, new_receipt = reopened.read(record.submission_id, target)
    assert new_record.state == "PREPARED"
    assert new_record.generation == record.generation
    assert new_record.recovery_epoch == recovery.recovery_epoch
    assert new_receipt.recovery_epoch == recovery.recovery_epoch
    assert new_receipt.generation == record.generation
    reopened.close()


def test_k_dispatching_restart_becomes_unknown(tmp_path) -> None:
    (
        store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
        envelope,
    ) = prepare_claim_recover(tmp_path)
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    store.close()

    reopened = make_store(tmp_path)
    recover_store(reopened, target, observed_at=OUTCOME_AT)
    new_record, _new_receipt = reopened.read(envelope.submission_id, target)
    assert new_record.state == "OUTCOME_UNKNOWN"
    reopened.close()


def test_l_unknown_survives_restart(tmp_path) -> None:
    (
        store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
        envelope,
    ) = prepare_claim_recover(tmp_path)
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    store.persist_transport_outcome(
        BrokerSubmissionOutcome(
            submission_id=envelope.submission_id,
            status="OUTCOME_UNKNOWN",
        ),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    store.close()

    reopened = make_store(tmp_path)
    recovery = recover_store(reopened, target, observed_at=datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC))
    new_record, _new_receipt = reopened.read(envelope.submission_id, target)
    assert new_record.state == "OUTCOME_UNKNOWN"
    assert envelope.submission_id in recovery.unresolved_submission_ids
    with pytest.raises(ExecutionStateDeniedError):
        reopened.claim(
            envelope.submission_id,
            target,
            owner_id="worker-2",
            token="token-2",
            acquired_at=OUTCOME_AT,
            expires_at=CLAIM_EXPIRES_AT,
        )
    reopened.close()
