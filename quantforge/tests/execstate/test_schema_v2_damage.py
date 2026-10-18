"""Schema-v2 damage matrix: semantic damage fails closed, formatting does not."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from _helpers import make_store

from quantforge.execstate import ExecutionStateSchemaError
from quantforge.execstate import store as store_module

_V1_AUDIT_EVENTS = """
CREATE TABLE audit_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL,
    target_json TEXT NOT NULL,
    prior_state TEXT,
    new_state TEXT NOT NULL CHECK (
        new_state IN (
            'PREPARED', 'DISPATCHING', 'ACKNOWLEDGED',
            'REJECTED', 'OUTCOME_UNKNOWN', 'NO_EFFECT_CONFIRMED'
        )
    ),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    recovery_epoch INTEGER NOT NULL CHECK (recovery_epoch >= 1),
    event_kind TEXT NOT NULL CHECK (
        event_kind IN (
            'PREPARED', 'CLAIM_ACQUIRED', 'CLAIM_CONFLICT',
            'CLAIM_REVOKED', 'DISPATCHING', 'TRANSPORT_OUTCOME',
            'PRE_DISPATCH_NO_EFFECT', 'RECONCILIATION',
            'RECOVERY_EPOCH_REBIND', 'RECOVERY_UNKNOWN_CONVERSION'
        )
    ),
    reason TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    owner_id TEXT,
    token_reference TEXT,
    claim_id TEXT,
    payload_sequence INTEGER,
    status TEXT,
    event_json TEXT NOT NULL,
    FOREIGN KEY (submission_id, target_json)
        REFERENCES submissions(submission_id, target_json)
)
"""


def _v1_schema_statements() -> list[str]:
    statements: list[str] = []
    for statement in store_module._SCHEMA_STATEMENTS:
        normalized = statement.strip().upper()
        if normalized.startswith("CREATE TABLE AUDIT_EVENTS"):
            statements.append(_V1_AUDIT_EVENTS)
        elif (
            "audit_events_boundary_run_phase" in statement
            or "recovery_runs_require_cut" in statement
        ):
            continue
        elif "'schema_version', '2'" in statement:
            statements.append(
                statement.replace("'schema_version', '2'", "'schema_version', '1'")
            )
        else:
            statements.append(statement)
    return statements


def _schema_fingerprint(database: Path) -> list[tuple[str, ...]]:
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        objects = [
            (str(row["type"]), str(row["name"]), str(row["sql"]))
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
        ]
        rows: list[tuple[str, ...]] = []
        for row in objects:
            if row[0] != "table":
                continue
            count = connection.execute(f"SELECT COUNT(*) FROM {row[1]}").fetchone()[0]
            rows.append((row[1], str(count)))
        return objects + rows
    finally:
        connection.close()


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _rewrite_table_sql(
    database: Path, *, table: str, old: str, new: str
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
        rewritten = sql.replace(old, new, 1)
        version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql = ? "
            "WHERE type = 'table' AND name = ?",
            (rewritten, table),
        )
        connection.execute(f"PRAGMA schema_version = {version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
    finally:
        connection.close()


def _drop_object(database: Path, *, kind: str, name: str) -> None:
    connection = _connect(database)
    try:
        connection.execute(f"DROP {kind} {name}")
    finally:
        connection.close()


def _set_user_version(database: Path, value: int) -> None:
    connection = _connect(database)
    try:
        connection.execute(f"PRAGMA user_version = {value}")
    finally:
        connection.close()


def _set_metadata(database: Path, *, key: str, value: str) -> None:
    connection = _connect(database)
    try:
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = ?", (value, key)
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        (
            "weakened_row_scope_check",
            lambda database: _rewrite_table_sql(
                database,
                table="audit_events",
                old="row_scope IN ('SUBMISSION', 'RECOVERY_BOUNDARY')",
                new="row_scope IN ('SUBMISSION')",
            ),
        ),
        (
            "weakened_boundary_phase_check",
            lambda database: _rewrite_table_sql(
                database,
                table="audit_events",
                old="boundary_phase IN ('BEGIN', 'END')",
                new="boundary_phase IS NOT NULL",
            ),
        ),
        (
            "weakened_audit_vocabulary",
            lambda database: _rewrite_table_sql(
                database,
                table="audit_events",
                old="'RECOVERY_CUT_BEGIN', 'RECOVERY_CUT_END'",
                new="'RECOVERY_CUT_BEGIN'",
            ),
        ),
        (
            "changed_boundary_fk",
            lambda database: _rewrite_table_sql(
                database,
                table="audit_events",
                old=(
                    "REFERENCES recovery_runs(run_sequence) "
                    "DEFERRABLE INITIALLY DEFERRED"
                ),
                new="REFERENCES recovery_runs(run_sequence)",
            ),
        ),
        (
            "missing_boundary_partial_index",
            lambda database: _drop_object(
                database, kind="INDEX", name="audit_events_boundary_run_phase"
            ),
        ),
        (
            "missing_cut_trigger",
            lambda database: _drop_object(
                database, kind="TRIGGER", name="recovery_runs_require_cut"
            ),
        ),
        (
            "missing_audit_delete_protection",
            lambda database: _drop_object(
                database, kind="TRIGGER", name="audit_events_no_delete"
            ),
        ),
        (
            "missing_boundary_update_protection",
            lambda database: _drop_object(
                database, kind="TRIGGER", name="recovery_runs_no_update"
            ),
        ),
        (
            "wrong_user_version",
            lambda database: _set_user_version(database, 1),
        ),
        (
            "wrong_schema_version_metadata",
            lambda database: _set_metadata(
                database, key="schema_version", value="1"
            ),
        ),
        (
            "wrong_schema_name_metadata",
            lambda database: _set_metadata(
                database, key="schema_name", value="foreign.sqlite"
            ),
        ),
    ],
)
def test_schema_v2_damage_fails_closed(tmp_path, case: str, mutate) -> None:
    store = make_store(tmp_path)
    store.close()
    database = tmp_path / "execstate.sqlite3"
    mutate(database)
    with pytest.raises(ExecutionStateSchemaError):
        make_store(tmp_path)
    assert case


def test_schema_v2_harmless_formatting_is_accepted(tmp_path) -> None:
    store = make_store(tmp_path)
    store.close()
    database = tmp_path / "execstate.sqlite3"
    _rewrite_table_sql(
        database,
        table="audit_events",
        old="CREATE TABLE audit_events (",
        new="CREATE TABLE audit_events(",
    )
    reopened = make_store(tmp_path)
    reopened.close()


def test_valid_schema_v1_database_fails_closed_without_mutation(tmp_path) -> None:
    database = tmp_path / "execstate.sqlite3"
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        for statement in _v1_schema_statements():
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()
    before = _schema_fingerprint(database)
    with pytest.raises(ExecutionStateSchemaError):
        make_store(tmp_path)
    after = _schema_fingerprint(database)
    assert after == before
    connection = sqlite3.connect(str(database))
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        marker = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert marker == "1"
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()
        }
        assert "row_scope" not in columns
    finally:
        connection.close()
