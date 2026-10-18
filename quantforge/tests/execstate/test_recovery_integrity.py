"""Committed regressions for the Reviewer A blocker repairs.

EXECSTATE-A-B1  prepare ordering against the epoch-allocating recovery run
EXECSTATE-A-B2  startup-recovery validation of stored durable state
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _helpers import (
    CLAIM_AT,
    CLAIM_EXPIRES_AT,
    OUTCOME_AT,
    PREPARE_AT,
    RECOVER_AT,
    RECOVER_RESCAN_AT,
    build_eligibility,
    make_provenance,
    make_store,
    make_target,
    prepare_claim_recover,
    prepare_store,
    recover_store,
)

from quantforge.broker import BrokerPreDispatchError, BrokerSubmissionOutcome
from quantforge.execstate import (
    ExecutionStateArgumentError,
    ExecutionStateCorruptionError,
    ExecutionStateRecoveryRequiredError,
    SQLiteExecutionStateStore,
)
from quantforge.frontier import ExecutionTargetBinding, RecoveryCompleteEvidence

_TABLES = (
    "submissions",
    "commit_receipts",
    "claims",
    "operational_payloads",
    "recovery_runs",
    "audit_events",
    "metadata",
)


def _database_snapshot(database: Path) -> dict[str, list[tuple[Any, ...]]]:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
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


def _execute(database: Path, statement: str, parameters: tuple[Any, ...] = ()) -> None:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(statement, parameters)
    finally:
        connection.close()


def _stored_state(database: Path, target: ExecutionTargetBinding) -> str:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT state FROM submissions
            WHERE submission_id = ? AND target_json = ?
            """,
            (target.submission_id, target.model_dump_json()),
        ).fetchone()
        assert row is not None
        return str(row["state"])
    finally:
        connection.close()


def _rewrite_recovery_evidence(
    database: Path,
    *,
    scanned_through_generation: int,
    unresolved_submission_ids: tuple[str, ...],
    recovery_epoch: int | None = None,
) -> None:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM recovery_runs ORDER BY run_sequence LIMIT 1"
        ).fetchone()
        assert row is not None
        original = RecoveryCompleteEvidence.from_json(str(row["evidence_json"]))
        replacement = RecoveryCompleteEvidence(
            schema=original.schema,
            evidence_id=original.evidence_id,
            target=original.target,
            recovery_epoch=(
                original.recovery_epoch
                if recovery_epoch is None
                else recovery_epoch
            ),
            scanned_through_generation=scanned_through_generation,
            status=original.status,
            unresolved_submission_ids=unresolved_submission_ids,
            observed_at=original.observed_at,
        )
        connection.execute("DROP TRIGGER recovery_runs_no_update")
        connection.execute(
            """
            UPDATE recovery_runs
            SET recovery_epoch = ?, scanned_through_generation = ?, evidence_json = ?
            WHERE run_sequence = ?
            """,
            (
                replacement.recovery_epoch,
                scanned_through_generation,
                replacement.model_dump_json(),
                row["run_sequence"],
            ),
        )
        connection.execute(
            """
            CREATE TRIGGER recovery_runs_no_update
            BEFORE UPDATE ON recovery_runs
            BEGIN SELECT RAISE(ABORT, 'recovery runs are append-only'); END
            """
        )
    finally:
        connection.close()


def _assert_recovery_fails_closed(
    database: Path,
    target: ExecutionTargetBinding,
    observed_at: datetime,
    *,
    expected_state: str,
) -> None:
    """Startup recovery itself must reject the corruption and publish nothing."""
    runs_before = _database_snapshot(database)["recovery_runs"]
    store = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            store.recover(target, observed_at=observed_at)
        with pytest.raises(ExecutionStateRecoveryRequiredError):
            store.read(target.submission_id, target)
    finally:
        store.close()
    assert _database_snapshot(database)["recovery_runs"] == runs_before
    assert _stored_state(database, target) == expected_state


def _dispatched(tmp_path: Path) -> tuple[Any, ...]:
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
    return store, provenance, target, record, receipt, recovery, claim, envelope, decision


# --------------------------------------------------------------------------
# EXECSTATE-A-B1 - prepared_at versus the epoch-allocating recovery run
# --------------------------------------------------------------------------


