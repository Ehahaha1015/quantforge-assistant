"""Committed regressions for Reviewer-B blocker EXECSTATE-B-001 under v2.

The rejected v1 lineage inferred historical presence from a numeric comparison
between ``audit_events.event_sequence`` and ``recovery_runs.run_sequence``.
Those are independent AUTOINCREMENT domains, so the relation is not durable
ordering evidence.  Schema v2 orders history only through the shared
``audit_events.event_sequence`` recovery cut anchors.

These regressions preserve the original safety intent using real production
histories and then relational tampering:

* same-run conversion truth cannot be omitted or understated;
* sibling-target unresolved truth cannot be omitted;
* run-sequence magnitude is irrelevant to historical membership.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from _helpers import (
    OUTCOME_AT,
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
from quantforge.frontier import RecoveryCompleteEvidence

_TABLES = (
    "metadata",
    "submissions",
    "commit_receipts",
    "claims",
    "operational_payloads",
    "recovery_runs",
    "audit_events",
    "sqlite_sequence",
)
_LATE = OUTCOME_AT + timedelta(seconds=10)


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _snapshot(database: Path) -> dict[str, list[tuple[Any, ...]]]:
    connection = _connect(database)
    try:
        return {
            table: sorted(
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            )
            for table in _TABLES
        }
    finally:
        connection.close()


def _latest_run_sequence(database: Path) -> int:
    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT MAX(run_sequence) FROM recovery_runs"
        ).fetchone()
        assert row is not None and row[0] is not None
        return int(row[0])
    finally:
        connection.close()


def _rewrite_run_evidence(
    database: Path,
    *,
    run_sequence: int,
    unresolved: tuple[str, ...] | None = None,
    scanned: int | None = None,
) -> None:
    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT * FROM recovery_runs WHERE run_sequence = ?",
            (run_sequence,),
        ).fetchone()
        assert row is not None
        evidence = RecoveryCompleteEvidence.from_json(str(row["evidence_json"]))
        changes: dict[str, Any] = {}
        if unresolved is not None:
            changes["unresolved_submission_ids"] = unresolved
        if scanned is not None:
            changes["scanned_through_generation"] = scanned
        replacement = evidence.model_copy(update=changes)
        connection.execute("DROP TRIGGER recovery_runs_no_update")
        connection.execute(
            """
            UPDATE recovery_runs
            SET scanned_through_generation = ?, evidence_json = ?
            WHERE run_sequence = ?
            """,
            (
                replacement.scanned_through_generation,
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


def _move_run_key(
    database: Path, *, run_sequence: int, new_run_sequence: int
) -> None:
    """Move one committed run key to a distant value with its anchors."""

    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT evidence_json FROM recovery_runs WHERE run_sequence = ?",
            (run_sequence,),
        ).fetchone()
        assert row is not None
        evidence = RecoveryCompleteEvidence.from_json(str(row[0]))
        replacement = evidence.model_copy(
            update={"evidence_id": f"execstate-recovery-v1-{new_run_sequence}"}
        )
        connection.execute("DROP TRIGGER recovery_runs_no_update")
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute("DROP TRIGGER recovery_runs_require_cut")
        for anchor in connection.execute(
            "SELECT * FROM audit_events WHERE boundary_run_sequence = ?",
            (run_sequence,),
        ).fetchall():
            payload = json.loads(str(anchor["event_json"]))
            payload["boundary_run_sequence"] = new_run_sequence
            connection.execute(
                "UPDATE audit_events SET event_json = ? WHERE event_sequence = ?",
                (
                    json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ),
                    int(anchor["event_sequence"]),
                ),
            )
        connection.execute(
            "UPDATE audit_events SET boundary_run_sequence = ? "
            "WHERE boundary_run_sequence = ?",
            (new_run_sequence, run_sequence),
        )
        connection.execute(
            """
            UPDATE recovery_runs
            SET run_sequence = ?, evidence_id = ?, evidence_json = ?
            WHERE run_sequence = ?
            """,
            (
                new_run_sequence,
                replacement.evidence_id,
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
        connection.execute(
            """
            CREATE TRIGGER audit_events_no_update
            BEFORE UPDATE ON audit_events
            BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER recovery_runs_require_cut
            BEFORE INSERT ON recovery_runs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'recovery run requires exactly one BEGIN and one END cut anchor'
                )
                WHERE
                    (SELECT COUNT(*) FROM audit_events
                      WHERE boundary_run_sequence = NEW.run_sequence) <> 2
                    OR (SELECT COUNT(*) FROM audit_events
                         WHERE boundary_run_sequence = NEW.run_sequence
                           AND boundary_phase = 'BEGIN'
                           AND event_kind = 'RECOVERY_CUT_BEGIN'
                           AND reason = 'RECOVERY_CUT_BEGIN'
                           AND target_json = NEW.target_json
                           AND recovery_epoch = NEW.recovery_epoch
                           AND observed_at = NEW.observed_at) <> 1
                    OR (SELECT COUNT(*) FROM audit_events
                         WHERE boundary_run_sequence = NEW.run_sequence
                           AND boundary_phase = 'END'
                           AND event_kind = 'RECOVERY_CUT_END'
                           AND reason = 'RECOVERY_CUT_END'
                           AND target_json = NEW.target_json
                           AND recovery_epoch = NEW.recovery_epoch
                           AND observed_at = NEW.observed_at) <> 1
                    OR (SELECT event_sequence FROM audit_events
                         WHERE boundary_run_sequence = NEW.run_sequence
                           AND boundary_phase = 'BEGIN')
                       >=
                       (SELECT event_sequence FROM audit_events
                         WHERE boundary_run_sequence = NEW.run_sequence
                           AND boundary_phase = 'END');
            END
            """
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _build_conversion_history(
    tmp_path: Path,
) -> tuple[Path, str, Any, Any]:
    """Real history: target B dispatch converted by a target-A recovery cut."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    submission_id = envelope.submission_id
    target_a = make_target(submission_id, account_reference="acct-a")
    target_b = make_target(submission_id, account_reference="acct-b")
    store = make_store(tmp_path)
    recover_store(store, target_a)
    prepare_store(store, provenance, target_a, prepared_at=PREPARE_AT)
    record_b, receipt_b = prepare_store(
        store, provenance, target_b, prepared_at=PREPARE_AT
    )
    claim_b = claim_store(
        store,
        submission_id,
        target_b,
        owner_id="worker-b",
        token="token-b",
    )
    cover = recover_store(store, target_b, observed_at=RECOVER_RESCAN_AT)
    decision = build_eligibility(
        store, provenance, target_b, record_b, receipt_b, cover, claim_b
    )
    assert decision.eligible is True
    store.mark_dispatching(decision, owner_id="worker-b", token="token-b")
    conversion = store.recover(target_a, observed_at=OUTCOME_AT)
    assert conversion.unresolved_submission_ids == (submission_id,)
    store.close()
    return tmp_path / "execstate.sqlite3", submission_id, target_a, target_b


