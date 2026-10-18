from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from quantforge.execution_ledger import (
    ExecutionLedgerCorruptionError,
    ExecutionLedgerPathError,
    ExecutionLedgerSchemaError,
)
from quantforge.execution_ledger.sidecar import SQLiteObservationStore


@pytest.mark.parametrize(
    "path",
    [
        "relative/ledger.sqlite3",
        ":memory:",
        "file:///D:/ledger.sqlite3",
        "",
    ],
)
def test_invalid_paths_are_rejected(path: str) -> None:
    with pytest.raises(ExecutionLedgerPathError):
        SQLiteObservationStore(path)


def test_nul_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ExecutionLedgerPathError):
        SQLiteObservationStore(str(tmp_path / "a\x00b.sqlite3"))


def test_too_long_path_is_rejected() -> None:
    with pytest.raises(ExecutionLedgerPathError):
        SQLiteObservationStore("D:\\" + "x" * 4100 + ".sqlite3")


def test_missing_parent_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ExecutionLedgerPathError):
        SQLiteObservationStore(tmp_path / "missing" / "ledger.sqlite3")


def test_reserved_device_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ExecutionLedgerPathError):
        SQLiteObservationStore(tmp_path / "CON.sqlite3")


def test_directory_path_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "ledger.sqlite3"
    directory.mkdir()
    with pytest.raises(ExecutionLedgerPathError):
        SQLiteObservationStore(directory)


def test_unc_path_is_rejected() -> None:
    with pytest.raises(ExecutionLedgerPathError):
        SQLiteObservationStore(r"\\server\share\ledger.sqlite3")


def test_zero_byte_file_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    database.write_bytes(b"")
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(database)


def test_existing_non_database_file_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    database.write_bytes(b"not a database")
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(database)


def test_valid_absolute_path_is_accepted(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    store.close()
    assert database.is_file()


def test_final_junction_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "junction-target"
    target.mkdir()
    link = tmp_path / "junction-link"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("directory junction creation is unavailable")
    try:
        with pytest.raises(ExecutionLedgerPathError):
            SQLiteObservationStore(link)
    finally:
        subprocess.run(
            ["cmd", "/c", "rmdir", str(link)],
            check=False,
            capture_output=True,
        )


def test_final_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "symlink-target.sqlite3"
    SQLiteObservationStore(target).close()
    link = tmp_path / "symlink-link.sqlite3"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("file symlink creation requires unavailable privilege")
    with pytest.raises(ExecutionLedgerPathError):
        SQLiteObservationStore(link)
