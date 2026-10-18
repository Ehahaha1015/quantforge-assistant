"""Focused regressions for persisted dispatch-evidence identity binding."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from _helpers import (
    CLAIM_AT,
    CLAIM_EXPIRES_AT,
    DISPATCH_AT,
    EVIDENCE_AT,
    PREPARE_AT,
    RECOVER_RESCAN_AT,
    build_eligibility,
    claim_store,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)

from quantforge.execstate import (
    ExecutionStateCorruptionError,
    SQLiteExecutionStateStore,
)
from quantforge.frontier import FrontierEligibilityDecision


def _replace_dispatch_artifact_id(
    database: Path,
    target_json: str,
    *,
    artifact: str,
    replacement_id: str,
) -> None:
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        row = connection.execute(
            """
            SELECT dispatch_decision_json
            FROM submissions
            WHERE submission_id = (
                SELECT submission_id FROM submissions WHERE target_json = ? LIMIT 1
            )
            AND target_json = ?
            """,
            (target_json, target_json),
        ).fetchone()
        assert row is not None
        document: dict[str, Any] = json.loads(str(row[0]))
        nested = document[artifact]
        assert isinstance(nested, dict)
        nested_key = "receipt_id" if artifact == "commit_receipt" else "evidence_id"
        nested[nested_key] = replacement_id
        replacement = FrontierEligibilityDecision.from_json(
            json.dumps(document, sort_keys=True, separators=(",", ":"))
        )
        connection.execute(
            """
            UPDATE submissions
            SET dispatch_decision_json = ?
            WHERE target_json = ?
            """,
            (replacement.model_dump_json(), target_json),
        )
    finally:
        connection.close()


def _foreign_artifact_ids(
    database: Path, foreign_target_json: str
) -> tuple[str, str]:
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        receipt = connection.execute(
            """
            SELECT receipt_id
            FROM commit_receipts
            WHERE target_json = ?
            ORDER BY receipt_sequence
            LIMIT 1
            """,
            (foreign_target_json,),
        ).fetchone()
        evidence = connection.execute(
            """
            SELECT evidence_id
            FROM recovery_runs
            WHERE target_json = ?
            ORDER BY run_sequence DESC
            LIMIT 1
            """,
            (foreign_target_json,),
        ).fetchone()
        assert receipt is not None
        assert evidence is not None
        return str(receipt[0]), str(evidence[0])
    finally:
        connection.close()


def _two_target_dispatched_setup(tmp_path: Path) -> tuple[Any, Any, Any, str, str]:
    provenance = make_provenance()
    submission_id = provenance.pre_dispatch_decision.envelope.submission_id
    source_target = make_target(submission_id, account_reference="acct-source")
    foreign_target = make_target(submission_id, account_reference="acct-foreign")
    store = make_store(tmp_path)
    recover_store(store, source_target)
    source_record, source_receipt = prepare_store(
        store, provenance, source_target, prepared_at=PREPARE_AT
    )
    foreign_record, _foreign_receipt = prepare_store(
        store, provenance, foreign_target, prepared_at=PREPARE_AT
    )
    source_claim = claim_store(
        store,
        submission_id,
        source_target,
        acquired_at=CLAIM_AT,
        expires_at=CLAIM_EXPIRES_AT,
    )
    _foreign_claim = claim_store(
        store,
        submission_id,
        foreign_target,
        acquired_at=CLAIM_AT,
        expires_at=CLAIM_EXPIRES_AT,
    )
    source_recovery = recover_store(
        store, source_target, observed_at=RECOVER_RESCAN_AT
    )
    foreign_recovery = recover_store(
        store,
        foreign_target,
        observed_at=RECOVER_RESCAN_AT + timedelta(seconds=1),
    )
    source_decision = build_eligibility(
        store,
        provenance,
        source_target,
        source_record,
        source_receipt,
        source_recovery,
        source_claim,
        dispatch_at=DISPATCH_AT,
        price_at=EVIDENCE_AT,
        account_at=EVIDENCE_AT,
        operator_at=EVIDENCE_AT,
    )
    assert source_decision.eligible is True
    store.mark_dispatching(source_decision, owner_id="worker-1", token="token-1")
    database = tmp_path / "execstate.sqlite3"
    store.close()
    foreign_receipt_id, foreign_evidence_id = _foreign_artifact_ids(
        database, foreign_target.model_dump_json()
    )
    del foreign_record, foreign_recovery
    return database, source_target, source_decision, foreign_receipt_id, foreign_evidence_id


@pytest.mark.parametrize(
    ("artifact", "replacement_kind"),
    [
        ("commit_receipt", "receipt"),
        ("recovery_evidence", "evidence"),
    ],
)
def test_dispatch_decision_foreign_artifact_id_fails_closed(
    tmp_path: Path, artifact: str, replacement_kind: str
) -> None:
    (
        database,
        target,
        _decision,
        foreign_receipt_id,
        foreign_evidence_id,
    ) = _two_target_dispatched_setup(tmp_path)
    replacement_id = (
        foreign_receipt_id if replacement_kind == "receipt" else foreign_evidence_id
    )
    _replace_dispatch_artifact_id(
        database,
        target.model_dump_json(),
        artifact=artifact,
        replacement_id=replacement_id,
    )

    reopened = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            reopened.recover(target, observed_at=DISPATCH_AT + timedelta(seconds=2))
    finally:
        reopened.close()


def test_valid_dispatch_decision_survives_epoch_rebind(tmp_path: Path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, receipt = prepare_store(store, provenance, target)
    claim = claim_store(store, target.submission_id, target)
    recovery = recover_store(store, target, observed_at=RECOVER_RESCAN_AT)
    decision = build_eligibility(
        store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
    )
    assert decision.eligible is True
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    database = tmp_path / "execstate.sqlite3"
    store.close()

    reopened = SQLiteExecutionStateStore(database)
    try:
        evidence = reopened.recover(
            target, observed_at=DISPATCH_AT + timedelta(seconds=1)
        )
        assert evidence.status == "COMPLETE"
        current, current_receipt = reopened.read(target.submission_id, target)
        assert current.state == "OUTCOME_UNKNOWN"
        assert current.recovery_epoch == evidence.recovery_epoch
        assert current_receipt.recovery_epoch == evidence.recovery_epoch
    finally:
        reopened.close()
