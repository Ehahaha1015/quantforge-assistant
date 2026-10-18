"""Mandatory schema-v2 oracles AQ through BE for EXECSTATE-001.

Every oracle builds its positive history through the real public API and then
tampers narrowly selected durable fields so the expected verdict is falsifiable
without re-deriving historical truth from the helper under test.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from _helpers import (
    OUTCOME_AT,
    PREPARE_AT,
    RECOVER_AT,
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
from quantforge.frontier import RecoveryCompleteEvidence, evaluate_frontier_eligibility

_TABLES = (
    "metadata",
    "submissions",
    "commit_receipts",
    "claims",
    "operational_payloads",
    "recovery_runs",
    "audit_events",
)
_LATE = OUTCOME_AT + timedelta(seconds=30)
_WORKER = Path(__file__).resolve().parent / "_v2_oracle_worker.py"
_CUT_WORKER = Path(__file__).resolve().parent / "_v2_cut_crash_worker.py"

_TRIGGER_DDL = {
    "audit_events_no_update": (
        "CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END"
    ),
    "audit_events_no_delete": (
        "CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END"
    ),
    "recovery_runs_no_update": (
        "CREATE TRIGGER recovery_runs_no_update BEFORE UPDATE ON recovery_runs "
        "BEGIN SELECT RAISE(ABORT, 'recovery runs are append-only'); END"
    ),
    "recovery_runs_no_delete": (
        "CREATE TRIGGER recovery_runs_no_delete BEFORE DELETE ON recovery_runs "
        "BEGIN SELECT RAISE(ABORT, 'recovery runs are append-only'); END"
    ),
}


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
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


def _drop_protection(connection: sqlite3.Connection) -> None:
    for name in _TRIGGER_DDL:
        connection.execute(f"DROP TRIGGER {name}")


def _restore_protection(connection: sqlite3.Connection) -> None:
    for statement in _TRIGGER_DDL.values():
        connection.execute(statement)


def _latest_run(database: Path) -> int:
    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT MAX(run_sequence) FROM recovery_runs"
        ).fetchone()
        assert row is not None and row[0] is not None
        return int(row[0])
    finally:
        connection.close()


def _tamper_evidence(
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
        connection.execute(_TRIGGER_DDL["recovery_runs_no_update"])
    finally:
        connection.close()


def _boundary_rows(database: Path, run_sequence: int) -> list[sqlite3.Row]:
    connection = _connect(database)
    try:
        return connection.execute(
            "SELECT * FROM audit_events WHERE boundary_run_sequence = ? "
            "ORDER BY event_sequence",
            (run_sequence,),
        ).fetchall()
    finally:
        connection.close()


def _cut_positions(database: Path, run_sequence: int) -> tuple[int, int]:
    rows = _boundary_rows(database, run_sequence)
    begin = next(row for row in rows if str(row["boundary_phase"]) == "BEGIN")
    end = next(row for row in rows if str(row["boundary_phase"]) == "END")
    return int(begin["event_sequence"]), int(end["event_sequence"])


def _build_prepared_history(tmp_path: Path) -> tuple[Path, str, Any]:
    """Recover, prepare, then a covering rescan at PREPARE_AT."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    submission_id = envelope.submission_id
    target = make_target(submission_id)
    store = make_store(tmp_path)
    recover_store(store, target, observed_at=RECOVER_AT)
    prepare_store(store, provenance, target, prepared_at=PREPARE_AT)
    recover_store(store, target, observed_at=PREPARE_AT)
    store.close()
    return tmp_path / "execstate.sqlite3", submission_id, target


