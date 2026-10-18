"""Oracle AA - durable boundaries stay canary-free."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from _credential_fixtures import (
    CANARY,
    CANARY_BYTES,
    REQUESTED_AT,
    RecordingBackend,
    make_reference,
    make_scope,
)

import quantforge.credentials as credentials_package
from quantforge.credentials import CredentialBoundary, CredentialProviderError
from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.execution_ledger.sidecar import SQLiteObservationStore

SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _scan_database(path: Path) -> None:
    for suffix in SIDECAR_SUFFIXES:
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            assert CANARY.encode("utf-8") not in candidate.read_bytes()
    connection = sqlite3.connect(str(path))
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        for table in tables:
            for row in connection.execute(f"SELECT * FROM {table}"):
                assert CANARY not in repr(tuple(row))
    finally:
        connection.close()


def test_oracle_aa_durable_boundaries_stay_canary_free(
    tmp_path: Path,
) -> None:
    execstate_path = tmp_path / "execstate.sqlite3"
    ledger_path = tmp_path / "ledger.sqlite3"
    SQLiteExecutionStateStore(execstate_path).close()
    SQLiteObservationStore(ledger_path).close()

    backend = RecordingBackend()
    lease = CredentialBoundary(backend=backend).acquire(
        make_scope(), make_reference(), requested_at=REQUESTED_AT
    )
    assert lease.reveal(evaluation_time=REQUESTED_AT).as_bytes() == CANARY_BYTES

    failing = RecordingBackend(
        error=RuntimeError("failure carrying " + CANARY)
    )
    with pytest.raises(CredentialProviderError) as info:
        CredentialBoundary(backend=failing).acquire(
            make_scope(), make_reference(), requested_at=REQUESTED_AT
        )
    assert CANARY not in repr(info.value)

    _scan_database(execstate_path)
    _scan_database(ledger_path)


def test_credentials_package_never_imports_durable_subsystems() -> None:
    package_dir = Path(credentials_package.__file__).parent
    source = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(package_dir.glob("*.py"))
    )
    assert "quantforge.execstate" not in source
    assert "quantforge.execution_ledger" not in source
    assert "quantforge.broker" not in source
    assert "quantforge.provenance" not in source
