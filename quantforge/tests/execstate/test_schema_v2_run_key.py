"""Run-key allocation oracles for the schema-v2 recovery cut."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from _helpers import (
    OUTCOME_AT,
    PREPARE_AT,
    RECOVER_AT,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)

from quantforge.execstate import (
    ExecutionStateBusyError,
    SQLiteExecutionStateStore,
)

_WORKER = Path(__file__).resolve().parent / "_v2_recover_worker.py"
_LATE = OUTCOME_AT


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _run_keys(database: Path) -> list[int]:
    connection = _connect(database)
    try:
        return [
            int(row[0])
            for row in connection.execute(
                "SELECT run_sequence FROM recovery_runs ORDER BY run_sequence"
            ).fetchall()
        ]
    finally:
        connection.close()


def _sqlite_sequence(database: Path) -> int:
    connection = _connect(database)
    try:
        row = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'recovery_runs'"
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        connection.close()


def test_empty_database_first_run_is_one_and_second_is_two(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    try:
        first = recover_store(store, target, observed_at=RECOVER_AT)
        second = recover_store(store, target, observed_at=PREPARE_AT)
        assert first.evidence_id == "execstate-recovery-v1-1"
        assert second.evidence_id == "execstate-recovery-v1-2"
    finally:
        store.close()
    database = tmp_path / "execstate.sqlite3"
    assert _run_keys(database) == [1, 2]
    assert _sqlite_sequence(database) == 2


def test_restart_continues_from_max_committed_plus_one(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    try:
        for offset in range(4):
            recover_store(
                store, target, observed_at=RECOVER_AT + timedelta(seconds=offset)
            )
    finally:
        store.close()
    database = tmp_path / "execstate.sqlite3"
    assert _run_keys(database) == [1, 2, 3, 4]
    reopened = SQLiteExecutionStateStore(database)
    try:
        evidence = reopened.recover(
            target, observed_at=RECOVER_AT + timedelta(seconds=10)
        )
        assert evidence.evidence_id == "execstate-recovery-v1-5"
    finally:
        reopened.close()
    assert _run_keys(database) == [1, 2, 3, 4, 5]
    assert _sqlite_sequence(database) == 5


def test_rollback_reuses_the_never_committed_run_key(tmp_path, monkeypatch) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    try:
        recover_store(store, target, observed_at=RECOVER_AT)
        prepare_store(store, provenance, target, prepared_at=PREPARE_AT)
        recover_store(store, target, observed_at=PREPARE_AT)
        database = tmp_path / "execstate.sqlite3"
        before = _run_keys(database)
        assert before == [1, 2]

        def _fail(self, begin_sequence: int) -> tuple[int, tuple[str, ...]]:
            raise ExecutionStateBusyError("simulated contention")

        monkeypatch.setattr(
            SQLiteExecutionStateStore, "_current_run_cut_values", _fail
        )
        with pytest.raises(ExecutionStateBusyError):
            store.recover(target, observed_at=_LATE)
        monkeypatch.undo()
        assert _run_keys(database) == before
        connection = _connect(database)
        try:
            leaked = int(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_events "
                    "WHERE boundary_run_sequence = 3"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        assert leaked == 0
        evidence = store.recover(target, observed_at=_LATE)
        assert evidence.evidence_id == "execstate-recovery-v1-3"
    finally:
        store.close()
    assert _run_keys(tmp_path / "execstate.sqlite3") == [1, 2, 3]


def test_two_processes_allocate_distinct_committed_run_keys(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    try:
        recover_store(store, target, observed_at=RECOVER_AT)
    finally:
        store.close()
    database = tmp_path / "execstate.sqlite3"
    moment = _LATE.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    result_paths = [tmp_path / f"result-{index}.json" for index in range(2)]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(_WORKER),
                str(database),
                target.model_dump_json(),
                moment,
                str(result_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for result_path in result_paths
    ]
    for process in processes:
        process.wait(timeout=60)
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in result_paths]
    successes = [payload for payload in payloads if payload["status"] == "ok"]
    assert successes
    evidence_ids = {payload["evidence_id"] for payload in successes}
    assert len(evidence_ids) == len(successes)
    keys = _run_keys(database)
    assert keys == list(range(1, len(keys) + 1))
    assert _sqlite_sequence(database) == keys[-1]
    for payload in payloads:
        if payload["status"] != "ok":
            assert payload["status"] in {
                "ExecutionStateBusyError",
                "ExecutionStateDeniedError",
            }
