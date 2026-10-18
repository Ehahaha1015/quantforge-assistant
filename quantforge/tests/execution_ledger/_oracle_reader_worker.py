"""One fresh OS process that reconstructs ledger views (Oracles B and E)."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.execution_ledger.ledger import (
    DurableExecutionLedger,
)
from quantforge.frontier import ExecutionTargetBinding


def _view(view) -> dict[str, object]:
    payload = asdict(view)
    payload["target"] = view.target.model_dump_json()
    return payload


def main() -> None:
    (
        database,
        target_json,
        sidecar_path,
        recover_at,
        output_path,
    ) = sys.argv[1:]
    target = ExecutionTargetBinding.from_json(target_json)
    store = SQLiteExecutionStateStore(Path(database))
    ledger = None
    try:
        store.recover(
            target, observed_at=datetime.fromisoformat(recover_at)
        )
        ledger = DurableExecutionLedger(store, sidecar_path)
        history = ledger.read_attempt_history(
            target.submission_id, target
        )
        current = ledger.read_attempt(target.submission_id, target)
        observations = {
            view.attempt_id: [
                {
                    "observation_id": item.observation_id,
                    "status": item.status.value,
                    "observed_at": item.observed_at.isoformat(),
                }
                for item in ledger.read_observations(view.attempt_id)
            ]
            for view in history
        }
        payload = {
            "history": [_view(view) for view in history],
            "current": _view(current),
            "observations": observations,
        }
        Path(output_path).write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
    finally:
        if ledger is not None:
            ledger.close()
        store.close()


if __name__ == "__main__":
    main()
