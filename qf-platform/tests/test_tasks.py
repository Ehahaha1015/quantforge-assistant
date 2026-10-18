from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from qf_platform import task_tools
from qf_platform.server import make_server
from qf_platform.storage import PlatformError
from qf_platform.tasks import TaskService


def request(**changes: Any) -> dict[str, Any]:
    return {
        "mode": "OFFLINE_DEMO",
        "dataset": "synthetic-regimes-v1",
        "goal": "Compare two strategies under synthetic transaction costs.",
        "max_experiments": 6,
        "analysis": "cost_sensitivity",
        **changes,
    }


def complete(service: TaskService, task_id: str) -> dict[str, Any]:
    for _ in range(10):
        service.tick()
        state = service.get(task_id)
        if state["status"] in {"FAILED", "SUCCEEDED"}:
            return state
    pytest.fail("Task did not terminate within its declared plan")


def test_real_core_results_and_report_references(tmp_path: Path) -> None:
    app = TaskService(tmp_path)
    created = app.create(request(), "core-run")
    result = complete(app, created["id"])
    assert result["status"] == "SUCCEEDED", result["error"]
    assert result["experiments_used"] == 6
    assert result["completed_steps"] == result["total_steps"] == 8
    assert result["paid_model_calls"] == 0
    assert result["live_authority"] == "NONE"
    report = result["report"]
    ids = {value["evidence_id"] for value in result["evidence"]}
    assert set(report["evidence_refs"]) == ids
    assert all(row["evidence_id"] in ids for row in report["rows"])
    for family in task_tools.FAMILIES:
        rows = [row for row in report["rows"] if row["family"] == family]
        assert [row["fee_bps"] for row in rows] == [0, 10, 30]
        assert (
            rows[0]["total_return"] > rows[1]["total_return"] > rows[2]["total_return"]
        )
    assert "NO VALID ALPHA" in report["markdown"]
    assert "Core" not in report["conclusion"]
    assert (
        TaskService(tmp_path).get(created["id"])["ledger_hash"] == result["ledger_hash"]
    )
    assert not app.tick()


def test_create_idempotency_and_conflict_across_threads(tmp_path: Path) -> None:
    app = TaskService(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: app.create(request(), "same-key"), range(12)))
    assert len({value["id"] for value in results}) == 1
    assert len(app.list()["items"]) == 1
    before = app.get(results[0]["id"])["version"]
    with pytest.raises(PlatformError, match="IDEMPOTENCY_CONFLICT"):
        app.create(request(max_experiments=2), "same-key")
    assert app.get(results[0]["id"])["version"] == before


def test_cancel_resume_reuses_completed_steps_and_budget(tmp_path: Path) -> None:
    app = TaskService(tmp_path)
    task = app.create(request(max_experiments=4), "resume")
    app.tick()
    app.tick()
    before = app.get(task["id"])
    app.control(task["id"], "cancel")
    assert not app.tick()
    resumed = TaskService(tmp_path)
    assert resumed.get(task["id"])["status"] == "CANCELLED"
    resumed.control(task["id"], "resume")
    result = complete(resumed, task["id"])
    assert result["experiments_used"] == 4
    assert result["evidence"][:2] == before["evidence"]
    completed = [
        event for event in result["events"] if event["kind"] == "TOOL_COMPLETED"
    ]
    assert len(completed) == result["total_steps"] == 6
    with pytest.raises(PlatformError, match="RESUME_REQUIRES_CANCELLED"):
        app.control(task["id"], "resume")


def test_interrupted_started_step_is_recomputed_without_duplicate_evidence(
    tmp_path: Path,
) -> None:
    app = TaskService(tmp_path)
    task = app.create(request(max_experiments=2), "interrupted")
    app.tick()
    with app.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = app._history(db, task["id"])[-1]["state"]
        app._append(db, state, "TOOL_STARTED", "Interrupted pure tool")
    restarted = TaskService(tmp_path)
    result = complete(restarted, task["id"])
    assert result["experiments_used"] == 2
    assert len(result["evidence"]) == result["total_steps"] == 4
    assert len({row["evidence_id"] for row in result["evidence"]}) == 4


