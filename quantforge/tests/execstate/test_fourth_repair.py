from __future__ import annotations

import importlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from _helpers import (
    CLAIM_EXPIRES_AT,
    EVIDENCE_AT,
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
    ExecutionStateCorruptionError,
    ExecutionStateDeniedError,
    ExecutionStateSchemaError,
)

store_module = importlib.import_module("quantforge.execstate.store")


def _connection(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _recreate_audit_update_trigger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TRIGGER audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END"""
    )


def _rewrite_audit_event(
    database: Path,
    *,
    reason: str,
    updates: dict[str, Any],
) -> None:
    connection = _connection(database)
    row = connection.execute(
        "SELECT * FROM audit_events WHERE reason = ? ORDER BY event_sequence LIMIT 1",
        (reason,),
    ).fetchone()
    assert row is not None
    document = json.loads(row["event_json"])
    document.update(updates)
    assignments = ", ".join(f"{name} = ?" for name in updates)
    values = [*updates.values(), _canonical_json(document), row["event_sequence"]]
    connection.execute("DROP TRIGGER audit_events_no_update")
    connection.execute(
        f"UPDATE audit_events SET {assignments}, event_json = ? "
        "WHERE event_sequence = ?",
        values,
    )
    _recreate_audit_update_trigger(connection)
    connection.close()


def _append_fabricated_claim_conflict(database: Path) -> None:
    connection = _connection(database)
    prepared = connection.execute(
        "SELECT * FROM audit_events WHERE reason = 'NEW_PREPARE'"
    ).fetchone()
    assert prepared is not None
    sequence = int(prepared["event_sequence"]) + 1
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
        "observed_at": "2030-01-01T12:01:56.000000Z",
        "owner_id": "attacker",
        "token_reference": None,
        "claim_id": None,
        "payload_sequence": None,
        "status": None,
        "reason": "CLAIM_CONFLICT",
    }
    connection.execute(
        """INSERT INTO audit_events(
            event_sequence, row_scope, submission_id, target_json, prior_state,
            new_state, generation, recovery_epoch, event_kind, reason,
            observed_at, owner_id, token_reference, claim_id,
            payload_sequence, status, event_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            _canonical_json(event),
        ),
    )
    connection.close()


def _rewrite_schema(
    database: Path,
    *,
    table: str,
    old: str,
    new: str,
) -> None:
    connection = _connection(database)
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    assert row is not None
    sql = str(row["sql"])
    assert old in sql
    rewritten = sql.replace(old, new, 1)
    version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_schema SET sql = ? WHERE type = 'table' AND name = ?",
        (rewritten, table),
    )
    connection.execute(f"PRAGMA schema_version = {version + 1}")
    connection.execute("PRAGMA writable_schema = OFF")
    connection.close()


def _remove_check_constraint(database: Path, *, table: str, field: str) -> None:
    connection = _connection(database)
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    assert row is not None
    sql = str(row["sql"])
    upper = sql.upper()
    field_position = upper.index(field.upper())
    check_position = upper.index("CHECK", field_position)
    opening = sql.index("(", check_position)
    depth = 0
    closing = None
    for position in range(opening, len(sql)):
        character = sql[position]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                closing = position
                break
    assert closing is not None
    rewritten = sql[:check_position] + sql[closing + 1 :]
    schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_schema SET sql = ? WHERE type = 'table' AND name = ?",
        (rewritten, table),
    )
    connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
    connection.execute("PRAGMA writable_schema = OFF")
    connection.close()


def _foreign_key_topology(
    connection: sqlite3.Connection, table: str
) -> frozenset[tuple[str, tuple[tuple[str, str], ...], str, str, str]]:
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        grouped.setdefault(int(row["id"]), []).append(row)
    result = set()
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: int(row["seq"]))
        first = ordered[0]
        result.add(
            (
                str(first["table"]),
                tuple((str(row["from"]), str(row["to"])) for row in ordered),
                str(first["on_update"]),
                str(first["on_delete"]),
                str(first["match"]),
            )
        )
    return frozenset(result)


def _authority_snapshot(connection: sqlite3.Connection) -> tuple[Any, ...]:
    tables = (
        "submissions",
        "commit_receipts",
        "claims",
        "operational_payloads",
        "recovery_runs",
        "audit_events",
    )
    counts = tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    )
    metadata = tuple(
        connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()[0]
        for key in ("next_generation", "next_recovery_epoch")
    )
    return counts + metadata


