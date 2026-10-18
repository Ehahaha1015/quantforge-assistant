from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from _support import make_observation

from quantforge.execution_ledger import ExecutionLedgerSchemaError
from quantforge.execution_ledger.sidecar import (
    SQLiteObservationStore,
    _schema_sql_tokens,
)

_EXPECTED_METADATA = {
    "schema_name": "quantforge.execution-ledger.sqlite",
    "schema_version": "1",
    "binding_execstate_schema_name": "quantforge.execstate.sqlite",
    "binding_execstate_schema_version": "2",
    "binding_execstate_schema_fingerprint": (
        "21925c0fde36219ad76d2e18fbaae74beac8ad5aacf3694dcb34c884833875d1"
    ),
}

_EXPECTED_COLUMNS = [
    "observation_sequence",
    "observation_id",
    "attempt_id",
    "submission_id",
    "target_json",
    "generation",
    "recovery_epoch",
    "observation_kind",
    "status",
    "broker_reference",
    "broker_message",
    "observed_at",
    "payload_json",
    "recorded_at",
]


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _objects(path: Path) -> list[tuple[str, str, str]]:
    connection = _connect(path)
    try:
        return sorted(
            (
                str(row["type"]),
                str(row["name"]),
                str(row["sql"])
                if row["sql"] is not None
                else "",
            )
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master"
            ).fetchall()
        )
    finally:
        connection.close()


def _logical_fingerprint(path: Path) -> str:
    connection = _connect(path)
    try:
        objects = _objects(path)
        metadata = sorted(
            (str(row["key"]), str(row["value"]))
            for row in connection.execute(
                "SELECT key, value FROM metadata ORDER BY key"
            ).fetchall()
        )
        version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        return json.dumps(
            {
                "objects": objects,
                "metadata": metadata,
                "user_version": version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    finally:
        connection.close()


def _rewrite_schema(
    path: Path, *, name: str, old: str, new: str
) -> None:
    connection = _connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        assert row is not None
        sql = str(row["sql"])
        assert old in sql
        version = int(
            connection.execute("PRAGMA schema_version").fetchone()[0]
        )
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE name = ?",
            (sql.replace(old, new, 1), name),
        )
        connection.execute(f"PRAGMA schema_version = {version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
    finally:
        connection.close()


def _drop(path: Path, kind: str, name: str) -> None:
    connection = _connect(path)
    try:
        connection.execute(f"DROP {kind} {name}")
    finally:
        connection.close()


def _set_user_version(path: Path, value: int) -> None:
    connection = _connect(path)
    try:
        connection.execute(f"PRAGMA user_version = {value}")
    finally:
        connection.close()


def _set_metadata(path: Path, key: str, value: str) -> None:
    connection = _connect(path)
    try:
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = ?", (value, key)
        )
    finally:
        connection.close()


def test_fresh_schema_is_exact(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    store.close()
    connection = _connect(database)
    try:
        version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM metadata")
        }
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if str(row["name"]) != "sqlite_sequence"
        }
        indexes = {
            str(row["name"]): str(row["sql"])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND sql IS NOT NULL"
            )
        }
        triggers = {
            str(row["name"]): str(row["sql"])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        columns = [
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(observations)"
            )
        ]
        integrity = str(
            connection.execute("PRAGMA integrity_check").fetchone()[0]
        )
        foreign_keys = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
    finally:
        connection.close()
    assert version == 1
    assert metadata == _EXPECTED_METADATA
    assert tables == {"metadata", "observations"}
    assert set(indexes) == {"observations_attempt_lookup"}
    assert set(triggers) == {
        "observations_no_update",
        "observations_no_delete",
    }
    assert columns == _EXPECTED_COLUMNS
    assert integrity == "ok"
    assert foreign_keys == 0


def test_fresh_schema_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first" / "ledger.sqlite3"
    second = tmp_path / "second" / "ledger.sqlite3"
    first.parent.mkdir()
    second.parent.mkdir()
    SQLiteObservationStore(first).close()
    SQLiteObservationStore(second).close()
    assert _logical_fingerprint(first) == _logical_fingerprint(second)


def test_format_only_ddl_change_is_accepted(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    SQLiteObservationStore(database).close()
    _rewrite_schema(
        database,
        name="metadata",
        old="CREATE TABLE metadata (",
        new="CREATE TABLE metadata(",
    )
    SQLiteObservationStore(database).close()


def test_extra_table_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    SQLiteObservationStore(database).close()
    connection = _connect(database)
    try:
        connection.execute("CREATE TABLE extra (id INTEGER PRIMARY KEY)")
    finally:
        connection.close()
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(database)


def test_missing_index_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    SQLiteObservationStore(database).close()
    _drop(database, "INDEX", "observations_attempt_lookup")
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(database)


def test_missing_trigger_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    SQLiteObservationStore(database).close()
    _drop(database, "TRIGGER", "observations_no_update")
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(database)


def test_weakened_check_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    SQLiteObservationStore(database).close()
    _rewrite_schema(
        database,
        name="observations",
        old="generation >= 1",
        new="generation >= 0",
    )
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(database)


def test_wrong_user_version_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    SQLiteObservationStore(database).close()
    _set_user_version(database, 2)
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(database)


def test_wrong_metadata_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    SQLiteObservationStore(database).close()
    _set_metadata(database, "schema_name", "foreign.sqlite")
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(database)


def test_failed_verification_does_not_repair(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    store.append(make_observation())
    store.close()
    _drop(database, "TRIGGER", "observations_no_delete")
    before = (_objects(database), _logical_fingerprint(database))
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(database)
    after = (_objects(database), _logical_fingerprint(database))
    assert before == after


def test_schema_tokens_ignore_whitespace_but_not_quotes() -> None:
    assert _schema_sql_tokens("CREATE TABLE x (a >= 1)") == _schema_sql_tokens(
        "create   table x (a>=1)"
    )
    assert _schema_sql_tokens("CHECK (a IN ('x'))") != _schema_sql_tokens(
        "CHECK (a IN ('y'))"
    )