def test_cancel_during_compute_discards_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = TaskService(tmp_path)
    task = app.create(request(), "cancel-in-flight")
    original = task_tools.execute

    def cancel(
        tool: str, payload: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        app.control(task["id"], "cancel")
        return original(tool, payload, evidence)

    monkeypatch.setattr(task_tools, "execute", cancel)
    app.tick()
    state = app.get(task["id"])
    assert state["status"] == "CANCELLED"
    assert state["completed_steps"] == 0
    assert state["evidence"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "LIVE"},
        {"dataset": "../private"},
        {"max_experiments": True},
        {"max_experiments": 2.0},
        {"max_experiments": 100},
        {"analysis": "shell"},
        {"metrics": {"return": 1}},
    ],
)
def test_untrusted_configuration_cannot_create_task(
    tmp_path: Path,
    changes: dict[str, Any],
) -> None:
    app = TaskService(tmp_path)
    with pytest.raises(ValueError):
        app.create(request(**changes), "bad-config")
    assert app.list()["items"] == []


def test_tools_refuse_unknown_operations_and_metrics_injection() -> None:
    with pytest.raises(PlatformError, match="TOOL_NOT_ALLOWED"):
        task_tools.execute("submit_order", {}, [])
    with pytest.raises(ValueError):
        task_tools.execute(
            "run_experiment",
            {
                "dataset": task_tools.DATASET,
                "family": "moving_average_crossover",
                "fee_bps": 0,
                "metrics": {"total_return": 99},
            },
            [],
        )
    with pytest.raises(PlatformError, match="EVIDENCE_REQUIRED"):
        task_tools.execute("compare_results", {}, [])
    with pytest.raises(ValueError):
        task_tools.execute(
            "run_experiment",
            {
                "dataset": task_tools.DATASET,
                "family": "moving_average_crossover",
                "fee_bps": False,
            },
            [],
        )


def test_tampered_events_block_reads(tmp_path: Path) -> None:
    app = TaskService(tmp_path)
    task = app.create(request(), "tamper")
    with app.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM events")
        db.execute("DROP TRIGGER events_no_update")
        raw = json.loads(db.execute("SELECT payload FROM events").fetchone()[0])
        raw["summary"] = "forged"
        db.execute("UPDATE events SET payload=?", (json.dumps(raw),))
    with pytest.raises(PlatformError, match="INTEGRITY_FAILED"):
        app.get(task["id"])


def test_http_task_lifecycle_and_mode_isolation(tmp_path: Path) -> None:
    from http.client import HTTPConnection

    server = make_server(tmp_path, 0, embedded_scheduler=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def http(
        path: str, payload: dict[str, Any] | None = None, mode: str = "RESEARCH"
    ) -> tuple[int, dict[str, Any]]:
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            client.request(
                "GET" if payload is None else "POST",
                path,
                None if payload is None else json.dumps(payload),
                {
                    "Content-Type": "application/json",
                    "X-QF-Mode": mode,
                    "Idempotency-Key": "http-demo",
                },
            )
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    try:
        assert http("/api/tasks", request(), "SIMULATION")[0] == 403
        status, task = http("/api/tasks", request(max_experiments=2))
        assert status == 200
        task_path = "/api/tasks/" + task["id"]
        assert http(task_path + "/report")[0] == 409
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = http(task_path)[1]
            if result["status"] == "SUCCEEDED":
                break
            time.sleep(0.1)
        assert result["status"] == "SUCCEEDED"
        assert http("/api/tasks", request(max_experiments=2))[1]["id"] == task["id"]
        assert len(http("/api/tasks")[1]["items"]) == 1
        status, output = http(task_path + "/report")
        assert status == 200 and output["task_id"] == task["id"]
        assert output["report"]["evidence_refs"]
        assert http("/api/tasks/../../bindings")[0] == 404
        assert http(task_path + "/submit", {})[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
