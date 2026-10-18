import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from qf_platform.storage import PlatformError, canonical, digest
from qf_platform.tasks import TaskService


def task(root: Path) -> tuple[TaskService, str]:
    service = TaskService(root)
    created = service.create(
        {
            "mode": "OFFLINE_DEMO",
            "dataset": "synthetic-regimes-v1",
            "goal": "Synthetic immutable task blob tests only.",
            "max_experiments": 2,
            "analysis": "strategy_comparison",
        },
        "fixture",
    )
    return service, created["id"]


def test_growing_evidence_and_full_metadata_match_reference(tmp_path: Path) -> None:
    service, identity = task(tmp_path)
    with closing(service.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        state = service._history(db, identity)[-1]["state"]
        for index in range(6):
            state["evidence"].append(
                {
                    "evidence_id": "SYNTHETIC_TEST_ONLY_" + str(index),
                    "tool": "run_experiment",
                    "input": {},
                    "result": {"test_only": True, "curve": list(range(2000))},
                }
            )
            service._append(db, state, "TEST_ONLY_PROGRESS", "Synthetic test progress")
        original = service._snapshot(service._history(db, identity))
        projected = service._snapshot(service._projected_history(db, identity))
        original.pop("observed_at")
        projected.pop("observed_at")
        assert original == projected and len(projected["evidence"]) == 6
        assert db.execute("SELECT count(*) FROM state_blobs").fetchone()[0] == 6
        assert all(
            "state" not in row for row in service._projected_history(db, identity)[:-1]
        )
    assert len(service.get(identity)["events"]) == 7


def test_legacy_inline_events_are_unchanged_after_new_codec_append(
    tmp_path: Path,
) -> None:
    service, identity = task(tmp_path)
    with closing(service.connect()) as db, db:
        state = service._history(db, identity)[-1]["state"]
        # A separate test-only task carries a genuine old-style event envelope.
        old_id = "task-" + "a" * 32
        state["id"] = old_id
        db.execute(
            "INSERT INTO tasks VALUES(?,?,?,?)",
            (old_id, "legacy", "fixture", state["created_at"]),
        )
        value = {
            "at": state["created_at"],
            "kind": "TEST_ONLY_LEGACY",
            "summary": "Legacy fixture",
            "details": {},
            "state": state,
        }
        raw = canonical(value)
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (
                old_id,
                1,
                raw,
                "0" * 64,
                digest({"previous": "0" * 64, "payload": value}),
            ),
        )
        service._append(db, state, "TEST_ONLY_NEW_CODEC", "Current codec fixture")
        assert (
            db.execute(
                "SELECT payload FROM events WHERE task_id=? AND seq=1", (old_id,)
            ).fetchone()[0]
            == raw
        )
    assert service.get(old_id)["version"] == 2


def test_referenced_blob_cannot_be_changed_or_removed(tmp_path: Path) -> None:
    service, identity = task(tmp_path)
    with closing(service.connect()) as db, db:
        state = service._history(db, identity)[-1]["state"]
        state["evidence"] = [{"evidence_id": "TEST_ONLY", "test_only": True}]
        service._append(db, state, "TEST_ONLY", "Test blob")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE state_blobs SET payload='{}'")
        db.execute("DROP TRIGGER state_blobs_no_update")
        db.execute("UPDATE state_blobs SET payload='{}'")
    with pytest.raises(PlatformError, match="BLOB_INTEGRITY_FAILED"):
        service.get(identity)


def test_earlier_blob_and_event_are_verified_even_when_absent_from_latest(
    tmp_path: Path,
) -> None:
    service, identity = task(tmp_path)
    with closing(service.connect()) as db, db:
        state = service._history(db, identity)[-1]["state"]
        state["evidence"] = [{"test_only": True}]
        service._append(db, state, "TEST_ONLY", "Old blob fixture")
        state["evidence"] = []
        service._append(db, state, "TEST_ONLY", "No current blob fixture")
        db.execute("DROP TRIGGER state_blobs_no_delete")
        db.execute("DELETE FROM state_blobs")
    with pytest.raises(PlatformError, match="BLOB_MISSING"):
        service.get(identity)


def test_foreign_codec_is_not_interpreted_as_inline_data(tmp_path: Path) -> None:
    service, identity = task(tmp_path)
    with closing(service.connect()) as db, db:
        value = json.loads(
            db.execute(
                "SELECT payload FROM events WHERE task_id=?", (identity,)
            ).fetchone()[0]
        )
        value["state_codec"] = "UNSUPPORTED_TEST_ONLY"
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=?,hash=?",
            (canonical(value), digest({"previous": "0" * 64, "payload": value})),
        )
    with pytest.raises(PlatformError, match="CODEC_UNSUPPORTED"):
        service.get(identity)
