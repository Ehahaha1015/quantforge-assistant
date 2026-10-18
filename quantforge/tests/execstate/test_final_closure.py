"""Committed regressions for the final EXECSTATE-001 closure findings.

EXECSTATE-A-B2-RECOVERY-RUN-HISTORY
EXECSTATE-A-B2-DISPATCH-EVIDENCE-BINDING
EXECSTATE-A-B13-FORMAT-COMPATIBILITY
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _helpers import (
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
    ExecutionStateSchemaError,
    SQLiteExecutionStateStore,
)
from quantforge.frontier import (
    ExecutionTargetBinding,
    FrontierEligibilityDecision,
    RecoveryCompleteEvidence,
)

LATE_AT = datetime(2030, 1, 1, 12, 2, 3, tzinfo=UTC)


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _run_rows(database: Path) -> list[tuple[Any, ...]]:
    connection = _connect(database)
    try:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM recovery_runs ORDER BY run_sequence"
            ).fetchall()
        ]
    finally:
        connection.close()


def _rewrite_recovery_run(
    database: Path, run_sequence: int, **changes: Any
) -> None:
    """Rewrite one historical run through its canonical frozen model."""

    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT * FROM recovery_runs WHERE run_sequence = ?",
            (run_sequence,),
        ).fetchone()
        assert row is not None
        original = RecoveryCompleteEvidence.from_json(str(row["evidence_json"]))
        replacement = original.model_copy(update=changes)
        connection.execute("DROP TRIGGER recovery_runs_no_update")
        connection.execute(
            """
            UPDATE recovery_runs
            SET evidence_id = ?, recovery_epoch = ?,
                scanned_through_generation = ?, status = ?,
                observed_at = ?, evidence_json = ?
            WHERE run_sequence = ?
            """,
            (
                replacement.evidence_id,
                replacement.recovery_epoch,
                replacement.scanned_through_generation,
                replacement.status,
                replacement.observed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                replacement.model_dump_json(),
                run_sequence,
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


def _assert_recovery_rejects(
    database: Path,
    target: ExecutionTargetBinding,
    *,
    observed_at: datetime = LATE_AT,
) -> None:
    before = _run_rows(database)
    store = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            store.recover(target, observed_at=observed_at)
    finally:
        store.close()
    assert _run_rows(database) == before


def _prepared_with_rescan(
    tmp_path: Path,
) -> tuple[Any, Any, ExecutionTargetBinding, Any, Any]:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, _receipt = prepare_store(store, provenance, target)
    assert record.generation == 1
    rescan = recover_store(store, target, observed_at=RECOVER_RESCAN_AT)
    assert rescan.scanned_through_generation == 1
    return store, provenance, target, record, rescan


def _prepared_with_rebind(
    tmp_path: Path,
) -> tuple[Any, Any, ExecutionTargetBinding, Any, Any]:
    """Return a PREPARED record whose latest recovery run rebound its epoch.

    The RECOVERY_EPOCH_REBIND event at the run's exact epoch/instant is the
    durable proof that the record was in scope for that run; unlike the
    independent audit/recovery AUTOINCREMENT values, it is contract-backed.
    """

    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, _receipt = prepare_store(store, provenance, target)
    assert record.generation == 1
    store.close()
    reopened = make_store(tmp_path)
    rebind = recover_store(reopened, target, observed_at=RECOVER_RESCAN_AT)
    assert rebind.scanned_through_generation == 1
    assert rebind.recovery_epoch == 2
    return reopened, provenance, target, record, rebind


def _dispatched_database(
    tmp_path: Path,
) -> tuple[Path, ExecutionTargetBinding, FrontierEligibilityDecision]:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, receipt = prepare_store(store, provenance, target)
    claim = claim_store(store, envelope.submission_id, target)
    recovery = recover_store(store, target, observed_at=RECOVER_RESCAN_AT)
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    assert decision.eligible is True
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    database = tmp_path / "execstate.sqlite3"
    store.close()
    return database, target, decision


def _store_decision(
    database: Path,
    target: ExecutionTargetBinding,
    mutate: Any,
) -> None:
    connection = _connect(database)
    try:
        row = connection.execute(
            """
            SELECT dispatch_decision_json FROM submissions
            WHERE submission_id = ? AND target_json = ?
            """,
            (target.submission_id, target.model_dump_json()),
        ).fetchone()
        assert row is not None
        document = json.loads(str(row["dispatch_decision_json"]))
        mutate(document)
        tampered = FrontierEligibilityDecision.from_json(
            json.dumps(document, sort_keys=True, separators=(",", ":"))
        )
        assert tampered.eligible is True
        connection.execute(
            """
            UPDATE submissions SET dispatch_decision_json = ?
            WHERE submission_id = ? AND target_json = ?
            """,
            (
                tampered.model_dump_json(),
                target.submission_id,
                target.model_dump_json(),
            ),
        )
    finally:
        connection.close()


def _rewrite_table_sql(
    database: Path, table: str, old: str, new: str
) -> None:
    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        assert row is not None
        sql = str(row["sql"])
        assert old in sql
        version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql = ? WHERE type = 'table' AND name = ?",
            (sql.replace(old, new, 1), table),
        )
        connection.execute(f"PRAGMA schema_version = {version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
    finally:
        connection.close()


# --------------------------------------------------------------------------
# EXECSTATE-A-B2-RECOVERY-RUN-HISTORY
# --------------------------------------------------------------------------


def test_history_rejects_run_that_omits_a_covered_generation(tmp_path) -> None:
    store, _provenance, target, _record, _rebind = _prepared_with_rebind(tmp_path)
    database = tmp_path / "execstate.sqlite3"
    store.close()
    _rewrite_recovery_run(database, 2, scanned_through_generation=0)
    _assert_recovery_rejects(database, target)


def test_history_rejects_run_that_omits_unknown_identity(tmp_path) -> None:
    database, target, _decision = _dispatched_database(tmp_path)
    store = SQLiteExecutionStateStore(database)
    evidence = store.recover(target, observed_at=LATE_AT)
    assert evidence.status == "COMPLETE"
    assert evidence.unresolved_submission_ids == (target.submission_id,)
    store.close()
    _rewrite_recovery_run(database, 3, unresolved_submission_ids=())
    _assert_recovery_rejects(database, target)


def test_history_rejects_run_that_calls_prepared_unresolved(tmp_path) -> None:
    store, _provenance, target, _record, _rebind = _prepared_with_rebind(tmp_path)
    database = tmp_path / "execstate.sqlite3"
    store.close()
    _rewrite_recovery_run(
        database, 2, unresolved_submission_ids=(target.submission_id,)
    )
    _assert_recovery_rejects(database, target)


def test_multiple_generation_increasing_recovery_runs_recover_complete(
    tmp_path,
) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    submission_id = envelope.submission_id
    target_a = make_target(submission_id, account_reference="acct-a")
    target_b = make_target(submission_id, account_reference="acct-b")
    target_c = make_target(submission_id, account_reference="acct-c")
    store = make_store(tmp_path)
    first = recover_store(store, target_a)
    assert first.scanned_through_generation == 0
    prepare_store(store, provenance, target_a)
    second = recover_store(store, target_b, observed_at=RECOVER_RESCAN_AT)
    assert second.scanned_through_generation == 1
    prepare_store(
        store,
        provenance,
        target_b,
        prepared_at=RECOVER_RESCAN_AT + timedelta(seconds=1),
    )
    store.close()

    reopened = make_store(tmp_path)
    third = recover_store(reopened, target_c, observed_at=LATE_AT)
    assert third.status == "COMPLETE"
    assert third.scanned_through_generation == 2
    assert third.unresolved_submission_ids == ()
    reopened.close()


def test_legitimate_dispatching_ambiguity_history_recovers_complete(
    tmp_path,
) -> None:
    database, target, _decision = _dispatched_database(tmp_path)
    store = SQLiteExecutionStateStore(database)
    evidence = store.recover(target, observed_at=LATE_AT)
    assert evidence.status == "COMPLETE"
    assert evidence.scanned_through_generation == 1
    assert evidence.unresolved_submission_ids == (target.submission_id,)
    store.close()


# --------------------------------------------------------------------------
# EXECSTATE-A-B2-DISPATCH-EVIDENCE-BINDING
# --------------------------------------------------------------------------


def test_dispatch_decision_rejects_foreign_receipt_artifact(tmp_path) -> None:
    database, target, _decision = _dispatched_database(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        document["commit_receipt"]["committed_at"] = (
            PREPARE_AT + timedelta(microseconds=1)
        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    _store_decision(database, target, mutate)
    _assert_recovery_rejects(database, target)


def test_dispatch_decision_rejects_foreign_recovery_evidence(tmp_path) -> None:
    database, target, _decision = _dispatched_database(tmp_path)

    def mutate(document: dict[str, Any]) -> None:
        document["recovery_evidence"]["evidence_id"] = (
            "execstate-recovery-v1-foreign"
        )

    _store_decision(database, target, mutate)
    _assert_recovery_rejects(database, target)


def test_dispatch_decision_rejects_foreign_submission_evidence(tmp_path) -> None:
    source = make_provenance()
    envelope = source.pre_dispatch_decision.envelope
    source_target = make_target(
        envelope.submission_id, account_reference="acct-source"
    )
    kept_target = make_target(
        envelope.submission_id, account_reference="acct-keep"
    )
    store = make_store(tmp_path)
    recover_store(store, source_target)
    source_record, source_receipt = prepare_store(store, source, source_target)
    source_claim = claim_store(store, envelope.submission_id, source_target)
    source_recovery = recover_store(
        store, source_target, observed_at=RECOVER_RESCAN_AT
    )
    source_decision = build_eligibility(
        store,
        source,
        source_target,
        source_record,
        source_receipt,
        source_recovery,
        source_claim,
    )
    kept_record, kept_receipt = prepare_store(store, source, kept_target)
    kept_claim = claim_store(store, envelope.submission_id, kept_target)
    kept_recovery = recover_store(
        store, kept_target, observed_at=RECOVER_RESCAN_AT
    )
    kept_decision = build_eligibility(
        store,
        source,
        kept_target,
        kept_record,
        kept_receipt,
        kept_recovery,
        kept_claim,
    )
    store.mark_dispatching(kept_decision, owner_id="worker-1", token="token-1")
    database = tmp_path / "execstate.sqlite3"
    store.close()

    connection = _connect(database)
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
    _assert_recovery_rejects(database, kept_target)


def test_valid_real_dispatch_history_recovers_complete(tmp_path) -> None:
    database, target, _decision = _dispatched_database(tmp_path)
    store = SQLiteExecutionStateStore(database)
    evidence = store.recover(target, observed_at=LATE_AT)
    assert evidence.status == "COMPLETE"
    assert evidence.unresolved_submission_ids == (target.submission_id,)
    record, receipt = store.read(target.submission_id, target)
    assert record.state == "OUTCOME_UNKNOWN"
    assert receipt.recovery_epoch == record.recovery_epoch
    store.close()


# --------------------------------------------------------------------------
# EXECSTATE-A-B13-FORMAT-COMPATIBILITY
# --------------------------------------------------------------------------


def test_harmless_ddl_whitespace_is_accepted_on_reopen(tmp_path) -> None:
    store = make_store(tmp_path)
    store.close()
    database = tmp_path / "execstate.sqlite3"
    _rewrite_table_sql(
        database, "metadata", "CREATE TABLE metadata (", "CREATE TABLE metadata("
    )
    reopened = SQLiteExecutionStateStore(database)
    try:
        row = reopened._connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        assert row is not None
        assert row[0] == "2"
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("CHECK (generation >= 1)", ""),
        ("CHECK (generation >= 1)", "CHECK (generation > 1)"),
        ("CHECK (generation >= 1)", "CHECK (generation >= 2)"),
    ],
)
def test_semantic_ddl_damage_still_fails_closed(
    tmp_path, old: str, new: str
) -> None:
    store = make_store(tmp_path)
    store.close()
    database = tmp_path / "execstate.sqlite3"
    _rewrite_table_sql(database, "submissions", old, new)
    with pytest.raises(ExecutionStateSchemaError):
        SQLiteExecutionStateStore(database)
