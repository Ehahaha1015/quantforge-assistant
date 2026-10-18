"""One real OS process that dies after the DISPATCHING commit (Oracle B)."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ledger_support import (
    build_eligibility,
    make_provenance,
)

from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.frontier import ExecutionTargetBinding


def main() -> None:
    (
        database,
        target_json,
        submission_id,
        recover_at,
        prepared_at,
        acquired_at,
        expires_at,
        rescan_at,
    ) = sys.argv[1:]
    target = ExecutionTargetBinding.from_json(target_json)
    provenance = make_provenance()
    store = SQLiteExecutionStateStore(Path(database))
    store.recover(
        target, observed_at=datetime.fromisoformat(recover_at)
    )
    record, receipt = store.prepare(
        provenance.pre_dispatch_decision.envelope,
        provenance,
        target,
        prepared_at=datetime.fromisoformat(prepared_at),
    )
    claim = store.claim(
        submission_id,
        target,
        owner_id="crash-worker",
        token="crash-token",
        acquired_at=datetime.fromisoformat(acquired_at),
        expires_at=datetime.fromisoformat(expires_at),
    )
    recovery = store.recover(
        target, observed_at=datetime.fromisoformat(rescan_at)
    )
    decision = build_eligibility(
        provenance, target, record, receipt, recovery, claim
    )
    store.mark_dispatching(
        decision, owner_id="crash-worker", token="crash-token"
    )
    os._exit(0)


if __name__ == "__main__":
    main()
