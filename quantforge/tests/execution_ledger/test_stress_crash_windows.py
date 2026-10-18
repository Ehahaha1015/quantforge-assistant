"""Slice 8 bounded stress: repeated real-process crash windows.

Each crash window is repeated a bounded number of times in fresh directories:
process death right after the DISPATCHING commit, death after a sidecar
append, and death after a pure assessment. Every repetition must leave the
same fail-closed reconstruction: ambiguity stays OUTCOME_UNKNOWN, recorded
evidence survives exactly once, and nothing manufactures a resolution.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from _ledger_support import (
    CLAIM_AT,
    CLAIM_EXPIRES_AT,
    LATE_AT,
    PREPARE_AT,
    RECOVER_AT,
    RECOVER_RESCAN_AT,
    advance_to_dispatching,
    make_provenance,
    make_store,
    make_target,
    recover_store,
)

from quantforge.execstate import (
    ExecutionStateDeniedError,
    SQLiteExecutionStateStore,
)
from quantforge.execution_ledger import AttemptPhase
from quantforge.execution_ledger.ledger import DurableExecutionLedger

_CRASH_WORKER = Path(__file__).with_name("_oracle_crash_worker.py")
_CRASH_WINDOW_WORKER = Path(__file__).with_name(
    "_oracle_crash_window_worker.py"
)

_REPEATS = 3
_REOPEN_AT = LATE_AT
_REOPEN_LATER_AT = LATE_AT + timedelta(hours=1)


def _observation_rows(sidecar: Path) -> int:
    if not sidecar.exists():
        return 0
    connection = sqlite3.connect(str(sidecar), isolation_level=None)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _submission_row(database: Path) -> sqlite3.Row:
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT state, active_claim_id FROM submissions"
        ).fetchone()
    finally:
        connection.close()


def _composed(directory: Path):
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    state_dir = directory / "state"
    store = make_store(state_dir)
    try:
        recover_store(store, target, observed_at=RECOVER_AT)
        advance_to_dispatching(store, provenance, target)
    finally:
        store.close()
    return target, state_dir / "execstate.sqlite3", directory / "ledger.sqlite3"


def test_stress_dispatching_death_repetition(tmp_path: Path) -> None:
    for index in range(_REPEATS):
        directory = tmp_path / f"dispatch-{index}"
        directory.mkdir()
        database = directory / "execstate.sqlite3"
        sidecar = directory / "ledger.sqlite3"
        provenance = make_provenance()
        target = make_target(
            provenance.pre_dispatch_decision.envelope.submission_id
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(_CRASH_WORKER),
                str(database),
                target.model_dump_json(),
                target.submission_id,
                RECOVER_AT.isoformat(),
                PREPARE_AT.isoformat(),
                CLAIM_AT.isoformat(),
                CLAIM_EXPIRES_AT.isoformat(),
                RECOVER_RESCAN_AT.isoformat(),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        row = _submission_row(database)
        assert str(row["state"]) == "DISPATCHING"
        assert row["active_claim_id"] == "execstate-claim-v1-1"
        assert _observation_rows(sidecar) == 0

        store = SQLiteExecutionStateStore(database)
        ledger = None
        try:
            recover_store(store, target, observed_at=_REOPEN_AT)
            ledger = DurableExecutionLedger(store, sidecar)
            current = ledger.read_attempt(target.submission_id, target)
            history = ledger.read_attempt_history(
                target.submission_id, target
            )
            assert current.execstate_state == "OUTCOME_UNKNOWN"
            assert current.phase is AttemptPhase.UNKNOWN
            assert current.active_claim_id is None
            assert history[0].execstate_state == "DISPATCHING"
            assert history[0].active_claim_id == "execstate-claim-v1-1"
            assert history[0].observation_ids == ()
            assert current.observation_ids == ()
            # Ambiguity must not produce redispatch authority.
            with pytest.raises(ExecutionStateDeniedError):
                store.claim(
                    target.submission_id,
                    target,
                    owner_id="late-worker",
                    token="late-token",
                    acquired_at=_REOPEN_LATER_AT,
                    expires_at=_REOPEN_LATER_AT + timedelta(minutes=1),
                )
        finally:
            if ledger is not None:
                ledger.close()
            store.close()

        reopen = SQLiteExecutionStateStore(database)
        reopened_ledger = None
        try:
            recover_store(
                reopen, target, observed_at=_REOPEN_LATER_AT
            )
            reopened_ledger = DurableExecutionLedger(reopen, sidecar)
            reopened_current = reopened_ledger.read_attempt(
                target.submission_id, target
            )
            reopened_history = reopened_ledger.read_attempt_history(
                target.submission_id, target
            )
        finally:
            if reopened_ledger is not None:
                reopened_ledger.close()
            reopen.close()
        assert reopened_current.phase is AttemptPhase.UNKNOWN
        assert reopened_current.execstate_state == "OUTCOME_UNKNOWN"
        first = reopened_history[0]
        assert first.execstate_state == "DISPATCHING"
        assert first.observation_ids == ()
        assert _observation_rows(sidecar) == 0


def test_stress_sidecar_append_death_repetition(tmp_path: Path) -> None:
    for index in range(_REPEATS):
        directory = tmp_path / f"record-{index}"
        directory.mkdir()
        target, database, sidecar = _composed(directory)
        completed = subprocess.run(
            [
                sys.executable,
                str(_CRASH_WINDOW_WORKER),
                "record",
                str(database),
                target.model_dump_json(),
                str(sidecar),
                _REOPEN_AT.isoformat(),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert _observation_rows(sidecar) == 1

        recorded_ids: list[str] = []
        for moment in (_REOPEN_AT, _REOPEN_LATER_AT):
            store = SQLiteExecutionStateStore(database)
            ledger = None
            try:
                recover_store(store, target, observed_at=moment)
                ledger = DurableExecutionLedger(store, sidecar)
                history = ledger.read_attempt_history(
                    target.submission_id, target
                )
                current = ledger.read_attempt(
                    target.submission_id, target
                )
            finally:
                if ledger is not None:
                    ledger.close()
                store.close()
            assert current.phase is AttemptPhase.UNKNOWN
            assert current.execstate_state == "OUTCOME_UNKNOWN"
            assert all(
                view.phase is not AttemptPhase.FINALIZED
                for view in history
            )
            with_evidence = [
                view for view in history if view.observation_ids
            ]
            assert len(with_evidence) == 1
            assert len(with_evidence[0].observation_ids) == 1
            recorded_ids.append(with_evidence[0].observation_ids[0])
        assert recorded_ids == [
            "execstate-observation-v1-1",
            "execstate-observation-v1-1",
        ]
        assert _observation_rows(sidecar) == 1


def test_stress_assessment_death_repetition(tmp_path: Path) -> None:
    for index in range(_REPEATS):
        directory = tmp_path / f"assess-{index}"
        directory.mkdir()
        target, database, sidecar = _composed(directory)
        completed = subprocess.run(
            [
                sys.executable,
                str(_CRASH_WINDOW_WORKER),
                "assess",
                str(database),
                target.model_dump_json(),
                str(sidecar),
                _REOPEN_AT.isoformat(),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert _observation_rows(sidecar) == 0
        store = SQLiteExecutionStateStore(database)
        ledger = None
        try:
            recover_store(store, target, observed_at=_REOPEN_AT)
            ledger = DurableExecutionLedger(store, sidecar)
            history = ledger.read_attempt_history(
                target.submission_id, target
            )
            current = ledger.read_attempt(target.submission_id, target)
        finally:
            if ledger is not None:
                ledger.close()
            store.close()
        assert current.phase is AttemptPhase.UNKNOWN
        assert current.execstate_state == "OUTCOME_UNKNOWN"
        assert all(view.observation_ids == () for view in history)
        assert all(
            view.phase is not AttemptPhase.FINALIZED for view in history
        )
        assert _observation_rows(sidecar) == 0
