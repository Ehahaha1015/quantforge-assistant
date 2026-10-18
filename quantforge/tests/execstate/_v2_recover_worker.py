"""Fresh-process recovery worker for the run-key contention oracle."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from quantforge.execstate import ExecutionStateError, SQLiteExecutionStateStore
from quantforge.frontier import ExecutionTargetBinding


def main() -> None:
    database = Path(sys.argv[1])
    target = ExecutionTargetBinding.from_json(sys.argv[2])
    moment = datetime.strptime(sys.argv[3], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=UTC
    )
    result_path = Path(sys.argv[4])
    store = SQLiteExecutionStateStore(database)
    try:
        evidence = store.recover(target, observed_at=moment)
        payload = {"status": "ok", "evidence_id": evidence.evidence_id}
    except (ExecutionStateError, sqlite3.Error) as exc:
        payload = {"status": type(exc).__name__}
    finally:
        store.close()
    result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
