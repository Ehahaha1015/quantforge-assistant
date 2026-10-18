from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from _ledger_support import (
    RECOVER_AT,
    RECOVER_RESCAN_AT,
    claim_store,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)

from quantforge.execstate import (
    ExecutionStateConflictError,
    ExecutionStateDeniedError,
)
from quantforge.execution_ledger.identity import derive_attempt_id
from quantforge.execution_ledger.ledger import DurableExecutionLedger


def _independent_attempt_id(
    submission_id: str,
    target_json: str,
    generation: int,
    recovery_epoch: int,
) -> str:
    payload = {
        "submission_id": submission_id,
        "target_json": target_json,
        "generation": generation,
        "recovery_epoch": recovery_epoch,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "execstate-attempt-v1-" + hashlib.sha256(canonical).hexdigest()[
        :32
    ]


def _snapshot(database: Path) -> tuple[list[tuple], list[tuple], list[tuple]]:
    connection = sqlite3.connect(str(database))
    try:
        submissions = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM submissions ORDER BY generation"
            )
        ]
        claims = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM claims ORDER BY claim_sequence"
            )
        ]
        audit = [
            tuple(row)
            for row in connection.execute(
                "SELECT event_sequence, event_kind, reason FROM audit_events "
                "ORDER BY event_sequence"
            )
        ]
        return submissions, claims, audit
    finally:
        connection.close()


def test_oracle_d_duplicate_attempt_authority(tmp_path: Path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    directory = tmp_path / "state"
    store = make_store(directory)
    recover_store(store, target, observed_at=RECOVER_AT)
    database = directory / "execstate.sqlite3"
    sidecar = tmp_path / "ledger.sqlite3"
    record_one, receipt_one = prepare_store(store, provenance, target)
    record_two, receipt_two = prepare_store(store, provenance, target)
    assert record_two == record_one
    assert receipt_two == receipt_one
    submissions, claims, audit = _snapshot(database)
    assert len(submissions) == 1
    assert claims == []
    assert [row[2] for row in audit if row[1] == "PREPARED"] == [
        "NEW_PREPARE",
        "DUPLICATE_PREPARE_REPLAY",
    ]

    before_conflict = _snapshot(database)
    with pytest.raises(ExecutionStateConflictError):
        store.prepare(
            envelope,
            provenance,
            target,
            prepared_at=RECOVER_RESCAN_AT,
        )
    assert _snapshot(database) == before_conflict

    claim_one = claim_store(store, envelope.submission_id, target)
    claim_replay = claim_store(store, envelope.submission_id, target)
    assert claim_replay == claim_one
    with pytest.raises(ExecutionStateDeniedError):
        claim_store(
            store,
            envelope.submission_id,
            target,
            owner_id="worker-2",
            token="token-2",
        )
    submissions, claims, _audit = _snapshot(database)
    assert len(claims) == 1
    assert len(submissions) == 1
    assert submissions[0][11] == claim_one.claim_id

    ledger = DurableExecutionLedger(store, sidecar)
    try:
        view = ledger.read_attempt(envelope.submission_id, target)
        history = ledger.read_attempt_history(
            envelope.submission_id, target
        )
        observations = ledger.read_observations(view.attempt_id)
    finally:
        ledger.close()
        store.close()
    expected_attempt = _independent_attempt_id(
        envelope.submission_id,
        target.model_dump_json(),
        record_one.generation,
        record_one.recovery_epoch,
    )
    assert view.attempt_id == expected_attempt
    assert (
        derive_attempt_id(
            envelope.submission_id,
            target.model_dump_json(),
            record_one.generation,
            record_one.recovery_epoch,
        )
        == expected_attempt
    )
    assert len(history) == 1
    assert history[0].attempt_id == expected_attempt
    assert observations == ()
    assert view.attempt_id.startswith("execstate-attempt-v1-")
    assert len(view.attempt_id.removeprefix("execstate-attempt-v1-")) == 32
    assert claim_one.claim_id != view.attempt_id
