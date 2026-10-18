from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from _support import make_observation

from quantforge.execution_ledger.sidecar import SQLiteObservationStore


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), isolation_level=None)


def test_update_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    store.append(make_observation())
    store.close()
    connection = _connect(database)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE observations SET status = 'REJECTED'"
            )
        rows = connection.execute(
            "SELECT status FROM observations"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [("ACCEPTED",)]


def test_delete_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    store.append(make_observation())
    store.close()
    connection = _connect(database)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM observations")
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert count == 1


def test_triggers_survive_reopen(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    store.append(make_observation())
    store.close()
    reopened = SQLiteObservationStore(database)
    reopened.close()
    connection = _connect(database)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE observations SET broker_reference = 'other'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM observations")
    finally:
        connection.close()


def test_store_has_no_repair_or_migration_surface(tmp_path: Path) -> None:
    store = SQLiteObservationStore(tmp_path / "ledger.sqlite3")
    try:
        public = {
            name
            for name in dir(store)
            if not name.startswith("_") and callable(getattr(store, name))
        }
    finally:
        store.close()
    assert public == {"append", "read_observations", "close"}
