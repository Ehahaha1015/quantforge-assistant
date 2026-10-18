"""Fresh-process worker for schema-v2 oracles BB and BC."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.frontier import ExecutionTargetBinding


def main() -> None:
    database = Path(sys.argv[1])
    target = ExecutionTargetBinding.from_json(sys.argv[2])
    moment = datetime.strptime(
        sys.argv[3], "%Y-%m-%dT%H:%M:%S.%fZ"
    ).replace(tzinfo=UTC)
    store = SQLiteExecutionStateStore(database)
    try:
        evidence = store.recover(target, observed_at=moment)
        print(
            json.dumps(
                {
                    "status": evidence.status,
                    "evidence_id": evidence.evidence_id,
                    "unresolved_submission_ids": list(
                        evidence.unresolved_submission_ids
                    ),
                },
                sort_keys=True,
            )
        )
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        store.close()


if __name__ == "__main__":
    main()
