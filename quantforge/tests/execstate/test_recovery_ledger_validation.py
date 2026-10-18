"""Committed regressions for EXECSTATE-A-B4 and EXECSTATE-A-B5.

B4: recorded transport/reconciliation status <-> current state <->
    governing audit event reason <-> stored operational payload status.
B5: audit event observed_at canonical UTC grammar (column and event_json).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _helpers import (
    OUTCOME_AT,
    PREPARE_AT,
    RECOVER_AT,
    build_eligibility,
    claim_store,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)

from quantforge.broker import BrokerSubmissionOutcome
from quantforge.execstate import (
    ExecutionStateCorruptionError,
    ExecutionStateError,
    SQLiteExecutionStateStore,
)
from quantforge.frontier import ExecutionTargetBinding

RECONCILED_AT = datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC)
SCAN_AT = datetime(2030, 1, 1, 12, 2, 30, tzinfo=UTC)
_TABLES = (
    "submissions",
    "commit_receipts",
    "claims",
    "operational_payloads",
    "recovery_runs",
    "audit_events",
    "metadata",
)
_INVALID_AUDIT_INSTANTS = (
    "not-a-timestamp",
    "2030-01-01T12:01:56Z",
    "2030-01-01T12:01:56.000000+00:00",
    "2030-01-01T12:01:56.000000z",
    "2030-01-01T12:01:56.000000Z ",
    "2030-01-01T12:01:56.000Z",
    "2030-13-01T12:01:56.000000Z",
    "2030-01-01T24:01:56.000000Z",
)


def _canonical(document: Any) -> str:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _snapshot(database: Path) -> dict[str, list[tuple[Any, ...]]]:
    connection = _connect(database)
    try:
        snapshot: dict[str, list[tuple[Any, ...]]] = {}
        for table in _TABLES:
            rows = connection.execute(
                f"SELECT * FROM {table}"
            ).fetchall()
            snapshot[table] = sorted(tuple(row) for row in rows)
        return snapshot
    finally:
        connection.close()


def _governing_event(
    connection: sqlite3.Connection, target: ExecutionTargetBinding
) -> sqlite3.Row:
    rows = connection.execute(
        """
        SELECT * FROM audit_events
        WHERE submission_id = ? AND target_json = ?
        ORDER BY event_sequence
        """,
        (target.submission_id, target.model_dump_json()),
    ).fetchall()
    for row in reversed(rows):
        if row["prior_state"] != row["new_state"]:
            return row
    raise AssertionError("no governing audit event found")


def _rewrite_state(
    connection: sqlite3.Connection, target: ExecutionTargetBinding, state: str
) -> None:
    row = connection.execute(
        """
        SELECT record_json FROM submissions
        WHERE submission_id = ? AND target_json = ?
        """,
        (target.submission_id, target.model_dump_json()),
    ).fetchone()
    document = json.loads(row["record_json"])
    document["state"] = state
    connection.execute(
        """
        UPDATE submissions SET state = ?, record_json = ?
        WHERE submission_id = ? AND target_json = ?
        """,
        (state, _canonical(document), target.submission_id, target.model_dump_json()),
    )


def _rewrite_event(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
    *,
    prior_state: str | None = None,
    new_state: str | None = None,
    reason: str | None = None,
    observed_at: str | None = None,
    json_observed_at: str | None = None,
) -> None:
    connection.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
    document = json.loads(event["event_json"])
    document_changed = False
    if prior_state is not None:
        document["prior_state"] = prior_state
        document_changed = True
    if new_state is not None:
        document["new_state"] = new_state
        document_changed = True
    if reason is not None:
        document["reason"] = reason
        document_changed = True
    if json_observed_at is not None:
        document["observed_at"] = json_observed_at
        document_changed = True
    stored_json = _canonical(document) if document_changed else event["event_json"]
    connection.execute(
        """
        UPDATE audit_events
        SET prior_state = ?, new_state = ?, reason = ?, observed_at = ?,
            event_json = ?
        WHERE event_sequence = ?
        """,
        (
            prior_state if prior_state is not None else event["prior_state"],
            new_state if new_state is not None else event["new_state"],
            reason if reason is not None else event["reason"],
            observed_at if observed_at is not None else event["observed_at"],
            stored_json,
            event["event_sequence"],
        ),
    )
    connection.execute(
        """CREATE TRIGGER audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END"""
    )


def _rewrite_transport_payload(
    connection: sqlite3.Connection, status: str
) -> None:
    connection.execute(
        "DROP TRIGGER IF EXISTS operational_payloads_no_update"
    )
    row = connection.execute(
        """
        SELECT * FROM operational_payloads
        WHERE payload_kind = 'TRANSPORT_OUTCOME'
        ORDER BY payload_sequence DESC
        LIMIT 1
        """
    ).fetchone()
    document = json.loads(row["payload_json"])
    document["status"] = status
    connection.execute(
        "UPDATE operational_payloads SET payload_json = ? WHERE payload_sequence = ?",
        (_canonical(document), row["payload_sequence"]),
    )
    connection.execute(
        """CREATE TRIGGER operational_payloads_no_update
        BEFORE UPDATE ON operational_payloads
        BEGIN SELECT RAISE(ABORT, 'operational payloads are append-only'); END"""
    )


def _rebase_later_events(
    connection: sqlite3.Connection,
    target: ExecutionTargetBinding,
    after_sequence: int,
    state: str,
) -> None:
    """Keep the append-only history chain contiguous with a rewritten state."""
    rows = connection.execute(
        """
        SELECT * FROM audit_events
        WHERE submission_id = ? AND target_json = ? AND event_sequence > ?
        ORDER BY event_sequence
        """,
        (target.submission_id, target.model_dump_json(), after_sequence),
    ).fetchall()
    for row in rows:
        _rewrite_event(connection, row, new_state=state, prior_state=state)


def _scan(
    database: Path,
    target: ExecutionTargetBinding,
    observed_at: datetime = SCAN_AT,
) -> dict[str, Any]:
    store = SQLiteExecutionStateStore(database)
    try:
        evidence = store.recover(target, observed_at=observed_at)
        record, _receipt = store.read(target.submission_id, target)
        return {
            "outcome": "COMPLETE",
            "state": record.state,
            "evidence_id": evidence.evidence_id,
        }
    except ExecutionStateCorruptionError as exc:
        return {
            "outcome": "ExecutionStateCorruptionError",
            "message": str(exc)[:120],
        }
    except ExecutionStateError as exc:
        return {"outcome": type(exc).__name__, "message": str(exc)[:120]}
    finally:
        store.close()


def _dispatched_transport(
    tmp_path: Path, status: str
) -> tuple[Path, ExecutionTargetBinding]:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, receipt = prepare_store(store, provenance, target)
    claim = claim_store(store, envelope.submission_id, target)
    recovery = recover_store(
        store, target, observed_at=datetime(2030, 1, 1, 12, 1, 57, tzinfo=UTC)
    )
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    store.persist_transport_outcome(
        BrokerSubmissionOutcome(
            submission_id=envelope.submission_id, status=status
        ),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    database = tmp_path / "execstate.sqlite3"
    store.close()
    return database, target


def _dispatched_reconciled(
    tmp_path: Path, status: str
) -> tuple[Path, ExecutionTargetBinding]:
    database, target, store, envelope = _dispatched_unknown(tmp_path)
    store.apply_reconciliation(
        envelope.submission_id,
        target,
        status=status,
        evidence_reference=f"reconciliation-{status.lower()}",
        observed_at=RECONCILED_AT,
    )
    store.close()
    return database, target


def _dispatched_unknown(
    tmp_path: Path,
) -> tuple[Path, ExecutionTargetBinding, Any, Any]:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, receipt = prepare_store(store, provenance, target)
    claim = claim_store(store, envelope.submission_id, target)
    recovery = recover_store(
        store, target, observed_at=datetime(2030, 1, 1, 12, 1, 57, tzinfo=UTC)
    )
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    store.persist_transport_outcome(
        BrokerSubmissionOutcome(
            submission_id=envelope.submission_id, status="OUTCOME_UNKNOWN"
        ),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    database = tmp_path / "execstate.sqlite3"
    return database, target, store, envelope


def _claim_database(tmp_path: Path) -> tuple[Path, ExecutionTargetBinding]:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    claim_store(store, envelope.submission_id, target)
    database = tmp_path / "execstate.sqlite3"
    store.close()
    return database, target


@pytest.mark.parametrize(
    ("status", "state", "reason", "other_state", "other_reason"),
    [
        (
            "ACCEPTED",
            "ACKNOWLEDGED",
            "TRANSPORT_ACCEPTED",
            "REJECTED",
            "TRANSPORT_REJECTED",
        ),
        (
            "REJECTED",
            "REJECTED",
            "TRANSPORT_REJECTED",
            "ACKNOWLEDGED",
            "TRANSPORT_ACCEPTED",
        ),
        (
            "OUTCOME_UNKNOWN",
            "OUTCOME_UNKNOWN",
            "TRANSPORT_OUTCOME_UNKNOWN",
            "ACKNOWLEDGED",
            "TRANSPORT_ACCEPTED",
        ),
    ],
)
def test_b4_transport_status_state_reason_matrix(
    tmp_path, status, state, reason, other_state, other_reason
) -> None:
    valid_root = tmp_path / "valid"
    valid_root.mkdir()
    database, target = _dispatched_transport(valid_root, status)
    connection = _connect(database)
    try:
        governing = _governing_event(connection, target)
        assert governing["new_state"] == state
        assert governing["reason"] == reason
        assert governing["status"] == status
    finally:
        connection.close()
    valid = _scan(database, target)
    assert valid["outcome"] == "COMPLETE"
    assert valid["state"] == state

    tampered_root = tmp_path / "tampered"
    tampered_root.mkdir()
    database, target = _dispatched_transport(tampered_root, status)
    connection = _connect(database)
    try:
        governing = _governing_event(connection, target)
        _rewrite_state(connection, target, other_state)
        _rewrite_event(
            connection,
            governing,
            new_state=other_state,
            reason=other_reason,
        )
        _rebase_later_events(
            connection, target, governing["event_sequence"], other_state
        )
    finally:
        connection.close()

    assert _scan(database, target)["outcome"] == "ExecutionStateCorruptionError"


@pytest.mark.parametrize(
    ("status", "state", "reason", "other_state", "other_reason"),
    [
        (
            "ACCEPTED",
            "ACKNOWLEDGED",
            "RECONCILIATION_ACCEPTED",
            "REJECTED",
            "RECONCILIATION_REJECTED",
        ),
        (
            "REJECTED",
            "REJECTED",
            "RECONCILIATION_REJECTED",
            "ACKNOWLEDGED",
            "RECONCILIATION_ACCEPTED",
        ),
        (
            "NO_EFFECT_CONFIRMED",
            "NO_EFFECT_CONFIRMED",
            "RECONCILIATION_NO_EFFECT",
            "ACKNOWLEDGED",
            "RECONCILIATION_ACCEPTED",
        ),
    ],
)
def test_b4_reconciliation_status_state_reason_matrix(
    tmp_path, status, state, reason, other_state, other_reason
) -> None:
    valid_root = tmp_path / "valid"
    valid_root.mkdir()
    database, target = _dispatched_reconciled(valid_root, status)
    connection = _connect(database)
    try:
        governing = _governing_event(connection, target)
        assert governing["reason"] == reason
        assert governing["status"] == status
    finally:
        connection.close()
    valid = _scan(database, target)
    assert valid["outcome"] == "COMPLETE"
    assert valid["state"] == state

    tampered_root = tmp_path / "tampered"
    tampered_root.mkdir()
    database, target = _dispatched_reconciled(tampered_root, status)
    connection = _connect(database)
    try:
        governing = _governing_event(connection, target)
        _rewrite_state(connection, target, other_state)
        _rewrite_event(
            connection,
            governing,
            new_state=other_state,
            reason=other_reason,
        )
        _rebase_later_events(
            connection, target, governing["event_sequence"], other_state
        )
    finally:
        connection.close()

    assert _scan(database, target)["outcome"] == "ExecutionStateCorruptionError"


def test_b4_historical_transport_reason_contradiction(tmp_path) -> None:
    """A non-governing historical event must still match its recorded status."""
    database, target, store, envelope = _dispatched_unknown(tmp_path)
    store.apply_reconciliation(
        envelope.submission_id,
        target,
        status="ACCEPTED",
        evidence_reference="reconciliation-1",
        observed_at=RECONCILED_AT,
    )
    store.close()
    assert _scan(database, target)["state"] == "ACKNOWLEDGED"

    connection = _connect(database)
    try:
        historical = connection.execute(
            "SELECT * FROM audit_events WHERE reason = 'TRANSPORT_OUTCOME_UNKNOWN'"
        ).fetchone()
        assert historical["status"] == "OUTCOME_UNKNOWN"
        _rewrite_event(connection, historical, reason="TRANSPORT_ACCEPTED")
    finally:
        connection.close()

    assert _scan(database, target)["outcome"] == "ExecutionStateCorruptionError"


def test_b4_payload_status_contradiction(tmp_path) -> None:
    database, target = _dispatched_transport(tmp_path, "REJECTED")
    connection = _connect(database)
    try:
        _rewrite_transport_payload(connection, "ACCEPTED")
    finally:
        connection.close()

    assert _scan(database, target)["outcome"] == "ExecutionStateCorruptionError"


def test_b4_replay_after_reconciliation_still_recovers(tmp_path) -> None:
    """A contracted observational replay must not be mistaken for corruption."""
    database, target, store, envelope = _dispatched_unknown(tmp_path)
    unknown = BrokerSubmissionOutcome(
        submission_id=envelope.submission_id, status="OUTCOME_UNKNOWN"
    )
    store.apply_reconciliation(
        envelope.submission_id,
        target,
        status="ACCEPTED",
        evidence_reference="reconciliation-1",
        observed_at=RECONCILED_AT,
    )
    replayed = store.persist_transport_outcome(
        unknown,
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    store.close()
    assert replayed.state == "ACKNOWLEDGED"

    result = _scan(database, target)
    assert result["outcome"] == "COMPLETE"
    assert result["state"] == "ACKNOWLEDGED"


@pytest.mark.parametrize("token", _INVALID_AUDIT_INSTANTS)
def test_b5_invalid_audit_observed_at_rejected(tmp_path, token: str) -> None:
    database, target = _claim_database(tmp_path)
    runs_before = _snapshot(database)["recovery_runs"]
    connection = _connect(database)
    try:
        event = connection.execute(
            "SELECT * FROM audit_events WHERE reason = 'CLAIM_ACQUIRED'"
        ).fetchone()
        _rewrite_event(
            connection, event, observed_at=token, json_observed_at=token
        )
    finally:
        connection.close()

    assert _scan(database, target)["outcome"] == "ExecutionStateCorruptionError"
    assert _snapshot(database)["recovery_runs"] == runs_before


def test_b5_invalid_column_with_valid_event_json(tmp_path) -> None:
    database, target = _claim_database(tmp_path)
    connection = _connect(database)
    try:
        event = connection.execute(
            "SELECT * FROM audit_events WHERE reason = 'CLAIM_ACQUIRED'"
        ).fetchone()
        _rewrite_event(connection, event, observed_at="not-a-timestamp")
    finally:
        connection.close()

    assert _scan(database, target)["outcome"] == "ExecutionStateCorruptionError"


@pytest.mark.parametrize(
    "json_observed_at",
    ["not-a-timestamp", "2030-01-01T12:01:57.000000Z"],
)
def test_b5_column_and_event_json_disagree(tmp_path, json_observed_at: str) -> None:
    database, target = _claim_database(tmp_path)
    connection = _connect(database)
    try:
        event = connection.execute(
            "SELECT * FROM audit_events WHERE reason = 'CLAIM_ACQUIRED'"
        ).fetchone()
        _rewrite_event(
            connection,
            event,
            json_observed_at=json_observed_at,
        )
    finally:
        connection.close()

    assert _scan(database, target)["outcome"] == "ExecutionStateCorruptionError"


def test_b5_valid_canonical_timestamps_recover(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    store = make_store(tmp_path)
    first_target = make_target(
        envelope.submission_id, account_reference="acct-zero"
    )
    second_target = make_target(
        envelope.submission_id, account_reference="acct-one"
    )
    recover_store(store, first_target, observed_at=RECOVER_AT)
    first_record, _first_receipt = store.prepare(
        envelope, provenance, first_target, prepared_at=PREPARE_AT
    )
    second_at = PREPARE_AT + timedelta(microseconds=1)
    second_record, _second_receipt = store.prepare(
        envelope, provenance, second_target, prepared_at=second_at
    )
    store.close()

    assert first_record.updated_at == PREPARE_AT
    assert second_record.updated_at == second_at
    result = _scan(tmp_path / "execstate.sqlite3", first_target)
    assert result["outcome"] == "COMPLETE"


@pytest.mark.parametrize("case", ["transport", "reconciliation", "timestamp"])
def test_b4_b5_failed_recovery_rolls_back(tmp_path, case: str) -> None:
    if case == "transport":
        database, target = _dispatched_transport(tmp_path, "REJECTED")
    elif case == "reconciliation":
        database, target = _dispatched_reconciled(tmp_path, "REJECTED")
    else:
        database, target = _claim_database(tmp_path)
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    store = SQLiteExecutionStateStore(database)
    try:
        recover_store(store, target, observed_at=SCAN_AT)
        other_target = make_target(
            envelope.submission_id, account_reference="acct-other"
        )
        store.prepare(
            envelope,
            provenance,
            other_target,
            prepared_at=SCAN_AT + timedelta(seconds=1),
        )
    finally:
        store.close()

    connection = _connect(database)
    try:
        if case == "timestamp":
            event = connection.execute(
                "SELECT * FROM audit_events WHERE reason = 'CLAIM_ACQUIRED'"
            ).fetchone()
            _rewrite_event(
                connection,
                event,
                observed_at="not-a-timestamp",
                json_observed_at="not-a-timestamp",
            )
        else:
            governing = _governing_event(connection, target)
            _rewrite_state(connection, target, "ACKNOWLEDGED")
            _rewrite_event(
                connection,
                governing,
                new_state="ACKNOWLEDGED",
                reason="TRANSPORT_ACCEPTED"
                if case == "transport"
                else "RECONCILIATION_ACCEPTED",
            )
            _rebase_later_events(
                connection, target, governing["event_sequence"], "ACKNOWLEDGED"
            )
    finally:
        connection.close()

    before = _snapshot(database)
    reopened = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            reopened.recover(target, observed_at=SCAN_AT + timedelta(seconds=5))
    finally:
        reopened.close()

    assert _snapshot(database) == before
