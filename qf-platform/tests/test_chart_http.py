import base64
import io
import json
import os
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from qf_platform.chart_channels import IncomingChartImage
from qf_platform.server import make_server


@pytest.fixture
def api(tmp_path: Path) -> Iterator[int]:
    server = make_server(tmp_path, 0, offline_demo=True, chart_assistant=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def http(
    port: int, path: str, payload: Any = None, mode: str = "RESEARCH", **extra: str
) -> tuple[int, bytes, str]:
    client = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        client.request(
            "GET" if payload is None else "POST",
            path,
            json.dumps(payload) if payload is not None else None,
            {
                "Content-Type": "application/json",
                "X-QF-Mode": mode,
                "Origin": "http://127.0.0.1:4180",
                **extra,
            },
        )
        response = client.getresponse()
        return response.status, response.read(), response.getheader("Content-Type", "")
    finally:
        client.close()


def payload() -> dict[str, Any]:
    stream = io.BytesIO()
    Image.frombytes("RGB", (400, 300), os.urandom(400 * 300 * 3)).save(stream, "PNG")
    return {
        "symbol": "TEST",
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "image_base64": base64.b64encode(stream.getvalue()).decode(),
    }


def test_large_valid_upload_and_original_bytes_without_model_configuration(
    api: int,
) -> None:
    value = payload()
    assert len(json.dumps(value)) > 65536
    status, raw, _ = http(
        api, "/api/chart-analyses", value, **{"Idempotency-Key": "http-upload"}
    )
    created = json.loads(raw)
    assert (
        status == 200
        and created["status"] == "WAITING_FOR_MODEL"
        and created["report"] is None
    )
    path = "/api/chart-analyses/" + created["id"]
    status, data, mime = http(api, path + "/image")
    assert (
        status == 200
        and data == base64.b64decode(value["image_base64"])
        and mime == "image/png"
    )
    assert http(api, path + "/report.md")[0] == 409
    assert http(api, path, Host="foreign.example")[0] == 403


def test_settings_modes_and_no_secret_echo(api: int) -> None:
    value = {"api_key": "fixture-only-chart-key", "max_cost_cny": "0.00"}
    assert http(api, "/api/chart-settings", value)[0] == 403
    status, raw, _ = http(api, "/api/chart-settings", value, "CONFIGURATION")
    assert status == 200 and b"fixture-only-chart-key" not in raw
    settings = json.loads(raw)
    assert settings["configured"] and not settings["ready"]
    assert "secret" not in settings and "api_key" not in settings
    assert (
        http(
            api,
            "/api/chart-settings",
            value,
            "CONFIGURATION",
            Origin="https://foreign.example",
        )[0]
        == 403
    )
    assert http(api, "/api/agent-tasks", {})[0] == 403


def test_web_cannot_spoof_a_qq_message(api: int) -> None:
    status, raw, _ = http(
        api,
        "/api/chart-analyses",
        {**payload(), "channel": "QQ"},
        **{"Idempotency-Key": "forged-qq"},
    )
    assert status == 409 and json.loads(raw)["error"] == "CHART_CHANNEL_NOT_CONFIGURED"


def test_qq_deduplication_scopes_messages_to_sender() -> None:
    first = IncomingChartImage("QQ", "sender-1", "message-1", b"fixture")
    duplicate = IncomingChartImage("QQ", "sender-1", "message-1", b"fixture")
    other = IncomingChartImage("QQ", "sender-2", "message-1", b"fixture")
    assert first.request_key() == duplicate.request_key() != other.request_key()
    assert "sender" not in first.request_key()


def test_chart_only_profile_removes_legacy_routes(tmp_path: Path) -> None:
    server = make_server(tmp_path, 0, chart_assistant=True, chart_only=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in ("/api/tasks", "/api/bindings", "/api/state"):
            assert http(server.server_port, path)[0] == 404
            for body in ({}, {"retired": "x" * 4096}, {}):
                assert http(server.server_port, path, body)[0] == 403
        status, raw, _ = http(server.server_port, "/api/health")
        health = json.loads(raw)
        assert status == 200
        assert health["schema_version"] == "qf-health-v1"
        assert health["service"] == "quantforge-chart-assistant"
        assert health["task_mode"] == "CHART_ONLY"
        assert health["agent_task_mode"] == "DISABLED"
        assert health["deployment_profile"] == "SCREENSHOT_ASSISTANT"
        assert http(server.server_port, "/api/chart-settings")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_absent_or_malformed_job_id_is_not_found(api: int) -> None:
    missing = "chart-" + "0" * 32
    for job_id in (missing, "chart-doesnotexist"):
        status, raw, _ = http(api, "/api/chart-analyses/" + job_id)
        assert status == 404
        assert json.loads(raw)["error"] in {"CHART_NOT_FOUND", "CHART_ID_INVALID"}
        assert http(api, f"/api/chart-analyses/{job_id}/image")[0] == 404
        assert http(api, f"/api/chart-analyses/{job_id}/report.md")[0] == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"period_start": "2026-02-30"},
        {"timeframe": "bad"},
        {"held_bars": 1},
        {"period_end_time": "25:00"},
    ],
)
def test_invalid_request_has_definite_validation_error_and_creates_no_job(api, changes):
    status, raw, _ = http(
        api,
        "/api/chart-analyses",
        {**payload(), **changes},
        **{"Idempotency-Key": "invalid-request"},
    )
    assert status == 409 and json.loads(raw)["error"] == "CHART_REQUEST_INVALID"
    _, raw, _ = http(api, "/api/chart-analyses")
    assert json.loads(raw)["items"] == []
