from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from _helpers import (
    DISPATCH_AT,
    OUTCOME_AT,
    build_eligibility,
    make_provenance,
    make_store,
    make_target,
    prepare_claim_recover,
    prepare_store,
    recover_store,
)

from quantforge.broker import BrokerSubmissionOutcome
from quantforge.execstate import (
    ExecutionStateDeniedError,
    SQLiteExecutionStateStore,
)
from quantforge.frontier import evaluate_frontier_eligibility


def _acknowledge(
    store: SQLiteExecutionStateStore,
    provenance,
    target,
    record,
    receipt,
    recovery,
    claim,
) -> None:
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    store.persist_transport_outcome(
        BrokerSubmissionOutcome(
            submission_id=record.submission_id,
            status="ACCEPTED",
        ),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )


def test_m_acknowledged_survives_restart(tmp_path) -> None:
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
    _acknowledge(store, provenance, target, record, receipt, recovery, claim)
    store.close()

    reopened = make_store(tmp_path)
    recover_store(reopened, target, observed_at=datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC))
    new_record, _receipt = reopened.read(envelope.submission_id, target)
    assert new_record.state == "ACKNOWLEDGED"
    with pytest.raises(ExecutionStateDeniedError):
        reopened.claim(
            envelope.submission_id,
            target,
            owner_id="worker-2",
            token="token-2",
            acquired_at=OUTCOME_AT,
            expires_at=datetime(2030, 1, 1, 12, 3, tzinfo=UTC),
        )
    reopened.close()


def test_n_rejected_survives_restart(tmp_path) -> None:
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
            status="REJECTED",
        ),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    store.close()

    reopened = make_store(tmp_path)
    recover_store(reopened, target, observed_at=datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC))
    new_record, _receipt = reopened.read(envelope.submission_id, target)
    assert new_record.state == "REJECTED"
    with pytest.raises(ExecutionStateDeniedError):
        reopened.claim(
            envelope.submission_id,
            target,
            owner_id="worker-2",
            token="token-2",
            acquired_at=OUTCOME_AT,
            expires_at=datetime(2030, 1, 1, 12, 3, tzinfo=UTC),
        )
    reopened.close()


def test_o_recovery_scans_all_records_across_targets(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target_a = make_target(envelope.submission_id, account_reference="acct-a")
    target_b = make_target(envelope.submission_id, account_reference="acct-b")
    store = make_store(tmp_path)
    recover_store(store, target_a)
    prepare_store(store, provenance, target_a)
    prepare_store(store, provenance, target_b)
    evidence = recover_store(store, target_a, observed_at=OUTCOME_AT)
    assert evidence.scanned_through_generation == 2
    assert evidence.unresolved_submission_ids == ()
    store.close()


def test_p_recovery_evidence_is_target_bound_and_generation_covering(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    evidence = recover_store(store, target, observed_at=OUTCOME_AT)
    assert evidence.schema == "frontier-recovery-v1"
    assert evidence.target == target
    assert evidence.status == "COMPLETE"
    assert evidence.recovery_epoch == 1
    assert evidence.scanned_through_generation >= 1
    assert evidence.unresolved_submission_ids == ()
    store.close()


def test_q_unresolved_unknown_blocks_frontier(tmp_path) -> None:
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
    evidence = recover_store(store, target, observed_at=datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC))
    assert evidence.unresolved_submission_ids == (envelope.submission_id,)
    denied = evaluate_frontier_eligibility(
        provenance,
        DISPATCH_AT,
        target,
        price_evidence=decision.price_evidence,
        account_evidence=decision.account_evidence,
        durable_record=record,
        commit_receipt=receipt,
        recovery_evidence=evidence,
        operator_evidence=decision.operator_evidence,
        capability=decision.capability,
        claim=claim,
    )
    assert denied.eligible is False
    store.close()


def test_ai_uncommitted_transaction_does_not_persist(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, _receipt = prepare_store(store, provenance, target)
    second = sqlite3.connect(
        str(store._path), timeout=0.0, isolation_level=None, check_same_thread=True
    )
    second.execute("PRAGMA foreign_keys = ON")
    second.execute("BEGIN IMMEDIATE")
    second.execute(
        "UPDATE submissions SET state='REJECTED' WHERE submission_id=? AND target_json=?",
        (record.submission_id, target.model_dump_json()),
    )
    second.rollback()
    second.close()
    persisted, _ = store.read(record.submission_id, target)
    assert persisted.state == "PREPARED"
    store.close()


def test_ak_receipt_epoch_after_restart(tmp_path) -> None:
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
    old_decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    assert old_decision.eligible is True
    store.close()

    reopened = make_store(tmp_path)
    new_recovery = recover_store(reopened, target, observed_at=OUTCOME_AT)
    new_record, new_receipt = reopened.read(envelope.submission_id, target)
    assert new_record.recovery_epoch == new_recovery.recovery_epoch
    assert new_receipt.recovery_epoch == new_record.recovery_epoch
    assert new_receipt.generation == new_record.generation
    with pytest.raises(ExecutionStateDeniedError):
        reopened.mark_dispatching(old_decision, owner_id="worker-1", token="token-1")
    reopened.close()


def test_am_empty_database_recovery(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    evidence = recover_store(store, target)
    assert evidence.scanned_through_generation == 0
    assert evidence.unresolved_submission_ids == ()
    assert evidence.status == "COMPLETE"
    assert store._connection.execute(
        "SELECT COUNT(*) FROM submissions"
    ).fetchone()[0] == 0
    assert store._connection.execute(
        "SELECT COUNT(*) FROM recovery_runs"
    ).fetchone()[0] == 1
    assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()


def test_an_recovery_target_a_cannot_authorize_target_b(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target_a = make_target(envelope.submission_id, account_reference="acct-a")
    target_b = make_target(envelope.submission_id, account_reference="acct-b")
    store = make_store(tmp_path)
    evidence_a = recover_store(store, target_a)
    assert evidence_a.target == target_a
    assert evidence_a.target != target_b
    store.close()


def test_ao_foreign_key_relationships(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            """
            INSERT INTO commit_receipts(
                receipt_id, record_id, submission_id, target_json,
                generation, recovery_epoch, receipt_json, committed,
                committed_at
            ) VALUES ('foreign-receipt', 'missing-record', 'missing',
                      '{}', 1, 1, '{}', 1, '2030-01-01T00:00:00.000000Z')
            """
        )
    store._rollback()
    store.close()
