"""One real OS process in the Oracle A two-worker claim race."""

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
from quantforge.execution_ledger.ledger import (
    DurableExecutionLedger,
)
from quantforge.frontier import ExecutionTargetBinding


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
    result = {
        "result": "FAILED",
        "claim_id": None,
        "observation_id": None,
        "message": "",
    }
    store = SQLiteExecutionStateStore(Path(database))
    try:
        deadline = time.monotonic() + 30.0
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
        Path(ready_path).write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 30.0
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
        try:
            observation = make_observation(
                submission_id=submission_id,
                target=target,
                generation=record.generation,
                recovery_epoch=record.recovery_epoch,
                broker_reference=f"broker-ref-{owner_id}",
            )
            assessment = ledger.record_observation(observation)
            result["observation_id"] = assessment.observation_id
        finally:
            ledger.close()
    except ExecutionStateError as exc:
        result["result"] = type(exc).__name__
        result["message"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        result["result"] = type(exc).__name__
        result["message"] = str(exc)
    finally:
        store.close()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
