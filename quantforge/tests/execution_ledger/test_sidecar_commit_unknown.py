"""Uncertain commit outcomes must fail closed, never retry, never repair.

EXECSTATE-002 section 9.3 forbids implicit retry and section 13 requires a
sidecar row to be either fully committed or fully absent. When the COMMIT
outcome cannot be established, the append must raise
`ExecutionLedgerCommitUnknownError`, poison the store for every later
operation, leave EXECSTATE untouched, and resolve on reopen to exactly zero
or one complete canonical row.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from _ledger_support import (
    LATE_AT,
    RECOVER_AT,
    advance_to_dispatching,
    make_provenance,
    make_store,
    make_target,
    recover_store,
)
from _support import OBSERVED_AT, make_observation

from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.execution_ledger import (
    ExecutionLedgerCommitUnknownError,
)
from quantforge.execution_ledger.identity import derive_attempt_id
from quantforge.execution_ledger.ledger import DurableExecutionLedger


class _CommitFaultConnection:
    """Delegate to a real connection, but fault on COMMIT.

    Only the commit that follows an INSERT is faulted, so the facade's own
    read transactions keep working and the exercised path is exactly the
    evidence append.

    `fail_before` models a failure raised before the durable commit lands;
    `fail_after` models a landed commit whose success the caller never
    observed. Both are reported to the caller as an unknown commit outcome.
    """

    def __init__(
        self, connection: sqlite3.Connection, *, fail_before: bool
    ) -> None:
        object.__setattr__(self, "_real", connection)
        object.__setattr__(self, "_fail_before", fail_before)
        object.__setattr__(self, "_pending_insert", False)

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        cursor = self._real.execute(sql, *args)
        if sql.lstrip().upper().startswith("INSERT"):
            object.__setattr__(self, "_pending_insert", True)
        return cursor

    def commit(self) -> None:
        if not self._pending_insert:
            self._real.commit()
            return
        object.__setattr__(self, "_pending_insert", False)
        if self._fail_before:
            raise sqlite3.OperationalError("simulated commit failure")
        self._real.commit()
        raise sqlite3.OperationalError("simulated lost commit ack")

    def __getattr__(self, name: str) -> object:
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(object.__getattribute__(self, "_real"), name, value)


def _composed(tmp_path: Path):
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    state_dir = tmp_path / "state"
    store = make_store(state_dir)
    recover_store(store, target, observed_at=RECOVER_AT)
    advance_to_dispatching(store, provenance, target)
    sidecar_path = tmp_path / "ledger.sqlite3"
    ledger = DurableExecutionLedger(store, sidecar_path)
    return (
        store,
        target,
        ledger,
        state_dir / "execstate.sqlite3",
        sidecar_path,
    )


def _execstate_snapshot(database: Path) -> dict[str, object]:
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        return {
            "submissions": [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM submissions ORDER BY rowid"
                )
            ],
            "claims": [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM claims ORDER BY rowid"
                )
            ],
            "audit": [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM audit_events ORDER BY rowid"
                )
            ],
        }
    finally:
        connection.close()


def _sidecar_rows(sidecar: Path) -> list[tuple[object, ...]]:
    if not sidecar.exists():
        return []
    connection = sqlite3.connect(str(sidecar))
    try:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT observation_sequence, observation_id, attempt_id, "
                "observation_kind, status, payload_json FROM observations "
                "ORDER BY observation_sequence"
            )
        ]
    finally:
        connection.close()


@pytest.mark.parametrize("fail_before", [True, False])
def test_uncertain_commit_fails_closed_and_resolves_on_reopen(
    tmp_path: Path, fail_before: bool
) -> None:
    monkeypatch = pytest.MonkeyPatch()
    store, target, ledger, execstate_path, sidecar = _composed(tmp_path)
    try:
        record, _receipt = store.read(target.submission_id, target)
        observation = make_observation(
            submission_id=target.submission_id,
            target=target,
            generation=record.generation,
            recovery_epoch=record.recovery_epoch,
            broker_reference="broker-ref-commit",
            observed_at=OBSERVED_AT,
        )
        before = _execstate_snapshot(execstate_path)
        attempt_id = derive_attempt_id(
            target.submission_id,
            target.model_dump_json(),
            record.generation,
            record.recovery_epoch,
        )
        monkeypatch.setattr(
            ledger._sidecar,
            "_connection",
            _CommitFaultConnection(
                ledger._sidecar._connection, fail_before=fail_before
            ),
        )
        with pytest.raises(ExecutionLedgerCommitUnknownError):
            ledger.record_observation(observation)
        # No retry, no repair, no further use of a poisoned store.
        with pytest.raises(ExecutionLedgerCommitUnknownError):
            ledger.read_observations(attempt_id)
        with pytest.raises(ExecutionLedgerCommitUnknownError):
            ledger.record_observation(observation)
        # The authoritative EXECSTATE database never moves.
        assert _execstate_snapshot(execstate_path) == before
        # Either the row landed completely, or it did not land at all.
        assert len(_sidecar_rows(sidecar)) == (0 if fail_before else 1)
    finally:
        ledger.close()
        store.close()
        monkeypatch.undo()

    # Reopen from scratch: the outcome is now knowable and coherence-checked.
    reopened = SQLiteExecutionStateStore(execstate_path)
    try:
        recover_store(reopened, target, observed_at=LATE_AT)
        reopened_ledger = DurableExecutionLedger(reopened, sidecar)
        try:
            history = reopened_ledger.read_attempt_history(
                target.submission_id, target
            )
        finally:
            reopened_ledger.close()
    finally:
        reopened.close()
    rows = _sidecar_rows(sidecar)
    recorded = [
        view for view in history if view.observation_ids
    ]
    if fail_before:
        assert rows == []
        assert recorded == []
    else:
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][2].startswith("execstate-attempt-v1-")
        assert rows[0][4] == "ACCEPTED"
        assert len(recorded) == 1
        assert len(recorded[0].observation_ids) == 1
