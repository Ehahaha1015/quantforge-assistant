from __future__ import annotations

import json
import sqlite3

import pytest
from _helpers import (
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
    ExecutionStateBusyError,
    ExecutionStateCorruptionError,
    ExecutionStateSchemaError,
    SQLiteExecutionStateStore,
)


def test_x_database_contention_fails_closed(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    blocker = sqlite3.connect(
        str(store._path),
        timeout=0.0,
        isolation_level=None,
        check_same_thread=True,
    )
    blocker.execute("PRAGMA foreign_keys = ON")
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ExecutionStateBusyError):
            prepare_store(store, provenance, target)
    finally:
        blocker.rollback()
        blocker.close()
    store.close()


def test_y_schema_version_corruption_fails_closed(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    store.close()
    database = tmp_path / "execstate.sqlite3"
    connection = sqlite3.connect(str(database))
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    with pytest.raises(ExecutionStateSchemaError):
        SQLiteExecutionStateStore(database)


def test_z_record_artifact_corruption_fails_closed(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, _receipt = prepare_store(store, provenance, target)
    store._connection.execute(
        "UPDATE submissions SET record_json = ? WHERE submission_id = ? AND target_json = ?",
        ("{}", record.submission_id, target.model_dump_json()),
    )
    with pytest.raises(ExecutionStateCorruptionError):
        recover_store(store, target, observed_at=target_observed_at())
    store.close()


def test_ab_claim_tamper_fails_closed(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    from _helpers import claim_store

    claim = claim_store(store, target.submission_id, target)
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "UPDATE claims SET owner_id = 'tampered' WHERE claim_id = ?",
            (claim.claim_id,),
        )
    store.close()


def test_ab_receipt_tamper_fails_closed(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    _record, receipt = prepare_store(store, provenance, target)
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "UPDATE commit_receipts SET committed_at = ? WHERE receipt_id = ?",
            ("1999-01-01T00:00:00.000000Z", receipt.receipt_id),
        )
    store.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("provenance_json", "{}"),
        ("generation", 999),
        ("recovery_epoch", 999),
    ],
)
def test_ab_submission_artifact_tamper_fails_closed(
    tmp_path, column: str, value: object
) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, _receipt = prepare_store(store, provenance, target)
    store._connection.execute(
        f"UPDATE submissions SET {column} = ? WHERE submission_id = ? AND target_json = ?",
        (value, record.submission_id, target.model_dump_json()),
    )
    with pytest.raises(ExecutionStateCorruptionError):
        recover_store(store, target, observed_at=target_observed_at())
    store.close()


def target_observed_at():
    from datetime import UTC, datetime

    return datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC)


def test_ab_target_identity_tamper_fails_closed(tmp_path) -> None:
    """Oracle AB: the durable (submission_id, target) identity is protected."""
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    foreign = make_target(envelope.submission_id, account_reference="acct-foreign")
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    database = tmp_path / "execstate.sqlite3"
    store.close()

    # With the contracted foreign keys enabled the storage layer rejects the
    # identity rewrite outright.
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE submissions SET target_json = ?
                WHERE submission_id = ? AND target_json = ?
                """,
                (
                    foreign.model_dump_json(),
                    envelope.submission_id,
                    target.model_dump_json(),
                ),
            )
    finally:
        connection.close()

    # A connection that bypasses foreign keys still cannot hide the rewrite:
    # startup recovery rejects the row because the durable record and its
    # extracted identity disagree.
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        connection.execute(
            """
            UPDATE submissions SET target_json = ?
            WHERE submission_id = ? AND target_json = ?
            """,
            (
                foreign.model_dump_json(),
                envelope.submission_id,
                target.model_dump_json(),
            ),
        )
    finally:
        connection.close()

    with pytest.raises(ExecutionStateCorruptionError):
        reopened = SQLiteExecutionStateStore(database)
        try:
            recover_store(reopened, target, observed_at=target_observed_at())
        finally:
            reopened.close()


def test_ab_record_target_tamper_fails_closed(tmp_path) -> None:
    """Oracle AB: a record whose target no longer matches its identity fails."""
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    database = tmp_path / "execstate.sqlite3"
    store.close()

    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT record_json FROM submissions"
        ).fetchone()
        document = json.loads(row["record_json"])
        document["target"]["account_reference"] = "acct-tampered"
        tampered = json.dumps(document, sort_keys=True, separators=(",", ":"))
        assert tampered != row["record_json"]
        connection.execute(
            "UPDATE submissions SET record_json = ?", (tampered,)
        )
    finally:
        connection.close()

    reopened = SQLiteExecutionStateStore(database)
    with pytest.raises(ExecutionStateCorruptionError):
        recover_store(reopened, target, observed_at=target_observed_at())
    reopened.close()


def test_ab_token_tamper_fails_closed(tmp_path) -> None:
    """Oracle AB: mutating the persisted claim token fails closed."""
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    claim = claim_store(store, envelope.submission_id, target)
    database = tmp_path / "execstate.sqlite3"
    store.close()

    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        connection.execute("DROP TRIGGER claims_no_update")
        connection.execute(
            "UPDATE claims SET token = 'tampered-token' WHERE claim_id = ?",
            (claim.claim_id,),
        )
        connection.execute(
            """CREATE TRIGGER claims_no_update
            BEFORE UPDATE ON claims
            BEGIN SELECT RAISE(ABORT, 'claims are immutable'); END"""
        )
    finally:
        connection.close()

    reopened = SQLiteExecutionStateStore(database)
    with pytest.raises(ExecutionStateCorruptionError):
        recover_store(reopened, target, observed_at=target_observed_at())
    reopened.close()


def test_ab_stored_dispatch_decision_substitution_fails_closed(tmp_path) -> None:
    """Oracle AB: a valid decision bound to another identity is not accepted."""
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    source_target = make_target(
        envelope.submission_id, account_reference="acct-source"
    )
    kept_target = make_target(envelope.submission_id, account_reference="acct-keep")
    store = make_store(tmp_path)
    recover_store(store, source_target)

    source_record, source_receipt = prepare_store(store, provenance, source_target)
    source_claim = claim_store(store, envelope.submission_id, source_target)
    source_recovery = recover_store(
        store, source_target, observed_at=RECOVER_RESCAN_AT
    )
    source_decision = build_eligibility(
        store,
        provenance,
        source_target,
        source_record,
        source_receipt,
        source_recovery,
        source_claim,
    )
    assert source_decision.eligible is True

    kept_record, kept_receipt = prepare_store(store, provenance, kept_target)
    kept_claim = claim_store(store, envelope.submission_id, kept_target)
    kept_recovery = recover_store(
        store, kept_target, observed_at=RECOVER_RESCAN_AT
    )
    kept_decision = build_eligibility(
        store,
        provenance,
        kept_target,
        kept_record,
        kept_receipt,
        kept_recovery,
        kept_claim,
    )
    assert kept_decision.eligible is True
    store.mark_dispatching(kept_decision, owner_id="worker-1", token="token-1")
    database = tmp_path / "execstate.sqlite3"
    store.close()

    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        connection.execute(
            """
            UPDATE submissions SET dispatch_decision_json = ?
            WHERE submission_id = ? AND target_json = ?
            """,
            (
                source_decision.model_dump_json(),
                envelope.submission_id,
                kept_target.model_dump_json(),
            ),
        )
    finally:
        connection.close()

    reopened = SQLiteExecutionStateStore(database)
    with pytest.raises(ExecutionStateCorruptionError):
        recover_store(reopened, kept_target, observed_at=target_observed_at())
    reopened.close()
