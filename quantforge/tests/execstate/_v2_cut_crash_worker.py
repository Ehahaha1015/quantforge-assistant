"""Process-death worker for the schema-v2 recovery-cut transaction windows."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path


def main() -> None:
    database = Path(sys.argv[1])
    target_json = sys.argv[2]
    moment = sys.argv[3]
    mode = sys.argv[4]
    connection = sqlite3.connect(
        str(database),
        timeout=0.0,
        detect_types=0,
        isolation_level=None,
        check_same_thread=True,
        uri=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("BEGIN IMMEDIATE")
    run_key = int(
        connection.execute(
            "SELECT COALESCE(MAX(run_sequence), 0) + 1 FROM recovery_runs"
        ).fetchone()[0]
    )
    sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(event_sequence), 0) + 1 FROM audit_events"
        ).fetchone()[0]
    )
    epoch = (
        int(
            connection.execute(
                "SELECT value FROM metadata WHERE key = 'next_recovery_epoch'"
            ).fetchone()[0]
        )
        - 1
    )
    for phase, kind, anchor_sequence in (
        ("BEGIN", "RECOVERY_CUT_BEGIN", sequence),
        ("END", "RECOVERY_CUT_END", sequence + 1),
    ):
        payload = {
            "event_sequence": anchor_sequence,
            "row_scope": "RECOVERY_BOUNDARY",
            "target_json": target_json,
            "recovery_epoch": epoch,
            "event_kind": kind,
            "boundary_phase": phase,
            "boundary_run_sequence": run_key,
            "observed_at": moment,
            "reason": kind,
        }
        connection.execute(
            """
            INSERT INTO audit_events(
                event_sequence, row_scope, submission_id, target_json,
                prior_state, new_state, generation, recovery_epoch, event_kind,
                reason, observed_at, owner_id, token_reference, claim_id,
                payload_sequence, status, boundary_run_sequence, boundary_phase,
                event_json
            ) VALUES (?, 'RECOVERY_BOUNDARY', NULL, ?, NULL, NULL, NULL, ?, ?,
                      ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                anchor_sequence,
                target_json,
                epoch,
                kind,
                kind,
                moment,
                run_key,
                phase,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )
    scanned = int(
        connection.execute(
            "SELECT COALESCE(MAX(generation), 0) FROM submissions"
        ).fetchone()[0]
    )
    unresolved = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT submission_id FROM submissions
            WHERE state IN ('OUTCOME_UNKNOWN', 'DISPATCHING')
            ORDER BY submission_id
            """
        ).fetchall()
    ]
    evidence = {
        "schema": "frontier-recovery-v1",
        "evidence_id": f"execstate-recovery-v1-{run_key}",
        "target": json.loads(target_json),
        "recovery_epoch": epoch,
        "scanned_through_generation": scanned,
        "status": "COMPLETE",
        "unresolved_submission_ids": sorted(unresolved),
        "observed_at": moment,
    }
    connection.execute(
        """
        INSERT INTO recovery_runs(
            run_sequence, evidence_id, target_json, recovery_epoch,
            scanned_through_generation, status, evidence_json, observed_at
        ) VALUES (?, ?, ?, ?, ?, 'COMPLETE', ?, ?)
        """,
        (
            run_key,
            evidence["evidence_id"],
            target_json,
            epoch,
            scanned,
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            moment,
        ),
    )
    if mode == "committed":
        connection.commit()
        os._exit(0)
    os._exit(3)


if __name__ == "__main__":
    main()
