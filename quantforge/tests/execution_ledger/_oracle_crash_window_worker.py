"""Crash-window worker for Oracle K (sidecar/EXECSTATE commit windows)."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import make_observation

from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.execution_ledger.ledger import (
    DurableExecutionLedger,
)
from quantforge.frontier import ExecutionTargetBinding


def main() -> None:
    mode, database, target_json, sidecar, recover_at = sys.argv[1:]
    target = ExecutionTargetBinding.from_json(target_json)
    store = SQLiteExecutionStateStore(Path(database))
    store.recover(
        target, observed_at=datetime.fromisoformat(recover_at)
    )
    ledger = DurableExecutionLedger(store, sidecar)
    try:
        record, _receipt = store.read(target.submission_id, target)
        observation = make_observation(
            submission_id=target.submission_id,
            target=target,
            generation=record.generation,
            recovery_epoch=record.recovery_epoch,
            broker_reference="broker-ref-crash",
        )
        if mode == "record":
            ledger.record_observation(observation)
        elif mode == "assess":
            ledger.assess_observation(observation)
        else:
            raise ValueError(mode)
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
