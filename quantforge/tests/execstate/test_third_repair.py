from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

import pytest
from _helpers import (
    CLAIM_AT,
    CLAIM_EXPIRES_AT,
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
    ExecutionStateCommitUnknownError,
    ExecutionStateCorruptionError,
    ExecutionStateDeniedError,
    ExecutionStatePathError,
    ExecutionStateSchemaError,
)

store_module = importlib.import_module("quantforge.execstate.store")


def _connection(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _dispatched(tmp_path: Path):
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
    return store, target, envelope, record


class _CommitAfterStore(store_module.SQLiteExecutionStateStore):
    def __init__(self, path) -> None:
        self.fail_commit = False
        super().__init__(path)

    def _commit_connection(self) -> None:
        super()._commit_connection()
        if self.fail_commit:
            raise RuntimeError("simulated commit interruption")


def test_b6_commit_boundary_maps_post_commit_exception(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = _CommitAfterStore(tmp_path / "execstate.sqlite3")
    recover_store(store, target)
    store.fail_commit = True
    with pytest.raises(ExecutionStateCommitUnknownError):
        prepare_store(store, provenance, target)
    store.close()

    reopened = make_store(tmp_path)
    recover_store(reopened, target, observed_at=OUTCOME_AT)
    record, receipt = reopened.read(envelope.submission_id, target)
    assert record.state == "PREPARED"
    assert receipt.committed is True
    reopened.close()


def test_b7_runtime_version_gate(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(store_module.sqlite3, "sqlite_version_info", (3, 34, 9))
    with pytest.raises(ExecutionStatePathError):
        make_store(tmp_path)
    assert not (tmp_path / "execstate.sqlite3").exists()


def test_b7_required_connection_pragmas_are_verified(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert store._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    assert store._connection.execute("PRAGMA locking_mode").fetchone()[0] == "normal"
    store.close()


def test_b8_recordable_claim_conflict_is_audited(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, _receipt = prepare_store(store, provenance, target)
    store.claim(
        record.submission_id,
        target,
        owner_id="worker-1",
        token="token-1",
        acquired_at=CLAIM_AT,
        expires_at=CLAIM_EXPIRES_AT,
    )
    with pytest.raises(ExecutionStateDeniedError):
        store.claim(
            record.submission_id,
            target,
            owner_id="worker-2",
            token="token-2",
            acquired_at=CLAIM_AT,
            expires_at=CLAIM_EXPIRES_AT,
        )
    events = [json.loads(item) for item in store.read_audit(record.submission_id, target)]
    assert events[-1]["event_kind"] == "CLAIM_CONFLICT"
    assert events[-1]["reason"] == "CLAIM_CONFLICT"
    assert store._connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
    store.close()


def test_b9_reads_use_explicit_snapshots(tmp_path) -> None:
    store, _provenance, target, record, _receipt, _recovery, _claim, _envelope = (
        prepare_claim_recover(tmp_path)
    )
    calls: list[str] = []
    original = store._begin_read

    def begin_read() -> None:
        calls.append("BEGIN")
        original()

    store._begin_read = begin_read  # type: ignore[method-assign]
    store.read(record.submission_id, target)
    store.read_audit(record.submission_id, target)
    assert calls == ["BEGIN", "BEGIN"]
    store.close()


def test_b10_noncanonical_transport_outcome_is_rejected(tmp_path) -> None:
    store, target, envelope, record = _dispatched(tmp_path)
    store.persist_transport_outcome(
        BrokerSubmissionOutcome(submission_id=envelope.submission_id, status="ACCEPTED"),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    database = tmp_path / "execstate.sqlite3"
    store.close()
    connection = _connection(database)
    connection.execute("DROP TRIGGER operational_payloads_no_update")
    connection.execute(
        "UPDATE operational_payloads SET payload_json = ? WHERE record_id = ?",
        (
            '{"status":"ACCEPTED","broker_message":null,"submission_id":"'
            + envelope.submission_id
            + '","broker_reference":null}',
            record.record_id,
        ),
    )
    connection.execute(
        """CREATE TRIGGER operational_payloads_no_update
        BEFORE UPDATE ON operational_payloads
        BEGIN SELECT RAISE(ABORT, 'operational payloads are append-only'); END"""
    )
    connection.close()
    reopened = make_store(tmp_path)
    with pytest.raises(ExecutionStateCorruptionError):
        reopened.recover(target, observed_at=OUTCOME_AT)
    reopened.close()


def test_b11_receipt_epoch_column_must_match_json(tmp_path) -> None:
    store, target, _envelope, _record = _dispatched(tmp_path)
    database = tmp_path / "execstate.sqlite3"
    store.close()
    connection = _connection(database)
    row = connection.execute("SELECT receipt_id FROM commit_receipts ORDER BY receipt_sequence DESC LIMIT 1").fetchone()
    assert row is not None
    connection.execute("DROP TRIGGER commit_receipts_no_update")
    connection.execute(
        "UPDATE commit_receipts SET recovery_epoch = recovery_epoch + 1 WHERE receipt_id = ?",
        (row[0],),
    )
    connection.execute(
        """CREATE TRIGGER commit_receipts_no_update
        BEFORE UPDATE ON commit_receipts
        BEGIN SELECT RAISE(ABORT, 'commit receipts are immutable'); END"""
    )
    connection.close()
    reopened = make_store(tmp_path)
    with pytest.raises(ExecutionStateCorruptionError):
        reopened.recover(target, observed_at=OUTCOME_AT)
    reopened.close()


def test_b12_prepare_audit_timestamp_is_bound(tmp_path) -> None:
    store = make_store(tmp_path)
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    database = tmp_path / "execstate.sqlite3"
    store.close()
    connection = _connection(database)
    event = connection.execute(
        "SELECT * FROM audit_events WHERE reason = 'NEW_PREPARE'"
    ).fetchone()
    assert event is not None
    connection.execute("DROP TRIGGER audit_events_no_update")
    document = json.loads(event["event_json"])
    document["observed_at"] = "2030-01-01T12:02:00.000000Z"
    event_json = json.dumps(document, sort_keys=True, separators=(",", ":"))
    connection.execute(
        "UPDATE audit_events SET observed_at = ?, event_json = ? WHERE event_sequence = ?",
        (document["observed_at"], event_json, event["event_sequence"]),
    )
    connection.execute(
        """CREATE TRIGGER audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END"""
    )
    connection.close()
    reopened = make_store(tmp_path)
    with pytest.raises(ExecutionStateCorruptionError):
        reopened.recover(target, observed_at=OUTCOME_AT)
    reopened.close()


def test_b13_missing_safety_object_fails_on_open(tmp_path) -> None:
    store = make_store(tmp_path)
    store.close()
    database = tmp_path / "execstate.sqlite3"
    connection = _connection(database)
    connection.execute("DROP INDEX submissions_one_active_claim")
    connection.close()
    with pytest.raises(ExecutionStateSchemaError):
        make_store(tmp_path)


def test_b13_submission_delete_is_rejected(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, _receipt = prepare_store(store, provenance, target)
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "DELETE FROM submissions WHERE record_id = ?", (record.record_id,)
        )
    assert store._connection.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1
    store.close()


def test_b14_stale_open_store_epoch_is_denied(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    stale = make_store(tmp_path)
    recover_store(stale, target)
    record, _receipt = prepare_store(stale, provenance, target)
    current = make_store(tmp_path)
    recover_store(current, target, observed_at=OUTCOME_AT)
    with pytest.raises(ExecutionStateDeniedError):
        stale.claim(
            record.submission_id,
            target,
            owner_id="worker-1",
            token="token-1",
            acquired_at=CLAIM_AT,
            expires_at=CLAIM_EXPIRES_AT,
        )
    assert current._connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    stale.close()
    current.close()


def test_b15_relative_path_is_rejected_without_side_effect(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ExecutionStatePathError):
        store_module.SQLiteExecutionStateStore("relative.sqlite3")
    assert not (tmp_path / "relative.sqlite3").exists()
