"""C2 durable-contamination oracle for the concrete credential path.

The synthetic credential path is exercised end to end, refusal-inducing
writes are attempted against the current public durable APIs, each typed
refusal is asserted, and only then are the durable bytes and sidecars
scanned for the synthetic canary.  Opaque free-text parameters are never
used as attempt targets, exactly as the frozen contract requires.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from _credentials_windows_fixtures import (
    CANARY,
    CANARY_BYTES,
    REQUESTED_AT,
    RecordingSeam,
    make_record,
    make_reference,
    make_scope,
)

from quantforge.credentials import CredentialBoundary
from quantforge.credentials.secrets import CredentialLease, SecretMaterial
from quantforge.credentials_windows.backend import (
    WindowsCredentialManagerBackend,
)
from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.execution_ledger.ledger import DurableExecutionLedger
from quantforge.execution_ledger.sidecar import SQLiteObservationStore
from quantforge.frontier import ExecutionTargetBinding

SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")
EXPECTED_FILES = {
    "execstate.sqlite3",
    "execstate.sqlite3-wal",
    "execstate.sqlite3-shm",
    "ledger.sqlite3",
    "ledger.sqlite3-wal",
    "ledger.sqlite3-shm",
    "observations.sqlite3",
    "observations.sqlite3-wal",
    "observations.sqlite3-shm",
}


def _target() -> ExecutionTargetBinding:
    return ExecutionTargetBinding(
        schema="frontier-target-v1",
        broker="broker-main",
        environment="PAPER",
        account_reference="acct-001",
        adapter_id="adapter-a",
        adapter_version="v1",
        submission_id="broker1-" + "0" * 32,
    )


def _scan(path: Path) -> None:
    for suffix in SIDECAR_SUFFIXES:
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            assert CANARY_BYTES not in candidate.read_bytes()
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


def _lease() -> CredentialLease:
    scope = make_scope()
    reference = make_reference()
    seam = RecordingSeam(make_record(scope, reference, blob=CANARY_BYTES))
    backend = WindowsCredentialManagerBackend(seam=seam)
    return CredentialBoundary(backend=backend).acquire(
        scope, reference, requested_at=REQUESTED_AT
    )


def test_oracle_durable_boundaries_stay_canary_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    execstate_path = tmp_path / "execstate.sqlite3"
    ledger_path = tmp_path / "ledger.sqlite3"
    observations_path = tmp_path / "observations.sqlite3"
    execstate = SQLiteExecutionStateStore(execstate_path)
    ledger = DurableExecutionLedger(execstate, ledger_path)
    observation_store = SQLiteObservationStore(observations_path)
    try:
        target = _target()
        execstate.recover(target, observed_at=REQUESTED_AT)

        lease = _lease()
        material = lease.reveal(evaluation_time=REQUESTED_AT)
        assert material.as_bytes() == CANARY_BYTES

        # Object-typed positions reject every credential-derived value.
        for value in (material, lease, CANARY_BYTES, CANARY):
            with pytest.raises(TypeError):
                observation_store.append(value)
            with pytest.raises(TypeError):
                ledger.record_observation(value)
            with pytest.raises(TypeError):
                ledger.assess_observation(value)
            with pytest.raises(TypeError):
                execstate.prepare(
                    value, value, value, prepared_at=REQUESTED_AT
                )
            with pytest.raises(TypeError):
                execstate.mark_dispatching(
                    value, owner_id="owner-1", token="token-1"
                )
            with pytest.raises(TypeError):
                execstate.persist_transport_outcome(
                    value,
                    target,
                    owner_id="owner-1",
                    token="token-1",
                    observed_at=REQUESTED_AT,
                )
            with pytest.raises(TypeError):
                execstate.persist_pre_dispatch_no_effect(
                    "submission-1",
                    target,
                    value,
                    owner_id="owner-1",
                    token="token-1",
                    observed_at=REQUESTED_AT,
                )
            with pytest.raises(TypeError):
                execstate.apply_reconciliation(
                    "submission-1",
                    value,
                    status="ACCEPTED",
                    evidence_reference="evidence-1",
                    observed_at=REQUESTED_AT,
                )

        assert CANARY not in lease.facts.model_dump_json()
        assert CANARY not in repr(lease)
        assert CANARY not in repr(material)
        assert CANARY not in str(SecretMaterial(CANARY_BYTES))
    finally:
        ledger.close()
        observation_store.close()
        execstate.close()

    _scan(execstate_path)
    _scan(ledger_path)
    _scan(observations_path)

    created = {path.name for path in tmp_path.iterdir()}
    assert created <= EXPECTED_FILES

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert CANARY not in captured.out


def test_credentials_windows_package_never_imports_durable_subsystems() -> None:
    import quantforge.credentials_windows as package

    package_dir = Path(package.__file__).parent
    source = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(package_dir.glob("*.py"))
    )
    assert "quantforge.execstate" not in source
    assert "quantforge.execution_ledger" not in source
    assert "quantforge.provenance" not in source
