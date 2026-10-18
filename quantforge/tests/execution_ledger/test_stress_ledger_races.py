"""Slice 8 bounded stress: ledger observation races and reopen semantics.

Racing workers boot and recover while the submission is still PREPARED, then
the test drives the real EXECSTATE public API through claim and dispatch and
releases the workers onto a live DISPATCHING attempt. The ledger is
evidence-only: no race may mutate EXECSTATE, create dispatch authority,
invent a second attempt, or resolve a contradiction. Every negative outcome
must fail closed with one of the contracted error families, without retry.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from _ledger_support import (
    CLAIM_EXPIRES_AT,
    LATE_AT,
    RECOVER_AT,
    build_eligibility,
    claim_store,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)
from _support import OBSERVED_AT, make_observation

from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.execution_ledger import (
    AttemptPhase,
    ExecutionLedgerCorruptionError,
    ReconciliationConflict,
)
from quantforge.execution_ledger.ledger import DurableExecutionLedger
from quantforge.execution_ledger.models import BrokerObservationStatus
from quantforge.execution_ledger.sidecar import SQLiteObservationStore

_WORKER = Path(__file__).with_name("_stress_observation_worker.py")
_READER_WORKER = Path(__file__).with_name("_oracle_reader_worker.py")

_COMPATIBLE_ROUNDS = 3
_COMPATIBLE_WORKERS = 3
_CONTRADICTORY_ROUNDS = 3

# Recovery instants stay inside the claim window so the durable state is
# still DISPATCHING while the observation race runs.
_WORKER_RECOVER_AT = datetime(2030, 1, 1, 12, 1, 56, tzinfo=UTC)
_PARENT_RECOVER_AT = datetime(2030, 1, 1, 12, 1, 57, tzinfo=UTC)
_PARENT_CLAIM_AT = datetime(2030, 1, 1, 12, 1, 58, tzinfo=UTC)
_PARENT_RESCAN_AT = datetime(2030, 1, 1, 12, 1, 59, tzinfo=UTC)
_REOPEN_AT = LATE_AT
_READER_AT = LATE_AT + timedelta(hours=1)

_RECORDED_OUTCOMES = {
    "RECORDED",
    "ReconciliationConflict",
    "ExecutionLedgerCorruptionError",
    "ExecutionLedgerBusyError",
}

_EXECSTATE_TABLES = (
    "metadata",
    "submissions",
    "commit_receipts",
    "claims",
    "operational_payloads",
    "recovery_runs",
    "audit_events",
)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def _execstate_snapshot(database: Path) -> dict[str, object]:
    """Durable EXECSTATE content, independent of WAL file layout."""
    connection = _connect(database)
    try:
        snapshot: dict[str, object] = {}
        for table in _EXECSTATE_TABLES:
            snapshot[table] = [
                tuple(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} "
                    "ORDER BY rowid"
                )
            ]
        snapshot["sqlite_sequence"] = [
            tuple(row)
            for row in connection.execute(
                "SELECT name, seq FROM sqlite_sequence ORDER BY name"
            )
        ]
        snapshot["user_version"] = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
    finally:
        connection.close()
    return snapshot


def _submission_row(database: Path) -> sqlite3.Row:
    connection = _connect(database)
    try:
        return connection.execute(
            "SELECT state, active_claim_id, generation, recovery_epoch "
            "FROM submissions"
        ).fetchone()
    finally:
        connection.close()


def _sidecar_rows(sidecar: Path) -> list[dict[str, object]]:
    if not sidecar.exists():
        return []
    connection = _connect(sidecar)
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT observation_sequence, observation_id, attempt_id, "
                "observation_kind, status, broker_reference, observed_at, "
                "payload_json FROM observations "
                "ORDER BY observation_sequence"
            )
        ]
    finally:
        connection.close()


def _spawn(
    directory: Path,
    database: Path,
    target,
    sidecar: Path,
    status: str,
    broker_reference: str,
    index: int,
    workers: int,
) -> tuple[subprocess.Popen[str], Path]:
    result_path = directory / f"result-{index}.json"
    process = subprocess.Popen(
        [
            sys.executable,
            str(_WORKER),
            "record",
            str(database),
            target.model_dump_json(),
            str(sidecar),
            _WORKER_RECOVER_AT.isoformat(),
            OBSERVED_AT.isoformat(),
            status,
            broker_reference,
            str(directory / "ready"),
            str(index),
            str(workers),
            str(directory / "go"),
            str(result_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, result_path


def _await_workers_ready(
    directory: Path,
    workers: int,
    processes: list[subprocess.Popen[str]],
) -> None:
    ready_root = directory / "ready"
    deadline = datetime.now(UTC) + timedelta(seconds=60)
    while True:
        if len(list(ready_root.glob("ready-*"))) >= workers:
            return
        if datetime.now(UTC) > deadline:
            for process in processes:
                process.kill()
                process.wait(timeout=30)
            raise AssertionError("workers did not reach the race barrier")
        time.sleep(0.01)


def _prepare(directory: Path):
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    store = make_store(directory / "state")
    try:
        recover_store(store, target, observed_at=RECOVER_AT)
        record, receipt = prepare_store(store, provenance, target)
    finally:
        store.close()
    return provenance, target, record, receipt


def _dispatch(directory: Path, provenance, target, record, receipt):
    """Drive claim and dispatch through the real EXECSTATE public API."""
    parent = make_store(directory / "state")
    try:
        recover_store(parent, target, observed_at=_PARENT_RECOVER_AT)
        claim = claim_store(
            parent,
            target.submission_id,
            target,
            acquired_at=_PARENT_CLAIM_AT,
            expires_at=CLAIM_EXPIRES_AT,
        )
        cover = recover_store(
            parent, target, observed_at=_PARENT_RESCAN_AT
        )
        current_record, current_receipt = parent.read(
            target.submission_id, target
        )
        decision = build_eligibility(
            provenance,
            target,
            current_record,
            current_receipt,
            cover,
            claim,
        )
        parent.mark_dispatching(
            decision, owner_id="worker-1", token="token-1"
        )
        return claim
    finally:
        parent.close()


def _run_race(
    directory: Path, statuses: list[str], references: list[str]
) -> tuple[object, Path, Path, dict, dict, list, list, dict]:
    directory.mkdir(parents=True, exist_ok=True)
    provenance, target, record, receipt = _prepare(directory)
    database = directory / "state" / "execstate.sqlite3"
    sidecar = directory / "ledger.sqlite3"
    processes = [
        _spawn(
            directory,
            database,
            target,
            sidecar,
            status,
            reference,
            index,
            len(statuses),
        )
        for index, (status, reference) in enumerate(
            zip(statuses, references, strict=True)
        )
    ]
    _await_workers_ready(directory, len(statuses), processes)
    claim = _dispatch(directory, provenance, target, record, receipt)
    # The evidence store is created once by the first participant; this
    # slice stresses append races, not schema-creation races (Oracle J
    # covers the concurrent-creation and path matrix).
    with SQLiteObservationStore(sidecar):
        pass
    before = _execstate_snapshot(database)
    (directory / "go").write_text("go", encoding="utf-8")
    results = []
    for process, result_path in processes:
        _output, error = process.communicate(timeout=120)
        assert process.returncode == 0, error
        results.append(
            json.loads(result_path.read_text(encoding="utf-8"))
        )
    after = _execstate_snapshot(database)
    return (
        target,
        database,
        sidecar,
        before,
        after,
        _sidecar_rows(sidecar),
        results,
        claim,
    )


def _repeat_losing_observation(
    database: Path,
    sidecar: Path,
    target,
    status: str,
    broker_reference: str,
    generation: int,
    recovery_epoch: int,
) -> list[str]:
    """Re-run one losing observation twice; return the error types."""
    opened = SQLiteExecutionStateStore(database)
    ledger = None
    failures: list[str] = []
    try:
        recover_store(opened, target, observed_at=_READER_AT)
        ledger = DurableExecutionLedger(opened, sidecar)
        for _ in range(2):
            observation = make_observation(
                submission_id=target.submission_id,
                target=target,
                generation=generation,
                recovery_epoch=recovery_epoch,
                status=BrokerObservationStatus(status),
                broker_reference=broker_reference,
                observed_at=OBSERVED_AT,
            )
            try:
                ledger.record_observation(observation)
            except (
                ExecutionLedgerCorruptionError,
                ReconciliationConflict,
            ) as exc:
                failures.append(type(exc).__name__)
            else:
                failures.append("RECORDED")
    finally:
        if ledger is not None:
            ledger.close()
        opened.close()
    return failures


def _run_reader(
    database: Path, target, sidecar: Path, output: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_READER_WORKER),
            str(database),
            target.model_dump_json(),
            str(sidecar),
            _READER_AT.isoformat(),
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _race_generation_epoch(database: Path) -> tuple[int, int]:
    row = _submission_row(database)
    assert str(row["state"]) == "DISPATCHING"
    return int(row["generation"]), int(row["recovery_epoch"])


def test_stress_compatible_observation_race(tmp_path: Path) -> None:
    classification: Counter[str] = Counter()
    for round_index in range(_COMPATIBLE_ROUNDS):
        directory = tmp_path / f"compatible-{round_index}"
        statuses = ["ACCEPTED"] * _COMPATIBLE_WORKERS
        references = [
            f"broker-ref-{letter}"
            for letter in ("a", "b", "c")[:_COMPATIBLE_WORKERS]
        ]
        (
            target,
            database,
            sidecar,
            before,
            after,
            rows,
            results,
            claim,
        ) = _run_race(directory, statuses, references)
        classification.update(item["result"] for item in results)
        assert set(classification) <= _RECORDED_OUTCOMES
        # The ledger never writes EXECSTATE: every durable table, sequence,
        # and schema version is identical across the whole observation race.
        assert after == before
        row = _submission_row(database)
        assert str(row["state"]) == "DISPATCHING"
        assert row["active_claim_id"] == claim.claim_id
        assert {
            item["result"] for item in results
        } <= {"RECORDED", "ExecutionLedgerBusyError"}
        successes = [
            item for item in results if item["result"] == "RECORDED"
        ]
        assert successes
        # No hidden retries: each committed append belongs to exactly one
        # reported success, and sequence numbers are contiguous.
        assert len(rows) == len(successes)
        assert [row["observation_sequence"] for row in rows] == list(
            range(1, len(rows) + 1)
        )
        committed = [str(row["observation_id"]) for row in rows]
        assert len(set(committed)) == len(committed)
        assert sorted(committed) == sorted(
            str(item["observation_id"]) for item in successes
        )
        assert {row["observation_kind"] for row in rows} == {
            "BROKER_OBSERVATION"
        }
        assert {row["status"] for row in rows} == {"ACCEPTED"}
        assert len({item["attempt_id"] for item in successes}) == 1
        assert {str(row["attempt_id"]) for row in rows} == {
            str(successes[0]["attempt_id"])
        }

        reopened = SQLiteExecutionStateStore(database)
        ledger = None
        try:
            recover_store(reopened, target, observed_at=_REOPEN_AT)
            ledger = DurableExecutionLedger(reopened, sidecar)
            history = ledger.read_attempt_history(
                target.submission_id, target
            )
            current = ledger.read_attempt(target.submission_id, target)
            recorded = [
                view for view in history if view.observation_ids
            ]
            assert len(recorded) == 1
            view = recorded[0]
            assert str(view.attempt_id) == str(successes[0]["attempt_id"])
            assert list(view.observation_ids) == committed
            assert view.observation_count == len(committed)
            assert view.execstate_state == "DISPATCHING"
            assert view.phase is AttemptPhase.OBSERVED
            assert current.phase is AttemptPhase.UNKNOWN
            assert current.execstate_state == "OUTCOME_UNKNOWN"
            assert current.observation_ids == ()
        finally:
            if ledger is not None:
                ledger.close()
            reopened.close()

        if round_index == _COMPATIBLE_ROUNDS - 1:
            output = directory / "reader.json"
            reader = _run_reader(database, target, sidecar, output)
            assert reader.returncode == 0, reader.stderr
            payload = json.loads(output.read_text(encoding="utf-8"))
            assert payload["current"]["phase"] == "UNKNOWN"
    print(
        json.dumps(
            {"compatible": dict(sorted(classification.items()))},
            sort_keys=True,
        )
    )


def test_stress_contradictory_observation_race(tmp_path: Path) -> None:
    classification: Counter[str] = Counter()
    for round_index in range(_CONTRADICTORY_ROUNDS):
        directory = tmp_path / f"contradictory-{round_index}"
        statuses = ["ACCEPTED", "REJECTED"]
        references = ["broker-ref-a", "broker-ref-b"]
        (
            target,
            database,
            sidecar,
            before,
            after,
            rows,
            results,
            claim,
        ) = _run_race(directory, statuses, references)
        classification.update(item["result"] for item in results)
        assert set(classification) <= _RECORDED_OUTCOMES
        assert after == before
        row = _submission_row(database)
        assert str(row["state"]) == "DISPATCHING"
        assert row["active_claim_id"] == claim.claim_id
        generation, recovery_epoch = _race_generation_epoch(database)
        successes = [
            item for item in results if item["result"] == "RECORDED"
        ]
        # Reviewer A CH-A repair: the authorizing assessment and the append
        # share one writer transaction, so exactly one opposing observation
        # can ever be committed, no matter how the processes interleave.
        assert len(rows) == 1
        assert len(successes) == 1
        assert [row["observation_sequence"] for row in rows] == list(
            range(1, len(rows) + 1)
        )
        assert {row["status"] for row in rows} in (
            {"ACCEPTED"},
            {"REJECTED"},
        )
        assert str(rows[0]["observation_id"]) == str(
            successes[0]["observation_id"]
        )
        assert {row["status"] for row in rows} <= {
            "ACCEPTED",
            "REJECTED",
        }
        for item in results:
            if item["result"] in {
                "ReconciliationConflict",
                "ExecutionLedgerBusyError",
            }:
                assert item["observation_id"] is None
        opened = SQLiteExecutionStateStore(database)
        ledger = None
        try:
            recover_store(opened, target, observed_at=_REOPEN_AT)
            ledger = DurableExecutionLedger(opened, sidecar)
            history = ledger.read_attempt_history(
                target.submission_id, target
            )
            current = ledger.read_attempt(target.submission_id, target)
            recorded = [
                view for view in history if view.observation_ids
            ]
            assert len(recorded) == 1
            assert recorded[0].phase is AttemptPhase.OBSERVED
            assert current.phase is AttemptPhase.UNKNOWN
            assert current.execstate_state == "OUTCOME_UNKNOWN"
        finally:
            if ledger is not None:
                ledger.close()
            opened.close()

        # A losing observation must keep failing closed, identically, and
        # must never append evidence on the retry.
        rows_before_retry = _sidecar_rows(sidecar)
        for index, item in enumerate(results):
            if item["result"] == "RECORDED":
                continue
            failures = _repeat_losing_observation(
                database,
                sidecar,
                target,
                statuses[index],
                references[index],
                generation,
                recovery_epoch,
            )
            assert failures == [
                "ReconciliationConflict",
                "ReconciliationConflict",
            ]
        assert _sidecar_rows(sidecar) == rows_before_retry

        output = directory / "reader.json"
        reader = _run_reader(database, target, sidecar, output)
        assert reader.returncode == 0, reader.stderr
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["current"]["phase"] == "UNKNOWN"
    print(
        json.dumps(
            {"contradictory": dict(sorted(classification.items()))},
            sort_keys=True,
        )
    )
