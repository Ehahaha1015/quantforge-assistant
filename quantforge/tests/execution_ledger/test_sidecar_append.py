from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _support import SUBMISSION_ID, TARGET_JSON, make_observation

from quantforge.execution_ledger import (
    BrokerObservationStatus,
    ExecutionLedgerBusyError,
)
from quantforge.execution_ledger.identity import derive_attempt_id
from quantforge.execution_ledger.sidecar import SQLiteObservationStore

_LATER = datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC)
_EARLIER = _LATER - timedelta(minutes=1)
_ATTEMPT_ID = derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 1, 1)


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
                "SELECT observation_sequence, observation_id, attempt_id, "
                "observation_kind, status, observed_at, recorded_at, "
                "payload_json FROM observations ORDER BY observation_sequence"
            ).fetchall()
        ]
    finally:
        connection.close()


def test_append_assigns_sequence_and_identity(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    try:
        stored = store.append(make_observation())
    finally:
        store.close()
    assert stored.observation_id == "execstate-observation-v1-1"
    rows = _rows(database)
    assert len(rows) == 1
    sequence, observation_id, attempt_id, kind, status = rows[0][:5]
    assert sequence == 1
    assert observation_id == "execstate-observation-v1-1"
    assert attempt_id.startswith("execstate-attempt-v1-")
    assert kind == "BROKER_OBSERVATION"
    assert status == "ACCEPTED"
    assert rows[0][5] == rows[0][6] == "2030-01-01T12:02:01.000000Z"
    assert SUBMISSION_ID in str(rows[0][7])
    assert TARGET_JSON.replace('"', '\\"') in str(rows[0][7])


def test_append_ordering_is_sequence_not_timestamp(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    try:
        first = store.append(make_observation(observed_at=_LATER))
        second = store.append(
            make_observation(
                observed_at=_EARLIER, broker_reference="broker-ref-2"
            )
        )
        observations = store.read_observations(_ATTEMPT_ID)
    finally:
        store.close()
    assert first.observation_id == "execstate-observation-v1-1"
    assert second.observation_id == "execstate-observation-v1-2"
    assert [row[0] for row in _rows(database)] == [1, 2]
    assert observations[0].observed_at == _LATER
    assert observations[1].observed_at == _EARLIER


def test_exact_replay_appends_replay_row(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    try:
        first = store.append(make_observation())
        replay = store.append(first)
    finally:
        store.close()
    assert first.observation_id == "execstate-observation-v1-1"
    assert replay.observation_id == "execstate-observation-v1-2"
    rows = _rows(database)
    assert [row[3] for row in rows] == [
        "BROKER_OBSERVATION",
        "OBSERVATION_REPLAY",
    ]
    assert rows[0][7] != rows[1][7]
    assert rows[0][2] == rows[1][2]


def test_distinct_observation_is_not_a_replay(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    try:
        store.append(make_observation())
        store.append(make_observation(broker_reference="broker-ref-2"))
    finally:
        store.close()
    assert [row[3] for row in _rows(database)] == [
        "BROKER_OBSERVATION",
        "BROKER_OBSERVATION",
    ]


def test_reopen_reproduces_ordered_history(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    store.append(make_observation())
    store.append(
        make_observation(
            status=BrokerObservationStatus.REJECTED,
            broker_reference="broker-ref-2",
        )
    )
    store.close()
    rows_before = _rows(database)
    reopened = SQLiteObservationStore(database)
    try:
        history = reopened.read_observations(_ATTEMPT_ID)
    finally:
        reopened.close()
    assert [observation.observation_id for observation in history] == [
        "execstate-observation-v1-1",
        "execstate-observation-v1-2",
    ]
    assert rows_before == _rows(database)


def test_append_is_deterministic_across_stores(tmp_path: Path) -> None:
    first = tmp_path / "first" / "ledger.sqlite3"
    second = tmp_path / "second" / "ledger.sqlite3"
    first.parent.mkdir()
    second.parent.mkdir()
    first_store = SQLiteObservationStore(first)
    second_store = SQLiteObservationStore(second)
    try:
        for observed_at, reference in (
            (_EARLIER, "broker-ref-1"),
            (_LATER, "broker-ref-2"),
        ):
            observation = make_observation(
                observed_at=observed_at, broker_reference=reference
            )
            first_store.append(dataclasses.replace(observation))
            second_store.append(dataclasses.replace(observation))
    finally:
        first_store.close()
        second_store.close()
    assert _rows(first) == _rows(second)


def test_busy_append_fails_closed_without_retry(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    store = SQLiteObservationStore(database)
    blocker = _connect(database)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(ExecutionLedgerBusyError):
            store.append(make_observation())
        blocker.execute("ROLLBACK")
    finally:
        blocker.close()
    assert _rows(database) == []
    stored = store.append(make_observation())
    store.close()
    assert stored.observation_id == "execstate-observation-v1-1"


def test_read_unknown_attempt_returns_empty(tmp_path: Path) -> None:
    store = SQLiteObservationStore(tmp_path / "ledger.sqlite3")
    try:
        assert (
            store.read_observations("execstate-attempt-v1-" + "0" * 32)
            == ()
        )
    finally:
        store.close()
