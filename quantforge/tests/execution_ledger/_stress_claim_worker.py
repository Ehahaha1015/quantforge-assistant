"""One real OS process in the Slice 8 claim-authority race.

Only the pre-race setup may be retried: concurrent opens can contend on WAL
configuration and setup recovery is contracted to fail closed with
`ExecutionStateBusyError`. The measured claim below stays strictly one-shot
so the two processes contend for dispatch authority exactly once, and every
setup retry is reported so the stress test can classify it.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ledger_support import build_eligibility, make_provenance
from _support import make_observation

from quantforge.execstate import (
    ExecutionStateBusyError,
    ExecutionStateError,
    SQLiteExecutionStateStore,
)
from quantforge.execution_ledger.ledger import DurableExecutionLedger
from quantforge.frontier import ExecutionTargetBinding

_SETUP_DEADLINE_SECONDS = 60.0
_BARRIER_DEADLINE_SECONDS = 60.0


def _open_store(database: Path) -> tuple[SQLiteExecutionStateStore, int]:
    retries = 0
    deadline = time.monotonic() + _SETUP_DEADLINE_SECONDS
    while True:
        try:
            return SQLiteExecutionStateStore(database), retries
        except ExecutionStateBusyError:
            retries += 1
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)


def main() -> None:
    (
        database,
        target_json,
        submission_id,
        owner_id,
        token,
        recover_at,
        acquired_at,
        expires_at,
        ready_path,
        other_ready_path,
        sidecar_path,
    ) = sys.argv[1:]
    target = ExecutionTargetBinding.from_json(target_json)
    result: dict[str, object] = {
        "result": "FAILED",
        "claim_id": None,
        "observation_id": None,
        "setup_retries": 0,
        "message": "",
    }
    store, retries = _open_store(Path(database))
    setup_retries = retries
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
                setup_retries += 1
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        result["setup_retries"] = setup_retries
        Path(ready_path).write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + _BARRIER_DEADLINE_SECONDS
        while not Path(other_ready_path).exists():
            if time.monotonic() > deadline:
                raise TimeoutError("claim race barrier timed out")
            time.sleep(0.01)
        claim = store.claim(
            submission_id,
            target,
            owner_id=owner_id,
            token=token,
            acquired_at=datetime.fromisoformat(acquired_at),
            expires_at=datetime.fromisoformat(expires_at),
        )
        result["result"] = "ACQUIRED"
        result["claim_id"] = claim.claim_id
        recovery = store.recover(
            target, observed_at=datetime.fromisoformat(recover_at)
        )
        record, receipt = store.read(submission_id, target)
        provenance = make_provenance()
        decision = build_eligibility(
            provenance, target, record, receipt, recovery, claim
        )
        store.mark_dispatching(
            decision, owner_id=owner_id, token=token
        )
        record, _receipt = store.read(submission_id, target)
        ledger = DurableExecutionLedger(store, sidecar_path)
        observation = make_observation(
            submission_id=submission_id,
            target=target,
            generation=record.generation,
            recovery_epoch=record.recovery_epoch,
            broker_reference=f"broker-ref-{owner_id}",
        )
        assessment = ledger.record_observation(observation)
        result["observation_id"] = assessment.observation_id
    except ExecutionStateError as exc:
        result["result"] = type(exc).__name__
        result["message"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        result["result"] = type(exc).__name__
        result["message"] = str(exc)
    finally:
        if ledger is not None:
            ledger.close()
        store.close()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
