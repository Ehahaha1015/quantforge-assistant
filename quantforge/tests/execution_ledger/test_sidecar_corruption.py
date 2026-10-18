from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from _support import SUBMISSION_ID, TARGET_JSON, make_observation

from quantforge.execution_ledger import ExecutionLedgerCorruptionError
from quantforge.execution_ledger.identity import derive_attempt_id
from quantforge.execution_ledger.sidecar import SQLiteObservationStore

_TRIGGER_UPDATE = (
    "CREATE TRIGGER observations_no_update BEFORE UPDATE ON observations "
    "BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END"
)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(path: Path) -> list[tuple[object, ...]]:
    connection = _connect(path)
    try:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM observations ORDER BY observation_sequence"
            ).fetchall()
        ]
    finally:
        connection.close()


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


def _tamper(path: Path, sql: str, params: tuple[object, ...]) -> None:
    connection = _connect(path)
    try:
        connection.execute("DROP TRIGGER observations_no_update")
        connection.execute(sql, params)
        connection.execute(_TRIGGER_UPDATE)
    finally:
        connection.close()


def _prepared(tmp_path: Path) -> Path:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    store.append(make_observation())
    store.close()
    return database


def test_malformed_payload_is_detected(tmp_path: Path) -> None:
    database = _prepared(tmp_path)
    _tamper(database, "UPDATE observations SET payload_json = ?", ("{",))
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)


def test_non_canonical_payload_is_detected(tmp_path: Path) -> None:
    database = _prepared(tmp_path)
    parsed = json.loads(str(_rows(database)[0][12]))
    rewritten = json.dumps(parsed, indent=2, sort_keys=True)
    _tamper(
        database,
        "UPDATE observations SET payload_json = ?",
        (rewritten,),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)


def test_extracted_column_mismatch_is_detected(tmp_path: Path) -> None:
    database = _prepared(tmp_path)
    _tamper(
        database,
        "UPDATE observations SET status = 'REJECTED'",
        (),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)


def test_observation_id_mismatch_is_detected(tmp_path: Path) -> None:
    database = _prepared(tmp_path)
    _tamper(
        database,
        "UPDATE observations SET observation_id = ?",
        ("execstate-observation-v1-99",),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)


def test_attempt_id_mismatch_is_detected(tmp_path: Path) -> None:
    database = _prepared(tmp_path)
    _tamper(
        database,
        "UPDATE observations SET attempt_id = ?",
        (derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 2, 1),),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)


def test_non_canonical_observed_at_is_detected(tmp_path: Path) -> None:
    database = _prepared(tmp_path)
    _tamper(
        database,
        "UPDATE observations SET observed_at = ?",
        ("2030-01-01T12:02:01Z",),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)


def test_recorded_at_mismatch_is_detected(tmp_path: Path) -> None:
    database = _prepared(tmp_path)
    _tamper(
        database,
        "UPDATE observations SET recorded_at = ?",
        ("2030-01-01T12:02:02.000000Z",),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)


def test_replay_without_original_is_detected(tmp_path: Path) -> None:
    database = _prepared(tmp_path)
    _tamper(
        database,
        "UPDATE observations SET observation_kind = 'OBSERVATION_REPLAY'",
        (),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)


def test_corruption_blocks_append_without_mutation(tmp_path: Path) -> None:
    database = _prepared(tmp_path)
    store = SQLiteObservationStore(database)
    _tamper(
        database,
        "UPDATE observations SET status = 'REJECTED'",
        (),
    )
    before_rows = _rows(database)
    before_objects = _objects(database)
    with pytest.raises(ExecutionLedgerCorruptionError):
        store.append(
            make_observation(broker_reference="broker-ref-2")
        )
    store.close()
    assert _rows(database) == before_rows
    assert _objects(database) == before_objects


def test_broker_reference_column_mismatch_is_detected(
    tmp_path: Path,
) -> None:
    """Reviewer A CH-C: contract 9.5 extracted-column verification."""

    database = _prepared(tmp_path)
    _tamper(
        database,
        "UPDATE observations SET broker_reference = ?",
        ("broker-ref-tampered",),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)
    # A valid, untouched database still reopens successfully.
    clean = tmp_path / "clean.sqlite3"
    store = SQLiteObservationStore(clean)
    store.append(
        make_observation(broker_reference="broker-ref-1")
    )
    store.close()
    SQLiteObservationStore(clean).close()


def test_broker_message_column_mismatch_is_detected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "message.sqlite3"
    store = SQLiteObservationStore(database)
    store.append(make_observation(broker_message="broker-msg-1"))
    store.close()
    _tamper(
        database,
        "UPDATE observations SET broker_message = ?",
        ("broker-msg-tampered",),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)


def test_observation_kind_column_mismatch_is_detected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kind.sqlite3"
    store = SQLiteObservationStore(database)
    store.append(make_observation())
    store.append(make_observation())
    store.close()
    # The second row is a stored replay, so its canonical payload carries
    # OBSERVATION_REPLAY while the tampered extracted column claims a first
    # observation. Only the column/payload comparison can detect this.
    _tamper(
        database,
        "UPDATE observations SET observation_kind = 'BROKER_OBSERVATION' "
        "WHERE observation_sequence = 2",
        (),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)
