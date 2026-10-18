from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _helpers import (
    make_provenance,
    make_target,
    recover_store,
)

from quantforge.execstate import ExecutionStateNotFoundError, SQLiteExecutionStateStore


@pytest.mark.parametrize(
    ("crash_call", "crash_when", "expected"),
    [
        (2, "before", "NO_SUBMISSION"),
        (2, "after", "PREPARED"),
        (3, "after", "PREPARED"),
        (5, "after", "OUTCOME_UNKNOWN"),
        (6, "before", "OUTCOME_UNKNOWN"),
        (6, "after", "ACKNOWLEDGED"),
    ],
)
def test_r_through_w_real_process_crash_and_reopen(
    tmp_path, crash_call: int, crash_when: str, expected: str
) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    database = tmp_path / "crash.sqlite3"
    worker = Path(__file__).with_name("_crash_worker.py")
    completed = subprocess.run(
        [
            sys.executable,
            str(worker),
            str(database),
            target.model_dump_json(),
            provenance.model_dump_json(),
            str(crash_call),
            crash_when,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 99, completed.stderr

    reopened = SQLiteExecutionStateStore(database)
    recovery = recover_store(
        reopened,
        target,
        observed_at=datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC),
    )
    if expected == "NO_SUBMISSION":
        with pytest.raises(ExecutionStateNotFoundError):
            reopened.read(envelope.submission_id, target)
        assert recovery.scanned_through_generation == 0
    else:
        record, _receipt = reopened.read(envelope.submission_id, target)
        assert record.state == expected
        if expected == "OUTCOME_UNKNOWN":
            assert envelope.submission_id in recovery.unresolved_submission_ids
        if crash_call == 3:
            active = reopened._connection.execute(
                """
                SELECT active_claim_id FROM submissions
                WHERE submission_id = ? AND target_json = ?
                """,
                (envelope.submission_id, target.model_dump_json()),
            ).fetchone()
            assert active["active_claim_id"] is None
    reopened.close()
