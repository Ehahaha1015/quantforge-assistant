"""One real OS process in the Slice 8 ledger observation races."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import make_observation

from quantforge.execstate import (
    ExecutionStateBusyError,
    ExecutionStateError,
    SQLiteExecutionStateStore,
)
from quantforge.execution_ledger.errors import ExecutionLedgerError
from quantforge.execution_ledger.ledger import DurableExecutionLedger
from quantforge.execution_ledger.models import BrokerObservationStatus
from quantforge.frontier import ExecutionTargetBinding

_SETUP_DEADLINE_SECONDS = 30.0
_BARRIER_DEADLINE_SECONDS = 30.0


def main() -> None:
    (
        mode,
        database,
        target_json,
        sidecar_path,
        recover_at,
        observed_at,
        status,
        broker_reference,
        ready_dir,
        worker_index,
        expected_ready,
        go_path,
        result_path,
    ) = sys.argv[1:]
    target = ExecutionTargetBinding.from_json(target_json)
    result: dict[str, object] = {
        "result": "FAILED",
        "observation_id": None,
        "reason": None,
        "attempt_id": None,
        "message": "",
    }
    # Setup only: concurrent opens can contend on WAL configuration, which
    # EXECSTATE is contracted to fail closed with `ExecutionStateBusyError`.
    # The measured observation race below stays strictly one-shot.
    store = None
    setup_deadline = time.monotonic() + _SETUP_DEADLINE_SECONDS
    while store is None:
        try:
            store = SQLiteExecutionStateStore(Path(database))
        except ExecutionStateBusyError:
            if time.monotonic() > setup_deadline:
                raise
            time.sleep(0.01)
    ledger = None
    try:
        deadline = time.monotonic() + _SETUP_DEADLINE_SECONDS
        while True:
            try:
                store.recover(
                    target,
                    observed_at=datetime.fromisoformat(recover_at),
                )
                break
            except ExecutionStateBusyError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        ready_root = Path(ready_dir)
        ready_root.mkdir(parents=True, exist_ok=True)
        (ready_root / f"ready-{worker_index}").write_text(
            "ready", encoding="utf-8"
        )
        deadline = time.monotonic() + _BARRIER_DEADLINE_SECONDS
        while True:
            present = len(list(ready_root.glob("ready-*")))
            if present >= int(expected_ready):
                break
            if time.monotonic() > deadline:
                raise TimeoutError("observation race barrier timed out")
            time.sleep(0.005)
        deadline = time.monotonic() + _BARRIER_DEADLINE_SECONDS
        while not Path(go_path).exists():
            if time.monotonic() > deadline:
                raise TimeoutError("observation race gate timed out")
            time.sleep(0.005)
        record, _receipt = store.read(target.submission_id, target)
        ledger = DurableExecutionLedger(store, sidecar_path)
        observation = make_observation(
            submission_id=target.submission_id,
            target=target,
            generation=record.generation,
            recovery_epoch=record.recovery_epoch,
            status=BrokerObservationStatus(status),
            broker_reference=broker_reference,
            observed_at=datetime.fromisoformat(observed_at),
        )
        if mode == "record":
            assessment = ledger.record_observation(observation)
        elif mode == "assess":
            assessment = ledger.assess_observation(observation)
        else:
            raise ValueError(mode)
        result["result"] = (
            "RECORDED" if assessment.recorded else "ASSESSED"
        )
        result["observation_id"] = assessment.observation_id
        result["reason"] = assessment.reason.value
        result["attempt_id"] = assessment.attempt_id
    except ExecutionStateError as exc:
        result["result"] = type(exc).__name__
        result["message"] = str(exc)
    except ExecutionLedgerError as exc:
        result["result"] = type(exc).__name__
        result["message"] = str(exc)
        assessment = getattr(exc, "assessment", None)
        if assessment is not None:
            result["reason"] = assessment.reason.value
            result["attempt_id"] = assessment.attempt_id
    except Exception as exc:  # noqa: BLE001
        result["result"] = type(exc).__name__
        result["message"] = str(exc)
    finally:
        if ledger is not None:
            ledger.close()
        store.close()
    Path(result_path).write_text(
        json.dumps(result, sort_keys=True), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
