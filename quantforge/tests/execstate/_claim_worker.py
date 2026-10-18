from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from quantforge.execstate import (
    ExecutionStateBusyError,
    ExecutionStateError,
    SQLiteExecutionStateStore,
)
from quantforge.frontier import ExecutionTargetBinding


def main() -> None:
    (
        db,
        target_json,
        submission_id,
        owner_id,
        token,
        recover_at,
        acquired_at,
        expires_at,
        ready_path,
        other_ready_path,
    ) = sys.argv[1:]
    target = ExecutionTargetBinding.from_json(target_json)
    store = SQLiteExecutionStateStore(Path(db))
    try:
        # Two processes may open the same database at once, and BEGIN IMMEDIATE
        # contention is contracted to fail closed with ExecutionStateBusyError.
        # Only this pre-race setup step is retried so that the measured claim
        # race below stays strictly one-shot.
        setup_deadline = time.monotonic() + 30.0
        while True:
            try:
                store.recover(
                    target,
                    observed_at=datetime.fromisoformat(recover_at),
                )
                break
            except ExecutionStateBusyError:
                if time.monotonic() > setup_deadline:
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
        print(
            json.dumps(
                {"result": "ACQUIRED", "claim": claim.model_dump_json()},
                sort_keys=True,
            )
        )
    except ExecutionStateError as exc:
        print(
            json.dumps(
                {"result": type(exc).__name__, "message": str(exc)},
                sort_keys=True,
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
