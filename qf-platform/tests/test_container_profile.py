import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from qf_platform.server import make_server
from qf_platform.storage import PlatformError


@pytest.fixture
def container_api(tmp_path: Path) -> Iterator[int]:
    server = make_server(tmp_path, 0, host="0.0.0.0", offline_demo=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(
    port: int, path: str, payload: Any = None, **headers: str
) -> tuple[int, Any]:
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        client.request(
            "GET" if payload is None else "POST",
            path,
            None if payload is None else json.dumps(payload),
            {"Content-Type": "application/json", "X-QF-Mode": "RESEARCH", **headers},
        )
        response = client.getresponse()
        return response.status, json.loads(response.read())
    finally:
        client.close()


def test_container_listen_preserves_host_and_origin_guards(container_api: int) -> None:
    port = container_api
    status, value = request(port, "/api/health")
    assert status == 200 and value["deployment_profile"] == "OFFLINE_DEMO"
    assert value["agent_task_mode"] == "DISABLED"
    assert request(port, "/api/health", Host="foreign.example")[0] == 403
    assert request(port, "/api/health", Origin="https://foreign.example")[0] == 403
    assert request(port, "/api/health", Origin="http://127.0.0.1:4180")[0] == 200


@pytest.mark.parametrize(
    ("path", "mode"),
    [
        ("/api/agent-tasks", "RESEARCH"),
        ("/api/room/start", "RESEARCH"),
        ("/api/bindings/save", "CONFIGURATION"),
        ("/api/brokers/query", "OPERATIONS"),
        ("/api/autonomy", "SIMULATION"),
    ],
)
def test_offline_profile_denies_other_mutations(
    container_api: int,
    path: str,
    mode: str,
) -> None:
    status, body = request(container_api, path, {}, **{"X-QF-Mode": mode})
    assert status == 403 and body == {"error": "OFFLINE_DEMO_TASKS_ONLY"}


def test_offline_profile_can_create_cancel_and_resume(container_api: int) -> None:
    payload = {
        "mode": "OFFLINE_DEMO",
        "dataset": "synthetic-regimes-v1",
        "goal": "Actual container profile task lifecycle regression.",
        "max_experiments": 2,
        "analysis": "cost_sensitivity",
    }
    status, task = request(
        container_api, "/api/tasks", payload, **{"Idempotency-Key": "container-profile"}
    )
    assert status == 200 and task["paid_model_calls"] == 0
    assert (
        request(
            container_api, f"/api/tasks/{task['id']}/control", {"action": "cancel"}
        )[1]["status"]
        == "CANCELLED"
    )
    assert (
        request(
            container_api, f"/api/tasks/{task['id']}/control", {"action": "resume"}
        )[1]["status"]
        == "PENDING"
    )


def test_unrecognized_listen_host_is_rejected_before_initialization(
    tmp_path: Path,
) -> None:
    with pytest.raises(PlatformError, match="SERVER_LISTEN_HOST_INVALID"):
        make_server(tmp_path / "unused", host="public.example")
    assert not (tmp_path / "unused").exists()
