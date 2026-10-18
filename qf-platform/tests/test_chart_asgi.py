import json
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qf_platform.chart_api import create_app
from qf_platform.chart_demo import OfflineChartRuntime, prepare_demo, sample_request
from qf_platform.chart_evaluation import evaluate
from qf_platform.storage import PlatformError


def test_contracts_modes_schema_and_redacted_events(tmp_path: Path) -> None:
    app = create_app(tmp_path, port=4192)
    with TestClient(app, base_url="http://127.0.0.1:4192") as client:
        body = sample_request()
        assert client.post("/api/chart-analyses", json=body).status_code == 403
        headers = {"X-QF-Mode": "RESEARCH", "Idempotency-Key": "same"}
        first = client.post("/api/chart-analyses", json=body, headers=headers)
        assert first.status_code == 200
        value = first.json()
        assert value["status"] == "WAITING_FOR_MODEL"
        assert (
            client.post("/api/chart-analyses", json=body, headers=headers).json()
            == value
        )
        assert (
            client.post(
                "/api/chart-analyses",
                json={**body, "symbol": "000001"},
                headers=headers,
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/chart-analyses",
                json={**body, "channel": "QQ"},
                headers={**headers, "Idempotency-Key": "qq"},
            ).json()["error"]
            == "CHART_CHANNEL_NOT_CONFIGURED"
        )
        assert client.get("/api/chart-analyses/chart-bad").status_code == 404
        events = client.get(f"/api/chart-analyses/{value['id']}/events").json()
        assert events["items"][0]["kind"] == "SCREENSHOT_RECEIVED"
        assert "image_base64" not in json.dumps(
            events
        ) and "snapshot" not in json.dumps(events)
        spec = client.get("/api/openapi.json").json()
        assert "ChartRequest" in spec["components"]["schemas"]
        assert "/api/tasks" not in spec["paths"]
        assert client.get("/api/tasks").status_code == 404
        assert client.post("/api/tasks", json={}).status_code == 403
        assert (
            client.get(
                "/api/health", headers={"Origin": "https://foreign.example"}
            ).status_code
            == 403
        )
        assert (
            client.get("/api/health", headers={"Host": "foreign.example"}).status_code
            == 403
        )
        assert (
            first.headers["X-Request-Id"]
            and first.headers["Cache-Control"] == "no-store"
        )
        invalid = client.post(
            "/api/chart-settings",
            json={"api_key": "private-secret", "max_cost_cny": 10},
            headers={"X-QF-Mode": "CONFIGURATION"},
        )
        assert invalid.status_code == 409 and "private-secret" not in invalid.text
        assert (
            client.post(
                "/api/chart-analyses",
                content="{}",
                headers={**headers, "Content-Type": "text/plain"},
            ).status_code
            == 409
        )


def test_offline_same_service_success_refusal_invalid_unknown_and_restart(
    tmp_path: Path,
) -> None:
    ids = prepare_demo(tmp_path)
    app = create_app(
        tmp_path, port=4192, runtime=OfflineChartRuntime(tmp_path), demo=True
    )
    charts = app.state.application.charts
    for _ in range(4):
        assert charts.tick()
    assert [charts.get(i)["status"] for i in ids] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
        "UNKNOWN",
    ]
    assert charts.get(ids[0])["conclusion"]["status"] == "READY"
    assert charts.get(ids[1])["conclusion"]["status"] == "NEEDS_REVIEW"
    assert not charts.tick()
    assert len((tmp_path / "offline-calls.jsonl").read_text().splitlines()) == 4
    assert prepare_demo(tmp_path) == ids
    with TestClient(app, base_url="http://127.0.0.1:4192") as client:
        assert client.get("/api/health").json()["demo"]
        assert (
            client.post(
                f"/api/chart-analyses/{ids[0]}/review",
                json={"action": "review"},
                headers={"X-QF-Mode": "RESEARCH"},
            ).json()["error"]
            == "DEMO_EXTERNAL_DATA_DISABLED"
        )
        assert client.get(f"/api/chart-analyses/{ids[0]}/report.md").status_code == 200
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    (ordinary / "original-data").write_text("keep")
    with pytest.raises(PlatformError, match="DEDICATED_ROOT"):
        prepare_demo(ordinary)
    assert (ordinary / "original-data").read_text() == "keep"


def test_declared_contract_corpus_has_no_false_accepts() -> None:
    result = evaluate()
    assert result["passed"] and result["cases"] == 25
    assert result["false_accepts"] == 0 and result["real_model_calls"] == 0
    assert result["evidence"] == "SYNTHETIC_OUTPUT_CONTRACTS_NOT_MODEL_QUALITY"


def test_budget_boundary_with_competing_workers_preserves_one_reservation(
    tmp_path: Path,
) -> None:
    from qf_platform.chart_analysis import ChartAnalysisService
    from qf_platform.chart_demo import FIXTURE_KEY, check_demo_root

    probe = tmp_path / "probe"
    check_demo_root(probe, initialize=True)
    baseline = ChartAnalysisService(probe, runtime=OfflineChartRuntime(probe))
    baseline.configure({"api_key": FIXTURE_KEY, "max_cost_cny": "10.00"})
    baseline.create(sample_request(), "probe")
    baseline.tick()
    reserve = Decimal(baseline.status()["reserved_cny"])
    cap = reserve.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    assert reserve <= cap < 2 * reserve
    root = tmp_path / "competing"
    check_demo_root(root, initialize=True)
    first = ChartAnalysisService(root, runtime=OfflineChartRuntime(root))
    second = ChartAnalysisService(root, runtime=OfflineChartRuntime(root))
    first.configure({"api_key": FIXTURE_KEY, "max_cost_cny": str(cap)})
    first.create(sample_request(), "one")
    second.create(sample_request(), "two")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda app: app.tick(), [first, second]))
    first.tick()
    second.tick()
    assert sorted(v["status"] for v in first.list()["items"]) == [
        "SUCCEEDED",
        "WAITING_FOR_MODEL",
    ]
    waiting = next(
        v for v in first.list()["items"] if v["status"] == "WAITING_FOR_MODEL"
    )
    assert first.get(waiting["id"])["error"] == "CHART_BUDGET_EXHAUSTED"
    assert first.status()["calls_used"] == 1
    assert Decimal(first.status()["reserved_cny"]) <= cap
    assert len((root / "offline-calls.jsonl").read_text().splitlines()) == 1


def test_transport_limits_and_duplicate_control_headers_create_no_tasks(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path, port=4192)
    with TestClient(app, base_url="http://127.0.0.1:4192") as client:
        headers = {
            "X-QF-Mode": "RESEARCH",
            "Idempotency-Key": "bounded",
            "Content-Type": "application/json",
        }
        assert (
            client.post(
                "/api/chart-analyses",
                content="{}",
                headers={**headers, "Content-Length": "9000000"},
            ).json()["error"]
            == "BOUNDED_JSON_REQUIRED"
        )
        duplicated = client.post(
            "/api/chart-analyses",
            content="{}",
            headers=[
                *headers.items(),
                ("Host", "127.0.0.1:4192"),
                ("Host", "foreign.example"),
            ],
        )
        assert duplicated.status_code == 409
        assert duplicated.json()["error"] == "DUPLICATE_CONTROL_HEADER"
        assert client.get("/api/chart-analyses").json()["items"] == []
