from __future__ import annotations

import concurrent.futures
import http.client
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from test_agent_runtime import Planner, runtime

from qf_platform.agent_tasks import AgentTaskService
from qf_platform.agent_tools import AgentCoreBusy
from qf_platform.server import make_server
from qf_platform.storage import PlatformError


def request() -> dict[str, Any]:
    return {
        "mode": "REAL_RESEARCH",
        "dataset": "cn510300-exposed-raw-v1",
        "goal": "Compare two synthetic test fixtures only.",
        "max_experiments": 2,
        "max_model_calls": 1,
    }


def service(
    root: Path, provider: Planner | None = None, *, allow_test: bool = True
) -> tuple[AgentTaskService, Planner]:
    provider = Planner() if provider is None else provider
    source = root / "tool-runtime"
    source.mkdir()
    return AgentTaskService(
        root, runtime=runtime(source, provider), allow_test_provider=allow_test
    ), provider


def complete(app: AgentTaskService, id: str) -> dict[str, Any]:
    for _ in range(8):
        app.tick()
        value = app.get(id)
        if value["status"] in {"SUCCEEDED", "FAILED", "REFUSED"}:
            return value
    pytest.fail("Bounded task did not terminate")


def test_isolated_version_and_exact_model_and_core_recovery(tmp_path: Path) -> None:
    app, provider = service(tmp_path)
    created = app.create(request(), "create-once")
    assert (
        created["schema_version"] == "qf-agent-task-v2"
        and created["mode"] == "REAL_RESEARCH"
    )
    app.tick()
    app.tick()
    app.tick()
    before = app.get(created["id"])
    assert before["experiments_used"] == 1 and provider.count == 1
    app.control(created["id"], "cancel")
    assert not app.tick()
    recovered = AgentTaskService(
        tmp_path, runtime=app.runtime(), allow_test_provider=True
    )
    recovered.control(created["id"], "resume")
    done = complete(recovered, created["id"])
    assert done["status"] == "SUCCEEDED"
    assert (
        done["planner"] == "DETERMINISTIC_TEST_ONLY" and done["paid_model_calls"] == 0
    )
    assert done["experiments_used"] == 2 and provider.count == 1
    assert done["evidence"][:2] == before["evidence"]
    assert done["completed_steps"] == done["total_steps"] == 5
    assert len(done["report"]["rows"]) == 2
    assert set(done["report"]["evidence_refs"]) == {
        row["evidence_id"] for row in done["evidence"]
    }
    assert app.list()["schema_version"] == "qf-agent-task-list-v2"
    assert "paid_model_calls" not in app.list()
    assert app.create(request(), "create-once")["id"] == created["id"]


def test_real_mode_does_not_substitute_fake_planning(tmp_path: Path) -> None:
    app, _ = service(tmp_path, allow_test=False)
    task = app.create(request(), "no-fake")
    done = complete(app, task["id"])
    assert done["status"] == "FAILED" and done["experiments_used"] == 0
    assert not done["report"]


def test_unknown_call_survives_restart_and_is_never_repeated(tmp_path: Path) -> None:
    app, provider = service(tmp_path, Planner(failure=True))
    task = app.create(request(), "unknown-call")
    done = complete(app, task["id"])
    assert done["status"] == "FAILED" and done["model_call"]["status"] == "ERROR"
    assert provider.count == 1
    assert not app.tick()
    assert app.create(request(), "unknown-call")["id"] == task["id"]
    with pytest.raises(PlatformError):
        app.control(task["id"], "resume")
    assert provider.count == 1


def test_cancelling_during_paid_planning_keeps_known_response_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, provider = service(tmp_path)
    task = app.create(request(), "cancel-model")
    original = provider.complete_structured

    def cancel(*args: Any, **kwargs: Any) -> Any:
        app.control(task["id"], "cancel")
        return original(*args, **kwargs)

    monkeypatch.setattr(provider, "complete_structured", cancel)
    app.tick()
    cancelled = app.get(task["id"])
    assert cancelled["status"] == "CANCELLED" and cancelled["completed_steps"] == 1
    assert cancelled["model_outcome"] == "RESPONSE"
    app.control(task["id"], "resume")
    assert complete(app, task["id"])["status"] == "SUCCEEDED"
    assert provider.count == 1


