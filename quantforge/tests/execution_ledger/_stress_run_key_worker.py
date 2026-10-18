"""Run-key stress worker for the Slice 8 EXECSTATE race matrix.

Only the pre-race store construction may be retried: several processes can
contend on the WAL configuration of a freshly opened database, and EXECSTATE
is contracted to fail closed with `ExecutionStateBusyError` for it. The
measured recovery that allocates the run key stays strictly one-shot, and
every startup retry is reported so the stress test can classify it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from quantforge.execstate import (
    ExecutionStateBusyError,
    ExecutionStateError,
    SQLiteExecutionStateStore,
)
from quantforge.frontier import ExecutionTargetBinding

_SETUP_DEADLINE_SECONDS = 30.0


def main() -> None:
    database = Path(sys.argv[1])
    target = ExecutionTargetBinding.from_json(sys.argv[2])
    moment = datetime.strptime(
        sys.argv[3], "%Y-%m-%dT%H:%M:%S.%fZ"
    ).replace(tzinfo=UTC)
    result_path = Path(sys.argv[4])
    setup_retries = 0
    deadline = time.monotonic() + _SETUP_DEADLINE_SECONDS
    store = None
    try:
        while store is None:
            try:
                store = SQLiteExecutionStateStore(database)
            except ExecutionStateBusyError:
                setup_retries += 1
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        evidence = store.recover(target, observed_at=moment)
        payload: dict[str, object] = {
            "status": "ok",
            "evidence_id": evidence.evidence_id,
            "setup_retries": setup_retries,
        }
    except (ExecutionStateError, sqlite3.Error) as exc:
        payload = {
            "status": type(exc).__name__,
            "evidence_id": None,
            "setup_retries": setup_retries,
        }
    finally:
        if store is not None:
            store.close()
    result_path.write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