def test_audit_semantics_matrix_covers_all_contracted_pairs() -> None:
    boundary_pairs = {
        ("RECOVERY_CUT_BEGIN", "RECOVERY_CUT_BEGIN"),
        ("RECOVERY_CUT_END", "RECOVERY_CUT_END"),
    }
    assert set(store_module._REASON_PAIRS) - boundary_pairs == set(
        store_module._AUDIT_SEMANTICS
    )
    assert len(store_module._REASON_PAIRS) == 22
    assert len(store_module._AUDIT_SEMANTICS) == 20
    assert len(store_module._EVENT_KINDS) == 12


def test_fabricated_canonical_claim_conflict_prevents_complete(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    database = tmp_path / "execstate.sqlite3"
    store.close()
    _append_fabricated_claim_conflict(database)

    connection = _connection(database)
    before = _authority_snapshot(connection)
    connection.close()
    reopened = make_store(tmp_path)
    with pytest.raises(ExecutionStateCorruptionError):
        reopened.recover(target, observed_at=OUTCOME_AT)
    reopened.close()
    connection = _connection(database)
    assert _authority_snapshot(connection) == before
    connection.close()


@pytest.mark.parametrize(
    ("reason", "updates"),
    [
        ("NEW_PREPARE", {"owner_id": "attacker"}),
        ("DUPLICATE_PREPARE_REPLAY", {"observed_at": "2030-01-01T12:01:56.000000Z"}),
        ("CLAIM_ACQUIRED", {"owner_id": None}),
        ("CLAIM_ACQUIRED", {"observed_at": "2030-01-01T12:01:57.000000Z"}),
    ],
)
def test_operation_specific_audit_semantic_mutations_fail_closed(
    tmp_path, reason: str, updates: dict[str, Any]
) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    if reason == "DUPLICATE_PREPARE_REPLAY":
        prepare_store(store, provenance, target)
    if reason == "CLAIM_ACQUIRED":
        claim_store(store, target.submission_id, target)
    database = tmp_path / "execstate.sqlite3"
    store.close()
    _rewrite_audit_event(database, reason=reason, updates=updates)
    reopened = make_store(tmp_path)
    with pytest.raises(ExecutionStateCorruptionError):
        reopened.recover(target, observed_at=OUTCOME_AT)
    reopened.close()


def test_real_claim_conflict_history_recovers_complete(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    claim_store(store, target.submission_id, target)
    with pytest.raises(ExecutionStateDeniedError):
        claim_store(
            store,
            target.submission_id,
            target,
            owner_id="worker-2",
            token="token-2",
        )
    store.close()
    reopened = make_store(tmp_path)
    recovery = reopened.recover(target, observed_at=OUTCOME_AT)
    assert recovery.status == "COMPLETE"
    reopened.close()


def test_recovery_rebind_cannot_carry_operation_foreign_fields(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    first = make_store(tmp_path)
    recover_store(first, target)
    prepare_store(first, provenance, target)
    first.close()
    second = make_store(tmp_path)
    recover_store(second, target, observed_at=OUTCOME_AT)
    second.close()
    database = tmp_path / "execstate.sqlite3"
    _rewrite_audit_event(
        database,
        reason="RECOVERY_EPOCH_REBIND",
        updates={"owner_id": "attacker"},
    )
    reopened = make_store(tmp_path)
    with pytest.raises(ExecutionStateCorruptionError):
        reopened.recover(target, observed_at=OUTCOME_AT + timedelta(seconds=1))
    reopened.close()


def test_payload_replay_requires_its_exact_operation_fields(tmp_path) -> None:
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
    outcome = BrokerSubmissionOutcome(
        submission_id=envelope.submission_id,
        status="ACCEPTED",
    )
    store.persist_transport_outcome(
        outcome,
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    store.persist_transport_outcome(
        outcome,
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    database = tmp_path / "execstate.sqlite3"
    store.close()
    _rewrite_audit_event(
        database,
        reason="TRANSPORT_OUTCOME_REPLAY",
        updates={"owner_id": None},
    )
    reopened = make_store(tmp_path)
    with pytest.raises(ExecutionStateCorruptionError):
        reopened.recover(target, observed_at=OUTCOME_AT + timedelta(seconds=1))
    reopened.close()


def test_fresh_schema_has_exact_foreign_key_topology(tmp_path) -> None:
    store = make_store(tmp_path)
    expected = {
        "metadata": frozenset(),
        "submissions": frozenset(
            {
                ("commit_receipts", (("current_receipt_id", "receipt_id"),), "NO ACTION", "NO ACTION", "NONE"),
                ("claims", (("active_claim_id", "claim_id"),), "NO ACTION", "NO ACTION", "NONE"),
            }
        ),
        "commit_receipts": frozenset(
            {("submissions", (("record_id", "record_id"),), "NO ACTION", "NO ACTION", "NONE")}
        ),
        "claims": frozenset(
            {("submissions", (("record_id", "record_id"),), "NO ACTION", "NO ACTION", "NONE")}
        ),
        "operational_payloads": frozenset(
            {("submissions", (("record_id", "record_id"),), "NO ACTION", "NO ACTION", "NONE")}
        ),
        "audit_events": frozenset(
            {
                (
                    "submissions",
                    (("submission_id", "submission_id"), ("target_json", "target_json")),
                    "NO ACTION",
                    "NO ACTION",
                    "NONE",
                ),
                (
                    "recovery_runs",
                    (("boundary_run_sequence", "run_sequence"),),
                    "NO ACTION",
                    "NO ACTION",
                    "NONE",
                ),
            }
        ),
        "recovery_runs": frozenset(),
    }
    for table, topology in expected.items():
        assert _foreign_key_topology(store._connection, table) == topology
    store.close()
    make_store(tmp_path).close()


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        ("commit_receipts", "REFERENCES submissions(record_id)", ""),
        ("claims", "REFERENCES submissions(record_id)", ""),
        ("operational_payloads", "REFERENCES submissions(record_id)", ""),
        (
            "audit_events",
            "FOREIGN KEY (submission_id, target_json)\n            REFERENCES submissions(submission_id, target_json)",
            "CHECK (1 = 1)",
        ),
        (
            "audit_events",
            "boundary_run_sequence INTEGER\n            REFERENCES recovery_runs(run_sequence) DEFERRABLE INITIALLY DEFERRED,\n        boundary_phase",
            "boundary_run_sequence INTEGER,\n        boundary_phase",
        ),
        (
            "submissions",
            "REFERENCES commit_receipts(receipt_id)\n            DEFERRABLE INITIALLY DEFERRED",
            "",
        ),
        (
            "submissions",
            "REFERENCES claims(claim_id)\n            DEFERRABLE INITIALLY DEFERRED",
            "",
        ),
    ],
)
def test_missing_required_foreign_key_fails_on_reopen(
    tmp_path, table: str, old: str, new: str
) -> None:
    store = make_store(tmp_path)
    store.close()
    _rewrite_schema(tmp_path / "execstate.sqlite3", table=table, old=old, new=new)
    with pytest.raises(ExecutionStateSchemaError):
        make_store(tmp_path)


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        (
            "commit_receipts",
            "REFERENCES submissions(record_id)",
            "REFERENCES submissions(generation)",
        ),
        (
            "claims",
            "REFERENCES submissions(record_id)",
            "REFERENCES submissions(record_id) ON DELETE CASCADE",
        ),
        (
            "submissions",
            "DEFERRABLE INITIALLY DEFERRED",
            "DEFERRABLE INITIALLY IMMEDIATE",
        ),
        (
            "recovery_runs",
            "target_json TEXT NOT NULL",
            "target_json TEXT NOT NULL REFERENCES submissions(record_id)",
        ),
    ],
)
def test_altered_or_unexpected_foreign_key_fails_on_reopen(
    tmp_path, table: str, old: str, new: str
) -> None:
    store = make_store(tmp_path)
    store.close()
    _rewrite_schema(tmp_path / "execstate.sqlite3", table=table, old=old, new=new)
    with pytest.raises(ExecutionStateSchemaError):
        make_store(tmp_path)


@pytest.mark.parametrize(
    ("table", "field"),
    [
        ("submissions", "generation"),
        ("submissions", "recovery_epoch"),
        ("submissions", "state"),
        ("commit_receipts", "generation"),
        ("commit_receipts", "recovery_epoch"),
        ("commit_receipts", "committed"),
        ("claims", "generation"),
        ("claims", "recovery_epoch"),
        ("claims", "status"),
        ("operational_payloads", "payload_kind"),
        ("recovery_runs", "recovery_epoch"),
        ("recovery_runs", "scanned_through_generation"),
        ("recovery_runs", "status"),
        ("audit_events", "new_state"),
        ("audit_events", "generation"),
        ("audit_events", "recovery_epoch"),
        ("audit_events", "event_kind"),
    ],
)
def test_missing_required_table_check_fails_closed(
    tmp_path, table: str, field: str
) -> None:
    store = make_store(tmp_path)
    store.close()
    _remove_check_constraint(
        tmp_path / "execstate.sqlite3", table=table, field=field
    )
    with pytest.raises(ExecutionStateSchemaError):
        make_store(tmp_path)


def test_weakened_required_table_check_fails_closed(tmp_path) -> None:
    store = make_store(tmp_path)
    store.close()
    _rewrite_schema(
        tmp_path / "execstate.sqlite3",
        table="commit_receipts",
        old="CHECK (committed = 1)",
        new="CHECK (committed >= 0)",
    )
    with pytest.raises(ExecutionStateSchemaError):
        make_store(tmp_path)


def test_existing_valid_v1_schema_with_checks_reopens(tmp_path) -> None:
    store = make_store(tmp_path)
    store.close()
    reopened = make_store(tmp_path)
    reopened.close()


def test_stale_store_prepare_is_denied_without_mutation(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    first_target = make_target(envelope.submission_id)
    second_target = make_target(envelope.submission_id, account_reference="acct-8")
    stale = make_store(tmp_path)
    recover_store(stale, first_target)
    current = make_store(tmp_path)
    recover_store(current, first_target, observed_at=OUTCOME_AT)
    before = _authority_snapshot(current._connection)
    with pytest.raises(ExecutionStateDeniedError):
        stale.prepare(envelope, provenance, second_target, prepared_at=PREPARE_AT)
    assert _authority_snapshot(current._connection) == before
    stale.close()
    current.close()


def test_stale_store_claim_is_denied_without_mutation(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    stale = make_store(tmp_path)
    recover_store(stale, target)
    record, _receipt = prepare_store(stale, provenance, target)
    current = make_store(tmp_path)
    recover_store(current, target, observed_at=OUTCOME_AT)
    before = _authority_snapshot(current._connection)
    with pytest.raises(ExecutionStateDeniedError):
        claim_store(stale, record.submission_id, target)
    assert _authority_snapshot(current._connection) == before
    stale.close()
    current.close()


def test_stale_store_cannot_mark_dispatching_with_current_store_evidence(
    tmp_path,
) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    stale = make_store(tmp_path)
    recover_store(stale, target)
    prepare_store(stale, provenance, target)
    current = make_store(tmp_path)
    recover_store(current, target, observed_at=EVIDENCE_AT - timedelta(seconds=2))
    record, receipt = current.read(target.submission_id, target)
    claim = current.claim(
        record.submission_id,
        target,
        owner_id="worker-1",
        token="token-1",
        acquired_at=EVIDENCE_AT,
        expires_at=CLAIM_EXPIRES_AT,
    )
    recovery = current.recover(target, observed_at=EVIDENCE_AT)
    decision = build_eligibility(
        current, provenance, target, record, receipt, recovery, claim
    )
    before = _authority_snapshot(current._connection)
    with pytest.raises(ExecutionStateDeniedError):
        stale.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    assert _authority_snapshot(current._connection) == before
    stale.close()
    current.close()


def test_stale_store_recovery_cannot_move_epoch_backwards(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    stale = make_store(tmp_path)
    recover_store(stale, target)
    prepare_store(stale, provenance, target)
    current = make_store(tmp_path)
    recover_store(current, target, observed_at=OUTCOME_AT)
    before = _authority_snapshot(current._connection)
    with pytest.raises(ExecutionStateDeniedError):
        stale.recover(target, observed_at=OUTCOME_AT + timedelta(seconds=1))
    assert _authority_snapshot(current._connection) == before
    stale.close()
    current.close()


def test_epoch_freshness_check_runs_inside_write_transaction(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    observed: list[bool] = []
    original = store._require_current_recovery_epoch

    def probe() -> int:
        observed.append(store._connection.in_transaction)
        return original()

    store._require_current_recovery_epoch = probe  # type: ignore[method-assign]
    record, _receipt = prepare_store(store, provenance, target)
    claim_store(store, record.submission_id, target)
    assert observed == [True, True]
    store.close()


def test_fresh_reopened_store_can_prepare_and_claim_after_epoch_advance(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    first_target = make_target(envelope.submission_id)
    second_target = make_target(envelope.submission_id, account_reference="acct-8")
    first = make_store(tmp_path)
    recover_store(first, first_target)
    first.close()
    current = make_store(tmp_path)
    recovery = recover_store(current, second_target, observed_at=OUTCOME_AT)
    record, _receipt = current.prepare(
        envelope,
        provenance,
        second_target,
        prepared_at=OUTCOME_AT + timedelta(seconds=1),
    )
    claim = current.claim(
        record.submission_id,
        second_target,
        owner_id="worker-1",
        token="token-1",
        acquired_at=OUTCOME_AT + timedelta(seconds=2),
        expires_at=OUTCOME_AT + timedelta(seconds=60),
    )
    assert record.recovery_epoch == recovery.recovery_epoch == claim.recovery_epoch
    current.close()