def test_prepare_rejects_instant_before_epoch_allocating_recovery(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recovery = recover_store(store, target)
    assert recovery.observed_at == RECOVER_AT
    database = tmp_path / "execstate.sqlite3"
    before = _database_snapshot(database)

    with pytest.raises(ExecutionStateArgumentError) as excinfo:
        store.prepare(
            envelope,
            provenance,
            target,
            prepared_at=RECOVER_AT - timedelta(seconds=5),
        )
    assert "precedes its recovery epoch" in str(excinfo.value)
    assert _database_snapshot(database) == before

    record, receipt = store.prepare(
        envelope, provenance, target, prepared_at=RECOVER_AT
    )
    assert record.state == "PREPARED"
    assert record.generation == 1
    assert record.created_at == RECOVER_AT
    assert record.updated_at == RECOVER_AT
    assert receipt.committed_at == RECOVER_AT
    store.close()


def test_prepare_boundary_ignores_later_same_epoch_runs(tmp_path) -> None:
    """The boundary is the epoch-allocating run, not a later target's rescan."""
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target_a = make_target(envelope.submission_id, account_reference="acct-a")
    target_b = make_target(envelope.submission_id, account_reference="acct-b")
    target_c = make_target(envelope.submission_id, account_reference="acct-c")
    store = make_store(tmp_path)
    allocating = recover_store(store, target_a)
    later = recover_store(store, target_b, observed_at=RECOVER_RESCAN_AT)
    assert allocating.recovery_epoch == later.recovery_epoch == 1

    between = RECOVER_AT + timedelta(seconds=1)
    record_b, _receipt_b = store.prepare(
        envelope, provenance, target_b, prepared_at=between
    )
    assert record_b.created_at == between
    assert record_b.recovery_epoch == 1

    with pytest.raises(ExecutionStateArgumentError):
        store.prepare(
            envelope,
            provenance,
            target_c,
            prepared_at=RECOVER_AT - timedelta(seconds=1),
        )
    store.close()


def test_prepare_boundary_follows_the_new_epoch_allocating_run(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    store.close()

    reopened = make_store(tmp_path)
    second = recover_store(reopened, target, observed_at=OUTCOME_AT)
    assert second.recovery_epoch == 2
    late_target = make_target(envelope.submission_id, account_reference="acct-late")
    with pytest.raises(ExecutionStateArgumentError):
        reopened.prepare(
            envelope,
            provenance,
            late_target,
            prepared_at=OUTCOME_AT - timedelta(seconds=1),
        )
    record, _receipt = reopened.prepare(
        envelope, provenance, late_target, prepared_at=OUTCOME_AT
    )
    assert record.recovery_epoch == 2
    assert record.generation == 2
    reopened.close()


def test_duplicate_prepare_replay_survives_a_new_recovery_epoch(tmp_path) -> None:
    """The ordering guard applies to new records, never to an exact replay."""
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, _receipt = prepare_store(store, provenance, target)
    store.close()

    reopened = make_store(tmp_path)
    recovery = recover_store(reopened, target, observed_at=OUTCOME_AT)
    assert recovery.recovery_epoch == 2
    replay_record, replay_receipt = reopened.prepare(
        envelope, provenance, target, prepared_at=PREPARE_AT
    )
    assert replay_record.state == "PREPARED"
    assert replay_record.created_at == PREPARE_AT
    assert replay_record.generation == record.generation
    assert replay_record.recovery_epoch == recovery.recovery_epoch
    assert replay_receipt.recovery_epoch == recovery.recovery_epoch
    reopened.close()


# --------------------------------------------------------------------------
# EXECSTATE-A-B2 - startup recovery validates stored durable state
# --------------------------------------------------------------------------


def test_recovery_rejects_malformed_dispatch_decision(tmp_path) -> None:
    (
        store,
        _provenance,
        target,
        _record,
        _receipt,
        _recovery,
        _claim,
        envelope,
        _decision,
    ) = _dispatched(tmp_path)
    database = tmp_path / "execstate.sqlite3"
    store.close()
    _execute(
        database,
        """
        UPDATE submissions SET dispatch_decision_json = '{}'
        WHERE submission_id = ? AND target_json = ?
        """,
        (envelope.submission_id, target.model_dump_json()),
    )
    _assert_recovery_fails_closed(
        database, target, OUTCOME_AT, expected_state="DISPATCHING"
    )


def test_recovery_rejects_structurally_invalid_dispatch_decision(tmp_path) -> None:
    (
        store,
        _provenance,
        target,
        _record,
        _receipt,
        _recovery,
        _claim,
        envelope,
        _decision,
    ) = _dispatched(tmp_path)
    database = tmp_path / "execstate.sqlite3"
    store.close()
    _execute(
        database,
        """
        UPDATE submissions SET dispatch_decision_json = ?
        WHERE submission_id = ? AND target_json = ?
        """,
        (
            '{"schema":"frontier-eligibility-v1"}',
            envelope.submission_id,
            target.model_dump_json(),
        ),
    )
    _assert_recovery_fails_closed(
        database, target, OUTCOME_AT, expected_state="DISPATCHING"
    )


def test_recovery_rejects_tampered_transport_outcome_payload(tmp_path) -> None:
    (
        store,
        _provenance,
        target,
        record,
        _receipt,
        _recovery,
        _claim,
        envelope,
        _decision,
    ) = _dispatched(tmp_path)
    store.persist_transport_outcome(
        BrokerSubmissionOutcome(
            submission_id=envelope.submission_id, status="ACCEPTED"
        ),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    database = tmp_path / "execstate.sqlite3"
    store.close()
    # A direct-file attacker removes the append-only trigger and rewrites the
    # stored outcome bytes.
    _execute(database, "DROP TRIGGER operational_payloads_no_update")
    _execute(
        database,
        "UPDATE operational_payloads SET payload_json = ? WHERE record_id = ?",
        ('{"status":"ACCEPTED"}', record.record_id),
    )
    _execute(
        database,
        """CREATE TRIGGER operational_payloads_no_update
        BEFORE UPDATE ON operational_payloads
        BEGIN SELECT RAISE(ABORT, 'operational payloads are append-only'); END""",
    )
    _assert_recovery_fails_closed(
        database, target, OUTCOME_AT, expected_state="ACKNOWLEDGED"
    )


def test_recovery_rejects_prepared_row_with_dispatch_decision(tmp_path) -> None:
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
    database = tmp_path / "execstate.sqlite3"
    store.close()
    _execute(
        database,
        """
        UPDATE submissions SET dispatch_decision_json = ?
        WHERE submission_id = ? AND target_json = ?
        """,
        (decision.model_dump_json(), envelope.submission_id, target.model_dump_json()),
    )
    _assert_recovery_fails_closed(
        database, target, RECOVER_RESCAN_AT, expected_state="PREPARED"
    )


def test_recovery_rejects_claim_generation_mismatch(tmp_path) -> None:
    (
        store,
        _provenance,
        target,
        _record,
        _receipt,
        _recovery,
        claim,
        _envelope,
        _decision,
    ) = _dispatched(tmp_path)
    database = tmp_path / "execstate.sqlite3"
    store.close()
    document = json.loads(claim.model_dump_json())
    document["generation"] = 999
    tampered = json.dumps(document, sort_keys=True, separators=(",", ":"))
    assert tampered != claim.model_dump_json()
    _execute(database, "DROP TRIGGER claims_no_update")
    _execute(
        database,
        "UPDATE claims SET claim_json = ?, generation = 999 WHERE claim_id = ?",
        (tampered, claim.claim_id),
    )
    _execute(
        database,
        """CREATE TRIGGER claims_no_update
        BEFORE UPDATE ON claims
        BEGIN SELECT RAISE(ABORT, 'claims are immutable'); END""",
    )
    _assert_recovery_fails_closed(
        database, target, OUTCOME_AT, expected_state="DISPATCHING"
    )


def test_recovery_rejects_impossible_historical_run_coverage(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    database = tmp_path / "execstate.sqlite3"
    store.close()

    _rewrite_recovery_evidence(
        database,
        scanned_through_generation=999,
        unresolved_submission_ids=(),
    )
    before = _database_snapshot(database)
    reopened = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            reopened.recover(target, observed_at=OUTCOME_AT)
    finally:
        reopened.close()

    assert _database_snapshot(database) == before
    assert _stored_state(database, target) == "PREPARED"


def test_recovery_rejects_historical_unresolved_identity(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    database = tmp_path / "execstate.sqlite3"
    store.close()

    _rewrite_recovery_evidence(
        database,
        scanned_through_generation=1,
        unresolved_submission_ids=(
            "broker1-00000000000000000000000000000000",
        ),
    )
    before = _database_snapshot(database)
    reopened = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            reopened.recover(target, observed_at=OUTCOME_AT)
    finally:
        reopened.close()

    assert _database_snapshot(database) == before
    assert _stored_state(database, target) == "PREPARED"


def test_recovery_rejects_foreign_historical_epoch_identity(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    database = tmp_path / "execstate.sqlite3"
    store.close()

    _rewrite_recovery_evidence(
        database,
        scanned_through_generation=0,
        unresolved_submission_ids=(),
        recovery_epoch=2,
    )
    _execute(
        database,
        "UPDATE metadata SET value = '3' WHERE key = 'next_recovery_epoch'",
    )
    before = _database_snapshot(database)
    reopened = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            reopened.recover(target, observed_at=OUTCOME_AT)
    finally:
        reopened.close()
    assert _database_snapshot(database) == before


def test_recovery_rejects_stale_current_receipt_before_rebind(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target, prepared_at=PREPARE_AT)
    store.close()

    reopened = SQLiteExecutionStateStore(tmp_path / "execstate.sqlite3")
    recover_store(reopened, target, observed_at=PREPARE_AT)
    reopened.close()

    database = tmp_path / "execstate.sqlite3"
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        receipts = connection.execute(
            "SELECT receipt_id FROM commit_receipts ORDER BY receipt_sequence"
        ).fetchall()
        assert len(receipts) == 2
        stale_receipt_id = str(receipts[0]["receipt_id"])
        current_receipt_id = connection.execute(
            "SELECT current_receipt_id FROM submissions WHERE submission_id = ? AND target_json = ?",
            (target.submission_id, target.model_dump_json()),
        ).fetchone()["current_receipt_id"]
        assert current_receipt_id != stale_receipt_id
        connection.execute(
            """
            UPDATE submissions SET current_receipt_id = ?
            WHERE submission_id = ? AND target_json = ?
            """,
            (stale_receipt_id, target.submission_id, target.model_dump_json()),
        )
    finally:
        connection.close()

    before = _database_snapshot(database)
    corrupted = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            corrupted.recover(target, observed_at=OUTCOME_AT)
    finally:
        corrupted.close()

    assert _database_snapshot(database) == before
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        row = connection.execute(
            "SELECT current_receipt_id FROM submissions WHERE submission_id = ? AND target_json = ?",
            (target.submission_id, target.model_dump_json()),
        ).fetchone()
        assert row is not None
        assert row[0] == stale_receipt_id
    finally:
        connection.close()


def test_failed_recovery_rolls_back_every_partial_effect(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target_prepared = make_target(envelope.submission_id, account_reference="acct-keep")
    target_broken = make_target(envelope.submission_id, account_reference="acct-broken")
    store = make_store(tmp_path)
    recover_store(store, target_prepared)
    prepare_store(store, provenance, target_prepared)
    record_broken, receipt_broken = prepare_store(store, provenance, target_broken)
    claim_broken = store.claim(
        envelope.submission_id,
        target_broken,
        owner_id="worker-1",
        token="token-1",
        acquired_at=CLAIM_AT,
        expires_at=CLAIM_EXPIRES_AT,
    )
    rescan = recover_store(store, target_broken, observed_at=RECOVER_RESCAN_AT)
    decision = build_eligibility(
        store,
        provenance,
        target_broken,
        record_broken,
        receipt_broken,
        rescan,
        claim_broken,
    )
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    database = tmp_path / "execstate.sqlite3"
    store.close()
    _execute(
        database,
        """
        UPDATE submissions SET dispatch_decision_json = '{}'
        WHERE submission_id = ? AND target_json = ?
        """,
        (envelope.submission_id, target_broken.model_dump_json()),
    )
    before = _database_snapshot(database)

    reopened = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            reopened.recover(target_prepared, observed_at=OUTCOME_AT)
    finally:
        reopened.close()

    # epoch allocation, epoch rebind, dispatch-decision conversion and the
    # recovery-run row must all be rolled back together
    assert _database_snapshot(database) == before
    assert _stored_state(database, target_prepared) == "PREPARED"
    assert _stored_state(database, target_broken) == "DISPATCHING"


def test_valid_nonempty_database_recovers_completely(tmp_path) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    submission_id = envelope.submission_id
    accounts = {
        "prepared": "acct-prepared",
        "ack": "acct-ack",
        "rejected": "acct-rejected",
        "unknown": "acct-unknown",
        "no_effect": "acct-no-effect",
        "dispatching": "acct-dispatching",
        "reconciled": "acct-reconciled",
    }
    targets = {
        name: make_target(submission_id, account_reference=account)
        for name, account in accounts.items()
    }
    store = make_store(tmp_path)
    recover_store(store, targets["prepared"])
    built: dict[str, tuple[Any, Any, Any]] = {}
    for name, target in targets.items():
        record, receipt = store.prepare(
            envelope, provenance, target, prepared_at=PREPARE_AT
        )
        claim = store.claim(
            submission_id,
            target,
            owner_id="worker-1",
            token="token-1",
            acquired_at=CLAIM_AT,
            expires_at=CLAIM_EXPIRES_AT,
        )
        built[name] = (record, receipt, claim)
    rescans = {
        name: recover_store(store, target, observed_at=RECOVER_RESCAN_AT)
        for name, target in targets.items()
    }
    for name, target in targets.items():
        if name == "prepared":
            continue
        record, receipt, claim = built[name]
        decision = build_eligibility(
            store, provenance, target, record, receipt, rescans[name], claim
        )
        store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
        if name == "ack":
            store.persist_transport_outcome(
                BrokerSubmissionOutcome(submission_id=submission_id, status="ACCEPTED"),
                target,
                owner_id="worker-1",
                token="token-1",
                observed_at=OUTCOME_AT,
            )
        elif name == "rejected":
            store.persist_transport_outcome(
                BrokerSubmissionOutcome(submission_id=submission_id, status="REJECTED"),
                target,
                owner_id="worker-1",
                token="token-1",
                observed_at=OUTCOME_AT,
            )
        elif name == "unknown":
            store.persist_transport_outcome(
                BrokerSubmissionOutcome(
                    submission_id=submission_id, status="OUTCOME_UNKNOWN"
                ),
                target,
                owner_id="worker-1",
                token="token-1",
                observed_at=OUTCOME_AT,
            )
        elif name == "no_effect":
            store.persist_pre_dispatch_no_effect(
                submission_id,
                target,
                BrokerPreDispatchError("adapter-proof-1"),
                owner_id="worker-1",
                token="token-1",
                observed_at=OUTCOME_AT,
            )
        elif name == "reconciled":
            store.persist_transport_outcome(
                BrokerSubmissionOutcome(
                    submission_id=submission_id, status="OUTCOME_UNKNOWN"
                ),
                target,
                owner_id="worker-1",
                token="token-1",
                observed_at=OUTCOME_AT,
            )
            store.apply_reconciliation(
                submission_id,
                target,
                status="ACCEPTED",
                evidence_reference="reconciliation-1",
                observed_at=datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC),
            )
    store.close()

    reopened = make_store(tmp_path)
    evidence = recover_store(
        reopened, targets["prepared"], observed_at=datetime(2030, 1, 1, 12, 2, 3, tzinfo=UTC)
    )
    assert evidence.status == "COMPLETE"
    assert evidence.scanned_through_generation == 7
    assert evidence.unresolved_submission_ids == (submission_id,)
    expected = {
        "prepared": "PREPARED",
        "ack": "ACKNOWLEDGED",
        "rejected": "REJECTED",
        "unknown": "OUTCOME_UNKNOWN",
        "no_effect": "NO_EFFECT_CONFIRMED",
        "dispatching": "OUTCOME_UNKNOWN",
        "reconciled": "ACKNOWLEDGED",
    }
    for name, target in targets.items():
        record, receipt = reopened.read(submission_id, target)
        assert record.state == expected[name]
        assert receipt.recovery_epoch == record.recovery_epoch
    assert reopened._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()