def test_cancel_after_core_effect_does_not_create_a_second_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, provider = service(tmp_path)
    task = app.create(request(), "cancel-core")
    app.tick()
    app.tick()
    original = app.runtime().tools.run_experiment

    def cancel(*args: Any, **kwargs: Any) -> dict[str, Any]:
        value = original(*args, **kwargs)
        app.control(task["id"], "cancel")
        return value

    monkeypatch.setattr(app.runtime().tools, "run_experiment", cancel)
    app.tick()
    cancelled = app.get(task["id"])
    assert cancelled["status"] == "CANCELLED" and cancelled["experiments_used"] == 1
    assert (
        app.runtime().tools.inspect_dataset(request()["dataset"])[
            "core_trials_remaining"
        ]
        == 7
    )
    monkeypatch.setattr(app.runtime().tools, "run_experiment", original)
    app.control(task["id"], "resume")
    assert complete(app, task["id"])["experiments_used"] == 2
    assert (
        app.runtime().tools.inspect_dataset(request()["dataset"])[
            "core_trials_remaining"
        ]
        == 6
    )
    assert provider.count == 1


def test_double_create_is_idempotent_and_foreign_budget_or_authority_rejected(
    tmp_path: Path,
) -> None:
    app, _ = service(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        values = list(
            pool.map(lambda _: app.create(request(), "same-request"), range(2))
        )
    assert values[0]["id"] == values[1]["id"]
    for change in (
        {"mode": "LIVE"},
        {"dataset": "private/final"},
        {"max_experiments": True},
        {"max_model_calls": 2},
        {"metrics": {"total_return": 9}},
    ):
        with pytest.raises(ValueError):
            app.create({**request(), **change}, "invalid")


def test_v2_http_domain_is_explicit_and_keeps_demo_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, provider = service(tmp_path)
    monkeypatch.setattr(
        "qf_platform.server.AgentTaskService", lambda root, **options: app
    )
    server = make_server(tmp_path, 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:

        def call(
            path: str, payload: dict[str, Any] | None = None, *, mode: str = "RESEARCH"
        ) -> tuple[int, dict[str, Any]]:
            client = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=30
            )
            try:
                client.request(
                    "GET" if payload is None else "POST",
                    path,
                    None if payload is None else json.dumps(payload),
                    {
                        "Content-Type": "application/json",
                        "X-QF-Mode": mode,
                        "Idempotency-Key": "http-real-task",
                    },
                )
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()

        assert call("/api/agent-tasks", request(), mode="SIMULATION")[0] == 403
        status, created = call("/api/agent-tasks", request())
        assert status == 200 and created["schema_version"] == "qf-agent-task-v2"
        assert call("/api/agent-tasks", request())[1]["id"] == created["id"]
        assert call("/api/agent-tasks/capabilities")[1]["live_authority"] == "NONE"
        assert call("/api/agent-tasks")[1]["schema_version"] == "qf-agent-task-list-v2"
        assert call("/api/tasks")[1]["mode"] == "OFFLINE_DEMO"
        assert call("/api/tasks")[1]["paid_model_calls"] == 0
        assert call("/api/tasks", request())[0] == 409
        assert provider.count <= 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_admission_contention_is_deferred_without_research_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, provider = service(tmp_path)
    task = app.create(request(), "busy-core")
    app.tick()
    original = app.runtime().tools.inspect_dataset

    def busy(dataset: str) -> dict[str, Any]:
        raise AgentCoreBusy("AGENT_CORE_BUSY_NOT_DISPATCHED")

    monkeypatch.setattr(app.runtime().tools, "inspect_dataset", busy)
    app.tick()
    waiting = app.get(task["id"])
    assert waiting["status"] == "RUNNING" and waiting["completed_steps"] == 1
    assert waiting["events"][-1]["kind"] == "CORE_ADMISSION_DEFERRED"
    monkeypatch.setattr(app.runtime().tools, "inspect_dataset", original)
    assert complete(app, task["id"])["status"] == "SUCCEEDED"
    assert provider.count == 1


def test_failed_pure_check_recovers_only_confirmed_model_without_rebilling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, provider = service(tmp_path)
    task = app.create(request(), "failed-pure-check")
    app.tick()
    original = app.runtime().tools.inspect_dataset

    def fail(dataset: str) -> dict[str, Any]:
        raise OSError("controlled pure metadata failure")

    monkeypatch.setattr(app.runtime().tools, "inspect_dataset", fail)
    app.tick()
    assert app.get(task["id"])["status"] == "FAILED"
    monkeypatch.setattr(app.runtime().tools, "inspect_dataset", original)
    app.control(task["id"], "recover")
    assert complete(app, task["id"])["status"] == "SUCCEEDED"
    assert provider.count == 1
