from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from qf_platform.chart_analysis import ChartAnalysisService
from qf_platform.chart_api import create_app
from qf_platform.chart_demo import (
    OfflineChartRuntime,
    check_demo_root,
    prepare_demo,
    sample_report,
    sample_request,
)
from qf_platform.chart_metadata import metadata, resolve
from qf_platform.chart_provider import UnknownChartCall
from qf_platform.chart_reports import markdown
from qf_platform.chart_visualization import visualization
from qf_platform.storage import PlatformError


def setup_auto(tmp_path, monkeypatch, report=None):
    check_demo_root(tmp_path, initialize=True)
    runtime = OfflineChartRuntime(tmp_path)
    events = []
    algorithm = runtime.algorithm

    def observe(key, data, mime, meta, facts):
        assert meta["input_mode"] == "AUTO" and meta["symbol"] == "待识别"
        assert facts is None
        events.append("MODEL")
        return report or sample_report(sample_request()), {
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }

    def calculate(meta, root, *, initial_levels):
        events.append("ALGORITHM")
        assert meta["symbol"] == "600519"
        return algorithm(meta, root, initial_levels=initial_levels)

    monkeypatch.setattr(runtime, "analyze", observe)
    monkeypatch.setattr(runtime, "algorithm", calculate)
    service = ChartAnalysisService(tmp_path, runtime=runtime)
    service.configure({"api_key": "fixture-only-not-real", "max_cost_cny": "10"})
    return service, runtime, events


def test_image_only_upload_resolves_metadata_with_one_existing_budgeted_call(
    tmp_path, monkeypatch
):
    service, runtime, events = setup_auto(tmp_path, monkeypatch)
    app = create_app(tmp_path, port=4192, runtime=runtime)
    with TestClient(app, base_url="http://127.0.0.1:4192") as client:
        payload = {"image_base64": sample_request()["image_base64"]}
        headers = {"X-QF-Mode": "RESEARCH", "Idempotency-Key": "image-only"}
        submitted = client.post("/api/chart-uploads", json=payload, headers=headers)
        assert submitted.status_code == 200
        saved = submitted.json()
        assert saved["request"]["symbol"] == "待识别"
        assert (
            client.post("/api/chart-uploads", json=payload, headers=headers).json()
            == saved
        )
        assert service.tick()
        completed = service.get(saved["id"])
        assert completed["status"] == "SUCCEEDED"
        assert (
            completed["request"] == saved["request"]
        )  # Original identity never rewritten.
        assert metadata(completed)["symbol"] == "600519"
        assert metadata(completed)["period_end"] == "2026-09-30"
        assert events == ["MODEL", "ALGORITHM"]
        assert service.status()["calls_used"] == 1
        assert completed["conclusion"]["status"] == "READY"
        assert visualization(completed, service.market_data).status == "AVAILABLE"
        assert "1970-01-01" not in markdown(completed)
        assert (
            client.get("/api/chart-analyses").json()["items"][0]["resolved_request"][
                "symbol"
            ]
            == "600519"
        )
        before = deepcopy(completed)
        assert (
            client.get(
                f"/api/chart-analyses/{saved['id']}/visualization?preview=true"
            ).json()["model_calls"]
            == 0
        )
        assert service.get(saved["id"]) == before


@pytest.mark.parametrize(
    "missing", ["recognized_period", "recognized_timeframe", "recognized_symbol"]
)
def test_partial_metadata_keeps_image_report_without_guessing_dates_or_period(
    tmp_path, monkeypatch, missing
):
    report = sample_report(sample_request()).model_copy(update={missing: None})
    service, runtime, events = setup_auto(tmp_path, monkeypatch, report)
    job = service.create(
        {
            **sample_request(),
            "input_mode": "AUTO",
            "symbol": "待识别",
            "period_start": "1970-01-01",
            "period_end": "1970-01-01",
        },
        "partial",
    )
    assert service.tick()
    saved = service.get(job["id"])
    assert saved["status"] == "SUCCEEDED" and saved["report"][missing] is None
    assert saved["resolved_request"] is None and saved["metadata_notes"]
    assert "1970-01-01" not in markdown(saved)
    assert events == ["MODEL"] and service.status()["calls_used"] == 1
    assert visualization(saved, service.market_data, preview=True).status == "BLOCKED"


def test_adjusted_image_can_show_independent_background_without_trade_levels(tmp_path):
    ids = prepare_demo(tmp_path)
    service = ChartAnalysisService(tmp_path, runtime=OfflineChartRuntime(tmp_path))
    while service.tick():
        pass
    original = service.get(ids[0])
    adjusted = deepcopy(original)
    adjusted["request"]["adjustment"] = "QFQ"
    adjusted["report"]["recognized_adjustment"] = "QFQ"
    assert visualization(adjusted, service.market_data).status == "BLOCKED"
    result = visualization(adjusted, service.market_data, preview=True)
    assert result.status == "AVAILABLE" and result.view_mode == "BACKGROUND"
    assert result.levels is None and result.direction == "UNCLEAR" and result.warnings
    assert result.model_calls == result.market_fetches == 0
    assert adjusted["request"]["adjustment"] == "QFQ"
    adjusted["report"]["recognized_symbol"] = "000001"
    assert (
        visualization(adjusted, service.market_data, preview=True).status == "BLOCKED"
    )


def test_unknown_automatic_call_preserves_reservation_and_never_replays(
    tmp_path, monkeypatch
):
    service, runtime, events = setup_auto(tmp_path, monkeypatch)

    def unknown(*args):
        raise UnknownChartCall("CHART_PROVIDER_EFFECT_UNKNOWN")

    monkeypatch.setattr(runtime, "analyze", unknown)
    payload = {**sample_request(), "input_mode": "AUTO"}
    job = service.create(payload, "unknown")
    assert service.tick()
    assert service.get(job["id"])["status"] == "UNKNOWN"
    assert service.status()["calls_used"] == 1 and not service.tick()
    with pytest.raises(PlatformError, match="PRIOR_CALL"):
        service.create(payload, "new-key")
    assert events == []


def test_hourly_missing_clock_never_defaults_to_close():
    snapshot = {"request": {"input_mode": "AUTO"}}
    report = sample_report(sample_request()).model_dump()
    report["recognized_timeframe"] = "60m"
    result, notes = resolve(snapshot, report)
    assert result is None and "15:00" in notes[0]
    report["recognized_timeframe"] = "1d"
    report["recognized_period"] = "1970-01-01 至 1970-01-01"
    result, notes = resolve(snapshot, report)
    assert result is None and "占位日期" in notes[0]
