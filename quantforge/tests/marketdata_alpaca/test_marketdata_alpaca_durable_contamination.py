"""End to end canary oracle for the concrete market-data credential path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from _marketdata_alpaca_fixtures import (
    CANARY,
    REQUESTED_AT,
    RecordingTransport,
    make_blob,
    make_body,
    make_boundary,
    make_reference,
    make_request,
    make_response,
    make_scope,
)

from quantforge.credentials.secrets import SecretMaterial
from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.execution_ledger.ledger import DurableExecutionLedger
from quantforge.execution_ledger.sidecar import SQLiteObservationStore
from quantforge.frontier import ExecutionTargetBinding
from quantforge.marketdata_alpaca.composition import _build_source

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
            assert CANARY.encode() not in candidate.read_bytes()
    with sqlite3.connect(str(path)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        for table in tables:
            for row in connection.execute(f"SELECT * FROM {table}"):
                assert CANARY not in repr(tuple(row))


def test_canary_stays_ephemeral_through_observation_and_durable_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = make_body(("AAPL",))
    transport = RecordingTransport(
        response=make_response(body, content_length=str(len(body)))
    )
    boundary = make_boundary(material=make_blob(secret_key=CANARY))
    source = _build_source(boundary=boundary, transport=transport)
    observation = source.observe(make_request(("AAPL",)))

    assert len(transport.calls) == 1
    assert CANARY in transport.calls[0][1]["APCA-API-SECRET-KEY"]
    assert CANARY not in repr(observation)
    assert CANARY not in observation.model_dump_json()
    assert CANARY not in observation.observation_id
    assert CANARY not in observation.source_observation_id

    execstate_path = tmp_path / "execstate.sqlite3"
    ledger_path = tmp_path / "ledger.sqlite3"
    observations_path = tmp_path / "observations.sqlite3"
    execstate = SQLiteExecutionStateStore(execstate_path)
    ledger = DurableExecutionLedger(execstate, ledger_path)
    observation_store = SQLiteObservationStore(observations_path)
    try:
        target = _target()
        execstate.recover(target, observed_at=REQUESTED_AT)
        lease = boundary.acquire(
            make_scope(),
            make_reference(),
            requested_at=REQUESTED_AT,
        )
        material = lease.reveal(evaluation_time=REQUESTED_AT)
        assert material.as_bytes() == make_blob(secret_key=CANARY)

        for value in (material, lease, CANARY.encode(), CANARY):
            with pytest.raises(TypeError):
                observation_store.append(value)
            with pytest.raises(TypeError):
                ledger.record_observation(value)
            with pytest.raises(TypeError):
                ledger.assess_observation(value)
            with pytest.raises(TypeError):
                execstate.prepare(value, value, value, prepared_at=REQUESTED_AT)
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
        assert CANARY not in str(SecretMaterial(make_blob(secret_key=CANARY)))
        lease.discard()
    finally:
        ledger.close()
        observation_store.close()
        execstate.close()

    _scan(execstate_path)
    _scan(ledger_path)
    _scan(observations_path)
    assert {path.name for path in tmp_path.iterdir()} <= EXPECTED_FILES
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert CANARY not in captured.out + captured.err