def _assert_corruption_no_mutation(database: Path, target: Any) -> None:
    before = _snapshot(database)
    store = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            store.recover(target, observed_at=_LATE)
    finally:
        store.close()
    assert _snapshot(database) == before


def test_same_run_conversion_positive_control(tmp_path) -> None:
    database, submission_id, target_a, _target_b = _build_conversion_history(
        tmp_path
    )
    reopened = SQLiteExecutionStateStore(database)
    try:
        evidence = reopened.recover(target_a, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
        assert evidence.scanned_through_generation == 2
        assert evidence.unresolved_submission_ids == (submission_id,)
    finally:
        reopened.close()


def test_same_run_conversion_unresolved_omission_fails_closed(tmp_path) -> None:
    database, _submission_id, target_a, _target_b = _build_conversion_history(
        tmp_path
    )
    _rewrite_run_evidence(
        database, run_sequence=_latest_run_sequence(database), unresolved=()
    )
    _assert_corruption_no_mutation(database, target_a)


def test_same_run_conversion_coverage_understatement_fails_closed(tmp_path) -> None:
    database, _submission_id, target_a, _target_b = _build_conversion_history(
        tmp_path
    )
    _rewrite_run_evidence(
        database, run_sequence=_latest_run_sequence(database), scanned=1
    )
    _assert_corruption_no_mutation(database, target_a)


def test_two_target_sibling_unresolved_omission_fails_closed(tmp_path) -> None:
    database, submission_id, target_a, target_b = _build_conversion_history(
        tmp_path
    )
    valid = SQLiteExecutionStateStore(database)
    try:
        evidence = valid.recover(target_a, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
        assert evidence.unresolved_submission_ids == (submission_id,)
    finally:
        valid.close()

    tampered = _build_conversion_history(tmp_path / "tampered")
    tampered_database, tampered_id, tampered_a, _tampered_b = tampered
    assert tampered_id == submission_id
    _rewrite_run_evidence(
        tampered_database,
        run_sequence=_latest_run_sequence(tampered_database),
        unresolved=(),
    )
    _assert_corruption_no_mutation(tampered_database, tampered_a)
    assert target_b.submission_id == target_a.submission_id


def test_counter_divergence_valid_history_recovers_complete(tmp_path) -> None:
    database, submission_id, target_a, _target_b = _build_conversion_history(
        tmp_path
    )
    latest = _latest_run_sequence(database)
    _move_run_key(
        database, run_sequence=latest, new_run_sequence=latest + 1000
    )
    connection = _connect(database)
    try:
        run_max = int(
            connection.execute(
                "SELECT MAX(run_sequence) FROM recovery_runs"
            ).fetchone()[0]
        )
        audit_max = int(
            connection.execute(
                "SELECT MAX(event_sequence) FROM audit_events"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert run_max > audit_max
    reopened = SQLiteExecutionStateStore(database)
    try:
        evidence = reopened.recover(target_a, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
        assert evidence.scanned_through_generation == 2
        assert evidence.unresolved_submission_ids == (submission_id,)
    finally:
        reopened.close()


def test_valid_history_with_audit_events_ahead_of_recovery_runs(tmp_path) -> None:
    database, submission_id, target_a, _target_b = _build_conversion_history(
        tmp_path
    )
    connection = _connect(database)
    try:
        audit_max = int(
            connection.execute(
                "SELECT MAX(event_sequence) FROM audit_events"
            ).fetchone()[0]
        )
        run_max = int(
            connection.execute(
                "SELECT MAX(run_sequence) FROM recovery_runs"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert audit_max > run_max
    reopened = SQLiteExecutionStateStore(database)
    try:
        evidence = reopened.recover(target_a, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
        assert evidence.unresolved_submission_ids == (submission_id,)
    finally:
        reopened.close()
