import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from qf_platform import task_tools
from qf_platform.storage import PlatformError, canonical
from qf_platform.tasks import TaskService


def task(app: TaskService) -> str:
    return str(
        app.create(
            {
                "mode": "OFFLINE_DEMO",
                "dataset": "synthetic-regimes-v1",
                "goal": "Write transaction integrity and recovery regression.",
                "max_experiments": 2,
                "analysis": "cost_sensitivity",
            },
            "write-integrity",
        )["id"]
    )


def corrupt(app: TaskService, kind: str) -> None:
    with app.connect() as db:
        if kind == "event":
            db.execute("DROP TRIGGER events_no_update")
            raw = json.loads(
                db.execute("SELECT payload FROM events WHERE seq=1").fetchone()[0]
            )
            raw["summary"] = "Forged prefix, unchanged last event"
            db.execute("UPDATE events SET payload=? WHERE seq=1", (canonical(raw),))
        elif kind == "deletion":
            db.execute("DROP TRIGGER events_no_delete")
            db.execute("DELETE FROM events WHERE seq=1")
        else:
            db.execute("DROP TRIGGER state_blobs_no_update")
            db.execute("UPDATE state_blobs SET payload='{}'")


@pytest.mark.parametrize("kind", ["event", "deletion", "blob"])
@pytest.mark.parametrize("during_compute", [False, True])
def test_worker_fully_rejects_corrupt_prefix_before_each_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    during_compute: bool,
) -> None:
    app = TaskService(tmp_path)
    task_id = task(app)
    assert app.tick()
    original = task_tools.execute
    calls: list[str] = []

    def execute(
        tool: str, payload: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        calls.append(tool)
        result = original(tool, payload, evidence)
        corrupt(app, kind)
        return result

    if during_compute:
        monkeypatch.setattr(task_tools, "execute", execute)
    else:
        corrupt(app, kind)
    with pytest.raises(PlatformError, match="INTEGRITY|GAP"):
        app.tick()
    assert len(calls) == int(during_compute)
    # Only the already committed first step has completion evidence. A start
    # may be retained when corruption happens while the pure tool computes.
    with app.connect() as db:
        completed = db.execute(
            "SELECT count(*) FROM events WHERE "
            "json_extract(payload,'$.kind')='TOOL_COMPLETED'"
        ).fetchone()[0]
    assert completed == 1
    with pytest.raises(PlatformError, match="INTEGRITY|GAP"):
        TaskService(tmp_path).get(task_id)


def test_final_report_failure_rolls_back_completion_and_recovery_has_no_duplicate(
    tmp_path: Path,
) -> None:
    app = TaskService(tmp_path)
    task_id = task(app)
    for _ in range(3):
        assert app.tick()
    before = app.get(task_id)
    with app.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_final_report BEFORE INSERT ON events "
            "WHEN json_extract(NEW.payload,'$.kind')='REPORT_READY' "
            "BEGIN SELECT RAISE(ABORT,'injected report failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected report failure"):
        app.tick()
    interrupted = TaskService(tmp_path).get(task_id)
    assert interrupted["version"] == before["version"] + 1
    assert interrupted["completed_steps"] == 3
    assert interrupted["evidence"] == before["evidence"]
    assert interrupted["report"] is None
    with app.connect() as db:
        db.execute("DROP TRIGGER fail_final_report")
    restarted = TaskService(tmp_path)
    assert restarted.tick()
    recovered = restarted.get(task_id)
    assert recovered["status"] == "SUCCEEDED"
    assert recovered["completed_steps"] == 4
    assert recovered["evidence"][:3] == before["evidence"]
    assert len({row["evidence_id"] for row in recovered["evidence"]}) == 4
    assert sum(row["kind"] == "TOOL_COMPLETED" for row in recovered["events"]) == 4
