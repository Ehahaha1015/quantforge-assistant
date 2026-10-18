from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest
from _helpers import (
    CLAIM_AT,
    CLAIM_EXPIRES_AT,
    EVIDENCE_AT,
    OUTCOME_AT,
    claim_store,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)

from quantforge.execstate import (
    ExecutionStateCorruptionError,
    ExecutionStateDeniedError,
)


def _database(tmp_path):
    return tmp_path / "execstate.sqlite3"


def _append_claim_conflict_after_revocation(database, *, claim_id: str) -> None:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    prepared = connection.execute(
        "SELECT * FROM submissions ORDER BY generation LIMIT 1"
    ).fetchone()
    assert prepared is not None
    sequence = int(
        connection.execute("SELECT MAX(event_sequence) FROM audit_events").fetchone()[0]
    ) + 1
    event = {
        "event_sequence": sequence,
        "row_scope": "SUBMISSION",
        "submission_id": prepared["submission_id"],
        "target_json": prepared["target_json"],
        "prior_state": "PREPARED",
        "new_state": "PREPARED",
        "generation": prepared["generation"],
        "recovery_epoch": prepared["recovery_epoch"],
        "event_kind": "CLAIM_CONFLICT",
        "observed_at": "2030-01-01T12:02:00.000000Z",
        "owner_id": "worker-2",
        "token_reference": claim_id,
        "claim_id": claim_id,
        "payload_sequence": None,
        "status": None,
        "reason": "CLAIM_CONFLICT",
    }
    event_json = json.dumps(event, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """
        INSERT INTO audit_events(
            event_sequence, row_scope, submission_id, target_json, prior_state,
            new_state, generation, recovery_epoch, event_kind, reason,
            observed_at, owner_id, token_reference, claim_id,
            payload_sequence, status, event_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sequence,
            event["row_scope"],
            event["submission_id"],
            event["target_json"],
            event["prior_state"],
            event["new_state"],
            event["generation"],
            event["recovery_epoch"],
            event["event_kind"],
            event["reason"],
            event["observed_at"],
            event["owner_id"],
            event["token_reference"],
            event["claim_id"],
            event["payload_sequence"],
            event["status"],
            event_json,
        ),
    )
    connection.close()


def test_revoked_claim_cannot_appear_in_later_conflict_history(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    first = make_store(tmp_path)
    recover_store(first, target)
    record, _ = prepare_store(first, provenance, target)
    claim = claim_store(
        first,
        record.submission_id,
        target,
        acquired_at=CLAIM_AT,
        expires_at=CLAIM_EXPIRES_AT,
    )
    first.close()

    second = make_store(tmp_path)
    recover_store(second, target, observed_at=OUTCOME_AT)
    second.close()
    _append_claim_conflict_after_revocation(_database(tmp_path), claim_id=claim.claim_id)

    reopened = make_store(tmp_path)
    with pytest.raises(ExecutionStateCorruptionError):
        reopened.recover(target, observed_at=OUTCOME_AT + timedelta(seconds=1))
    reopened.close()


def test_revocation_then_new_claim_and_conflict_is_valid_history(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    first = make_store(tmp_path)
    recover_store(first, target)
    record, _ = prepare_store(first, provenance, target)
    claim_store(
        first,
        record.submission_id,
        target,
        acquired_at=CLAIM_AT,
        expires_at=CLAIM_EXPIRES_AT,
    )
    first.close()

    second = make_store(tmp_path)
    recover_store(second, target, observed_at=OUTCOME_AT)
    replacement = claim_store(
        second,
        record.submission_id,
        target,
        owner_id="worker-2",
        token="token-2",
        acquired_at=OUTCOME_AT,
        expires_at=OUTCOME_AT + timedelta(seconds=10),
    )
    with pytest.raises(ExecutionStateDeniedError):
        claim_store(
            second,
            record.submission_id,
            target,
            owner_id="worker-3",
            token="token-3",
            acquired_at=OUTCOME_AT,
            expires_at=OUTCOME_AT + timedelta(seconds=10),
        )
    second.close()

    reopened = make_store(tmp_path)
    recovery = reopened.recover(
        target, observed_at=OUTCOME_AT + timedelta(seconds=20)
    )
    assert recovery.status == "COMPLETE"
    current, _ = reopened.read(record.submission_id, target)
    assert current.state == "PREPARED"
    assert replacement.claim_id
    reopened.close()


def test_lost_active_claim_pointer_fails_closed(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, _ = prepare_store(store, provenance, target)
    claim = claim_store(store, record.submission_id, target)
    store.close()

    connection = sqlite3.connect(str(_database(tmp_path)), isolation_level=None)
    connection.execute(
        "UPDATE submissions SET active_claim_id = NULL WHERE record_id = ?",
        (record.record_id,),
    )
    connection.close()

    reopened = make_store(tmp_path)
    with pytest.raises(ExecutionStateCorruptionError):
        reopened.recover(target, observed_at=EVIDENCE_AT)
    reopened.close()
    assert claim.claim_id


def test_dispatch_claim_owner_or_token_substitution_is_denied(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, receipt = prepare_store(store, provenance, target)
    claim = claim_store(store, record.submission_id, target)
    recovery = store.recover(target, observed_at=EVIDENCE_AT)
    from _helpers import build_eligibility

    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    replacement = type(claim)(
        **{
            **{name: getattr(claim, name) for name in type(claim).model_fields},
            "owner_id": "worker-evil",
        }
    )
    forged = decision.model_copy(update={"claim": replacement})
    with pytest.raises(ExecutionStateDeniedError):
        store.mark_dispatching(forged, owner_id="worker-1", token="token-1")
    with pytest.raises(ExecutionStateDeniedError):
        store.mark_dispatching(decision, owner_id="worker-evil", token="token-1")
    store.close()


def test_dispatch_decision_binds_the_active_claim_on_success(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, receipt = prepare_store(store, provenance, target)
    claim = claim_store(store, record.submission_id, target)
    recovery = store.recover(target, observed_at=EVIDENCE_AT)
    from _helpers import build_eligibility

    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    dispatched = store.mark_dispatching(
        decision, owner_id=claim.owner_id, token=claim.token
    )
    row = store._connection.execute(
        "SELECT dispatch_decision_json, active_claim_id FROM submissions WHERE record_id = ?",
        (record.record_id,),
    ).fetchone()
    assert row["active_claim_id"] == claim.claim_id
    stored_decision = json.loads(row["dispatch_decision_json"])
    assert stored_decision["claim"] == json.loads(claim.model_dump_json())
    assert dispatched.state == "DISPATCHING"
    store.close()


@pytest.mark.parametrize("field", ["owner_id", "token"])
def test_stored_dispatch_decision_claim_substitution_fails_recovery(
    tmp_path, field: str
) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, receipt = prepare_store(store, provenance, target)
    claim = claim_store(store, record.submission_id, target)
    recovery = store.recover(target, observed_at=EVIDENCE_AT)
    from _helpers import build_eligibility

    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    store.mark_dispatching(decision, owner_id=claim.owner_id, token=claim.token)
    database = _database(tmp_path)
    store.close()

    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT dispatch_decision_json FROM submissions WHERE record_id = ?",
            (record.record_id,),
        ).fetchone()
        assert row is not None
        document = json.loads(str(row["dispatch_decision_json"]))
        document["claim"][field] = "forged-owner" if field == "owner_id" else "forged-token"
        forged_json = json.dumps(document, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "UPDATE submissions SET dispatch_decision_json = ? WHERE record_id = ?",
            (forged_json, record.record_id),
        )
    finally:
        connection.close()

    reopened = make_store(tmp_path)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            reopened.recover(target, observed_at=OUTCOME_AT)
    finally:
        reopened.close()