def _build_dispatching_history(
    tmp_path: Path,
) -> tuple[Path, str, Any]:
    """Prepare, claim, mark DISPATCHING, then convert in a later run cut."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    submission_id = envelope.submission_id
    target = make_target(submission_id)
    store = make_store(tmp_path)
    recover_store(store, target, observed_at=RECOVER_AT)
    record, receipt = prepare_store(store, provenance, target, prepared_at=PREPARE_AT)
    claim = claim_store(
        store,
        submission_id,
        target,
        owner_id="worker-1",
        token="token-1",
    )
    cover = recover_store(store, target, observed_at=RECOVER_RESCAN_AT)
    decision = build_eligibility(
        store, provenance, target, record, receipt, cover, claim
    )
    assert decision.eligible is True
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    store.close()
    return tmp_path / "execstate.sqlite3", submission_id, target


def _assert_corruption_no_mutation(database: Path, target: Any) -> None:
    before = _snapshot(database)
    store = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            store.recover(target, observed_at=_LATE)
    finally:
        store.close()
    assert _snapshot(database) == before


def test_aq_h_valid_equal_instant_run_before_prepare(tmp_path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    submission_id = envelope.submission_id
    target = make_target(submission_id)
    store = make_store(tmp_path)
    recover_store(store, target, observed_at=RECOVER_AT)
    zero_mutation = recover_store(store, target, observed_at=PREPARE_AT)
    assert zero_mutation.scanned_through_generation == 0
    assert zero_mutation.unresolved_submission_ids == ()
    prepare_store(store, provenance, target, prepared_at=PREPARE_AT)
    store.close()

    database = tmp_path / "execstate.sqlite3"
    rows = _boundary_rows(database, 2)
    assert len(rows) == 2
    reopened = SQLiteExecutionStateStore(database)
    try:
        evidence = reopened.recover(target, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
        connection = _connect(database)
        try:
            stored = connection.execute(
                "SELECT scanned_through_generation, evidence_json FROM recovery_runs "
                "WHERE run_sequence = 2"
            ).fetchone()
        finally:
            connection.close()
        assert int(stored["scanned_through_generation"]) == 0
        stored_evidence = RecoveryCompleteEvidence.from_json(str(stored["evidence_json"]))
        assert stored_evidence.unresolved_submission_ids == ()
        assert evidence.unresolved_submission_ids == ()
    finally:
        reopened.close()
    assert submission_id


def test_ar_h_corrupt_equal_instant_coverage_omission(tmp_path) -> None:
    database, _submission_id, target = _build_prepared_history(tmp_path)
    _tamper_evidence(database, run_sequence=_latest_run(database), scanned=0)
    _assert_corruption_no_mutation(database, target)


def test_as_prepared_falsely_reported_unresolved(tmp_path) -> None:
    database, submission_id, target = _build_prepared_history(tmp_path)
    _tamper_evidence(
        database,
        run_sequence=_latest_run(database),
        unresolved=(submission_id,),
    )
    _assert_corruption_no_mutation(database, target)


def test_at_same_run_unknown_conversion_truth(tmp_path) -> None:
    database, submission_id, target = _build_dispatching_history(tmp_path)
    store = SQLiteExecutionStateStore(database)
    try:
        evidence = store.recover(target, observed_at=OUTCOME_AT)
        assert evidence.unresolved_submission_ids == (submission_id,)
    finally:
        store.close()
    _tamper_evidence(
        database, run_sequence=_latest_run(database), unresolved=()
    )
    _assert_corruption_no_mutation(database, target)


def test_au_coverage_understatement_and_overstatement(tmp_path) -> None:
    understated = _build_prepared_history(tmp_path / "under")
    _tamper_evidence(understated[0], run_sequence=_latest_run(understated[0]), scanned=0)
    _assert_corruption_no_mutation(understated[0], understated[2])

    overstated = _build_prepared_history(tmp_path / "over")
    _tamper_evidence(overstated[0], run_sequence=_latest_run(overstated[0]), scanned=9)
    _assert_corruption_no_mutation(overstated[0], overstated[2])


def test_au_post_end_generation_does_not_affect_earlier_run(tmp_path) -> None:
    database, submission_id, target = _build_prepared_history(tmp_path)
    provenance = make_provenance()
    second_target = make_target(submission_id, account_reference="acct-later")
    store = SQLiteExecutionStateStore(database)
    try:
        store.recover(target, observed_at=PREPARE_AT)
        prepare_store(
            store, provenance, second_target, prepared_at=PREPARE_AT
        )
    finally:
        store.close()
    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT scanned_through_generation, evidence_json FROM recovery_runs "
            "WHERE run_sequence = 2"
        ).fetchone()
    finally:
        connection.close()
    assert int(row["scanned_through_generation"]) == 1
    reopened = SQLiteExecutionStateStore(database)
    try:
        evidence = reopened.recover(target, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
        assert evidence.scanned_through_generation == 2
    finally:
        reopened.close()


def test_av_sibling_target_unresolved_truth(tmp_path) -> None:
    database, submission_id, target = _build_dispatching_history(tmp_path)
    sibling_database = tmp_path / "sibling"
    sibling_database.mkdir()
    sibling_path, sibling_id, sibling_target = _build_dispatching_history(
        sibling_database
    )
    assert sibling_id == submission_id
    valid = SQLiteExecutionStateStore(sibling_path)
    try:
        evidence = valid.recover(sibling_target, observed_at=OUTCOME_AT)
        assert evidence.unresolved_submission_ids == (sibling_id,)
    finally:
        valid.close()
    _tamper_evidence(
        sibling_path, run_sequence=_latest_run(sibling_path), unresolved=()
    )
    _assert_corruption_no_mutation(sibling_path, sibling_target)
    assert database.exists() and target.submission_id == sibling_target.submission_id


def test_aw_multiple_same_epoch_runs_are_distinguished(tmp_path) -> None:
    database, submission_id, target = _build_dispatching_history(tmp_path)
    reopened = SQLiteExecutionStateStore(database)
    try:
        evidence = reopened.recover(target, observed_at=OUTCOME_AT)
        assert evidence.unresolved_submission_ids == (submission_id,)
    finally:
        reopened.close()
    connection = _connect(database)
    try:
        runs = [
            int(row[0])
            for row in connection.execute(
                "SELECT run_sequence FROM recovery_runs ORDER BY run_sequence"
            ).fetchall()
        ]
        conversion = connection.execute(
            "SELECT event_sequence FROM audit_events WHERE event_kind = "
            "'RECOVERY_UNKNOWN_CONVERSION'"
        ).fetchone()
    finally:
        connection.close()
    assert len(runs) >= 3
    previous_end = -1
    for run in runs:
        begin, end = _cut_positions(database, run)
        assert begin < end
        assert begin > previous_end
        previous_end = end
    assert conversion is not None
    final_begin, final_end = _cut_positions(database, runs[-1])
    assert final_begin < int(conversion[0]) < final_end


def test_ax_zero_mutation_and_empty_database_cuts(tmp_path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    empty = recover_store(store, target, observed_at=RECOVER_AT)
    assert empty.scanned_through_generation == 0
    prepare_store(
        store,
        provenance,
        target,
        prepared_at=PREPARE_AT,
    )
    zero = recover_store(store, target, observed_at=PREPARE_AT)
    assert zero.scanned_through_generation == 1
    assert zero.unresolved_submission_ids == ()
    store.close()
    database = tmp_path / "execstate.sqlite3"
    connection = _connect(database)
    try:
        run_count = int(
            connection.execute("SELECT COUNT(*) FROM recovery_runs").fetchone()[0]
        )
        anchor_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE row_scope = "
                "'RECOVERY_BOUNDARY'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert run_count == 2
    assert anchor_count == 4
    reopened = SQLiteExecutionStateStore(database)
    try:
        evidence = reopened.recover(target, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
        assert evidence.scanned_through_generation == 1
    finally:
        reopened.close()


def test_ay_boundary_tamper_matrix(tmp_path) -> None:
    cases: list[tuple[str, Callable[[sqlite3.Connection, int], None]]] = [
        ("missing_begin", lambda c, run: c.execute(
            "DELETE FROM audit_events WHERE boundary_run_sequence = ? "
            "AND boundary_phase = 'BEGIN'", (run,))),
        ("missing_end", lambda c, run: c.execute(
            "DELETE FROM audit_events WHERE boundary_run_sequence = ? "
            "AND boundary_phase = 'END'", (run,))),
        ("wrong_target", lambda c, run: c.execute(
            "UPDATE audit_events SET target_json = ? WHERE boundary_run_sequence = ? "
            "AND boundary_phase = 'BEGIN'", ('{"foreign":true}', run))),
        ("reversed_cut", lambda c, run: c.execute(
            "UPDATE audit_events SET event_sequence = ? WHERE boundary_run_sequence = ? "
            "AND boundary_phase = 'BEGIN'",
            (
                int(c.execute(
                    "SELECT MAX(event_sequence) FROM audit_events"
                ).fetchone()[0]) + 1,
                run,
            ))),
    ]
    for name, mutation in cases:
        case_dir = tmp_path / name
        database, _submission_id, target = _build_prepared_history(case_dir)
        run = _latest_run(database)
        connection = _connect(database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            _drop_protection(connection)
            mutation(connection, run)
            _restore_protection(connection)
            connection.commit()
        finally:
            connection.close()
        _assert_corruption_no_mutation(database, target)

    blocked = _build_prepared_history(tmp_path / "scope")
    connection = _connect(blocked[0])
    try:
        connection.execute("BEGIN IMMEDIATE")
        for name in ("audit_events_no_update", "audit_events_no_delete"):
            connection.execute(f"DROP TRIGGER {name}")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE audit_events SET row_scope = 'SUBMISSION' "
                "WHERE boundary_phase = 'BEGIN'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE audit_events SET event_kind = 'RECOVERY_CUT_END' "
                "WHERE boundary_phase = 'BEGIN'"
            )
        connection.rollback()
    finally:
        connection.close()


def test_ay_required_recovery_event_outside_cut_fails_closed(tmp_path) -> None:
    database, _submission_id, target = _build_dispatching_history(tmp_path)
    store = SQLiteExecutionStateStore(database)
    try:
        store.recover(target, observed_at=OUTCOME_AT)
    finally:
        store.close()
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _drop_protection(connection)
        maximum = int(
            connection.execute(
                "SELECT MAX(event_sequence) FROM audit_events"
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE audit_events SET event_sequence = ? WHERE event_kind = "
            "'RECOVERY_UNKNOWN_CONVERSION'",
            (maximum + 1,),
        )
        _restore_protection(connection)
        connection.commit()
    finally:
        connection.close()
    _assert_corruption_no_mutation(database, target)


def test_ay_invalid_cut_cannot_authorize_eligibility(tmp_path) -> None:
    database, _submission_id, target = _build_dispatching_history(tmp_path)
    connection = _connect(database)
    try:
        decision_json = connection.execute(
            "SELECT dispatch_decision_json FROM submissions"
        ).fetchone()[0]
        evidence_id = json.loads(str(decision_json))["recovery_evidence"][
            "evidence_id"
        ]
        run = int(
            connection.execute(
                "SELECT run_sequence FROM recovery_runs WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()[0]
        )
        connection.execute("BEGIN IMMEDIATE")
        _drop_protection(connection)
        connection.execute(
            "DELETE FROM audit_events WHERE boundary_run_sequence = ? "
            "AND boundary_phase = 'END'",
            (run,),
        )
        _restore_protection(connection)
        connection.commit()
    finally:
        connection.close()
    _assert_corruption_no_mutation(database, target)


def test_ay_orphan_boundary_fails_closed(tmp_path) -> None:
    database, _submission_id, target = _build_prepared_history(tmp_path)
    connection = _connect(database)
    error: sqlite3.Error | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        _drop_protection(connection)
        payload = {
            "event_sequence": 10_000,
            "row_scope": "RECOVERY_BOUNDARY",
            "target_json": target.model_dump_json(),
            "recovery_epoch": 1,
            "event_kind": "RECOVERY_CUT_BEGIN",
            "boundary_phase": "BEGIN",
            "boundary_run_sequence": 999,
            "observed_at": PREPARE_AT.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "reason": "RECOVERY_CUT_BEGIN",
        }
        connection.execute(
            """
            INSERT INTO audit_events(
                event_sequence, row_scope, submission_id, target_json,
                prior_state, new_state, generation, recovery_epoch, event_kind,
                reason, observed_at, owner_id, token_reference, claim_id,
                payload_sequence, status, boundary_run_sequence, boundary_phase,
                event_json
            ) VALUES (10000, 'RECOVERY_BOUNDARY', NULL, ?, NULL, NULL, NULL, 1,
                      'RECOVERY_CUT_BEGIN', 'RECOVERY_CUT_BEGIN', ?, NULL, NULL,
                      NULL, NULL, NULL, 999, 'BEGIN', ?)
            """,
            (
                target.model_dump_json(),
                payload["observed_at"],
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )
        try:
            connection.commit()
        except sqlite3.IntegrityError as exc:
            error = exc
            connection.rollback()
    finally:
        connection.rollback()
        connection.close()
    assert error is not None


def test_az_counter_divergence_is_irrelevant(tmp_path) -> None:
    database, submission_id, target = _build_prepared_history(tmp_path)
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _drop_protection(connection)
        maximum = int(
            connection.execute(
                "SELECT MAX(run_sequence) FROM recovery_runs"
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE recovery_runs SET run_sequence = run_sequence + 5000"
        )
        connection.execute(
            "UPDATE audit_events SET boundary_run_sequence = "
            "boundary_run_sequence + 5000 WHERE boundary_run_sequence IS NOT NULL"
        )
        for anchor in connection.execute(
            "SELECT event_sequence, event_json FROM audit_events "
            "WHERE boundary_run_sequence IS NOT NULL"
        ).fetchall():
            payload = json.loads(str(anchor["event_json"]))
            payload["boundary_run_sequence"] = (
                int(payload["boundary_run_sequence"]) + 5000
            )
            connection.execute(
                "UPDATE audit_events SET event_json = ? WHERE event_sequence = ?",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    int(anchor["event_sequence"]),
                ),
            )
        for row in connection.execute(
            "SELECT run_sequence, evidence_json FROM recovery_runs"
        ).fetchall():
            payload = json.loads(str(row["evidence_json"]))
            payload["evidence_id"] = f"execstate-recovery-v1-{row['run_sequence']}"
            connection.execute(
                "UPDATE recovery_runs SET evidence_id = ?, evidence_json = ? "
                "WHERE run_sequence = ?",
                (
                    payload["evidence_id"],
                    json.dumps(
                        payload, sort_keys=True, separators=(",", ":")
                    ),
                    int(row["run_sequence"]),
                ),
            )
        _restore_protection(connection)
        connection.commit()
    finally:
        connection.close()
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
    assert run_max > audit_max + maximum
    reopened = SQLiteExecutionStateStore(database)
    try:
        evidence = reopened.recover(target, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
        assert evidence.unresolved_submission_ids == ()
    finally:
        reopened.close()
    assert submission_id


def test_read_audit_returns_only_submission_rows(tmp_path) -> None:
    database, submission_id, target = _build_dispatching_history(tmp_path)
    store = SQLiteExecutionStateStore(database)
    try:
        store.recover(target, observed_at=OUTCOME_AT)
        events = store.read_audit(submission_id, target)
    finally:
        store.close()
    assert events
    for value in events:
        assert json.loads(value)["row_scope"] == "SUBMISSION"
    connection = _connect(database)
    try:
        boundary_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE row_scope = 'RECOVERY_BOUNDARY'"
            ).fetchone()[0]
        )
        submission_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE row_scope = 'SUBMISSION' AND submission_id = ?",
                (submission_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert boundary_rows >= 2
    assert len(events) == submission_rows


def test_ba_pre_existing_corruption_zero_mutation(tmp_path) -> None:
    database, _submission_id, target = _build_prepared_history(tmp_path)
    _tamper_evidence(database, run_sequence=_latest_run(database), scanned=0)
    before = _snapshot(database)
    store = SQLiteExecutionStateStore(database)
    try:
        with pytest.raises(ExecutionStateCorruptionError):
            store.recover(target, observed_at=_LATE)
    finally:
        store.close()
    after = _snapshot(database)
    assert after == before
    assert after["recovery_runs"] == before["recovery_runs"]


def _run_worker(database: Path, target: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_WORKER),
            str(database),
            target.model_dump_json(),
            _LATE.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_bb_fresh_process_reopen_reproduces_validation(tmp_path) -> None:
    database, submission_id, target = _build_dispatching_history(tmp_path)
    first = _run_worker(database, target)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["unresolved_submission_ids"] == [submission_id]
    second = _run_worker(database, target)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["status"] == "COMPLETE"


def test_bc_post_reopen_historical_tamper_fails_closed(tmp_path) -> None:
    database, _submission_id, target = _build_prepared_history(tmp_path)
    assert _run_worker(database, target).returncode == 0
    _tamper_evidence(database, run_sequence=_latest_run(database), scanned=0)
    result = _run_worker(database, target)
    assert result.returncode != 0
    assert "ExecutionStateCorruptionError" in result.stderr + result.stdout


def _run_count(database: Path) -> int:
    connection = _connect(database)
    try:
        return int(
            connection.execute("SELECT COUNT(*) FROM recovery_runs").fetchone()[0]
        )
    finally:
        connection.close()


def _integrity(database: Path) -> tuple[str, int]:
    connection = _connect(database)
    try:
        status = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        return status, foreign_keys
    finally:
        connection.close()


def _run_cut_worker(database: Path, target: Any, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_CUT_WORKER),
            str(database),
            target.model_dump_json(),
            RECOVER_RESCAN_AT.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            mode,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_recovery_cut_process_death_windows(tmp_path) -> None:
    database, _submission_id, target = _build_prepared_history(tmp_path)
    before = _run_count(database)
    uncommitted = _run_cut_worker(database, target, "uncommitted")
    assert uncommitted.returncode == 3
    assert _run_count(database) == before
    assert _integrity(database) == ("ok", 0)
    store = SQLiteExecutionStateStore(database)
    try:
        evidence = store.recover(target, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
    finally:
        store.close()

    committed_dir = tmp_path / "committed"
    committed_database, _id, committed_target = _build_prepared_history(committed_dir)
    committed_before = _run_count(committed_database)
    committed = _run_cut_worker(committed_database, committed_target, "committed")
    assert committed.returncode == 0
    assert _run_count(committed_database) == committed_before + 1
    assert _integrity(committed_database) == ("ok", 0)
    reopened = SQLiteExecutionStateStore(committed_database)
    try:
        evidence = reopened.recover(committed_target, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
        assert evidence.scanned_through_generation == 1
    finally:
        reopened.close()


def test_bd_valid_complex_positive_history(tmp_path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    provenance_a = make_provenance()
    provenance_b = _provenance_variant(1)
    provenance_c = _provenance_variant(2)
    submission_a = provenance_a.pre_dispatch_decision.envelope.submission_id
    submission_b = provenance_b.pre_dispatch_decision.envelope.submission_id
    submission_c = provenance_c.pre_dispatch_decision.envelope.submission_id
    assert len({submission_a, submission_b, submission_c}) == 3
    target_a = make_target(submission_a, account_reference="acct-a")
    target_b = make_target(submission_b, account_reference="acct-b")
    target_c = make_target(submission_c, account_reference="acct-c")
    store = make_store(tmp_path)
    try:
        recover_store(store, target_a, observed_at=RECOVER_AT)
        record_a, _receipt_a = prepare_store(
            store, provenance_a, target_a, prepared_at=PREPARE_AT
        )
        prepare_store(store, provenance_a, target_a, prepared_at=PREPARE_AT)
        record_b, receipt_b = prepare_store(
            store, provenance_b, target_b, prepared_at=PREPARE_AT
        )
        claim_b = claim_store(
            store, submission_b, target_b, owner_id="w-b", token="t-b"
        )
        cover_b = recover_store(store, target_b, observed_at=RECOVER_RESCAN_AT)
        decision_b = build_eligibility(
            store, provenance_b, target_b, record_b, receipt_b, cover_b, claim_b
        )
        store.mark_dispatching(decision_b, owner_id="w-b", token="t-b")
        record_c, receipt_c = prepare_store(
            store, provenance_c, target_c, prepared_at=PREPARE_AT
        )
        claim_c = claim_store(
            store, submission_c, target_c, owner_id="w-c", token="t-c"
        )
        cover_c = recover_store(
            store,
            target_c,
            observed_at=RECOVER_RESCAN_AT + timedelta(seconds=3),
        )
        decision_c = build_eligibility(
            store, provenance_c, target_c, record_c, receipt_c, cover_c, claim_c
        )
        store.mark_dispatching(decision_c, owner_id="w-c", token="t-c")
        outcome = store.persist_transport_outcome(
            _accepted_outcome(submission_c),
            target_c,
            owner_id="w-c",
            token="t-c",
            observed_at=OUTCOME_AT,
        )
        assert outcome.state == "ACKNOWLEDGED"
        conversion = store.recover(target_a, observed_at=OUTCOME_AT)
        assert conversion.unresolved_submission_ids == (submission_b,)
        assert record_a.generation == 1
    finally:
        store.close()
    reopened = SQLiteExecutionStateStore(tmp_path / "execstate.sqlite3")
    try:
        evidence = reopened.recover(target_a, observed_at=_LATE)
        assert evidence.status == "COMPLETE"
        assert evidence.scanned_through_generation == 3
        assert evidence.unresolved_submission_ids == (submission_b,)
    finally:
        reopened.close()


def _provenance_variant(increment: int) -> Any:
    """Build one more independent fresh-risk provenance identity."""

    from datetime import UTC, datetime
    from decimal import Decimal

    from _helpers import AUTHORIZED_AT, DISPATCH_AT, ORDER_AT

    from quantforge.broker import create_broker_submission_envelope
    from quantforge.domain import OrderIntent, Position
    from quantforge.live import authorize_live_order, create_live_authorization
    from quantforge.portfolio import PortfolioState
    from quantforge.predispatch import (
        AuthorizationFreshnessPolicy,
        SubmissionHistoryState,
    )
    from quantforge.provenance import evaluate_pre_dispatch_with_provenance
    from quantforge.risk import RiskDecision, RiskLimits

    order = OrderIntent(
        strategy_id="alpha",
        symbol="AAPL",
        timestamp=ORDER_AT,
        side="BUY",
        quantity=Decimal(5 + increment),
    )
    authorization = create_live_authorization(order, "operator-7", AUTHORIZED_AT)
    permission = authorize_live_order(order, RiskDecision(approved=True), authorization)
    envelope = create_broker_submission_envelope(order, permission)
    portfolio = PortfolioState(
        initial_cash=Decimal("1000.00"),
        cash=Decimal("1000.00"),
        positions=(
            Position(
                symbol="MSFT",
                quantity=Decimal("2.0"),
                average_price=Decimal("50.00"),
            ),
        ),
        cumulative_fees=Decimal("0.00"),
        last_fill_timestamp=datetime(2030, 1, 1, 11, 59, tzinfo=UTC),
    )
    return evaluate_pre_dispatch_with_provenance(
        envelope,
        SubmissionHistoryState(
            submission_id=envelope.submission_id,
            state="NEVER_DISPATCHED",
        ),
        AuthorizationFreshnessPolicy(max_authorization_age=timedelta(minutes=5)),
        DISPATCH_AT,
        portfolio,
        {"MSFT": Decimal("50.0"), "AAPL": Decimal("100.00")},
        Decimal("1.00"),
        RiskLimits(
            max_order_notional=Decimal("1000.00"),
            max_position_notional=Decimal("1000.0"),
            max_gross_exposure=Decimal("2000.000"),
        ),
    )


def _accepted_outcome(submission_id: str) -> Any:
    from quantforge.broker import BrokerSubmissionOutcome

    return BrokerSubmissionOutcome(
        submission_id=submission_id,
        status="ACCEPTED",
        broker_reference="broker-ref-1",
        broker_message=None,
    )


def test_be_real_frontier_composition(tmp_path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    provenance = make_provenance()
    submission_id = provenance.pre_dispatch_decision.envelope.submission_id
    target = make_target(submission_id, account_reference="acct-frontier")
    store = make_store(tmp_path)
    try:
        recover_store(store, target, observed_at=RECOVER_AT)
        record, receipt = prepare_store(
            store, provenance, target, prepared_at=PREPARE_AT
        )
        claim = claim_store(
            store, submission_id, target, owner_id="w-a", token="t-a"
        )
        cover = recover_store(store, target, observed_at=RECOVER_RESCAN_AT)
        decision = build_eligibility(store, provenance, target, record, receipt, cover, claim)
        direct = evaluate_frontier_eligibility(
            provenance,
            provenance.pre_dispatch_decision.evaluation_timestamp,
            target,
            price_evidence=decision.price_evidence,
            account_evidence=decision.account_evidence,
            durable_record=record,
            commit_receipt=receipt,
            recovery_evidence=cover,
            operator_evidence=decision.operator_evidence,
            capability=decision.capability,
            claim=claim,
        )
        assert direct.eligible is True
        foreign = evaluate_frontier_eligibility(
            provenance,
            provenance.pre_dispatch_decision.evaluation_timestamp,
            make_target(submission_id, account_reference="acct-foreign"),
            price_evidence=decision.price_evidence,
            account_evidence=decision.account_evidence,
            durable_record=record,
            commit_receipt=receipt,
            recovery_evidence=cover,
            operator_evidence=decision.operator_evidence,
            capability=decision.capability,
            claim=claim,
        )
        assert foreign.eligible is False
        store.mark_dispatching(decision, owner_id="w-a", token="t-a")
        recovery = store.recover(target, observed_at=OUTCOME_AT)
        assert recovery.unresolved_submission_ids == (submission_id,)
    finally:
        store.close()
