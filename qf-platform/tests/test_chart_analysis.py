import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from qf_platform import chart_provider
from qf_platform.chart_analysis import ChartAnalysisService
from qf_platform.chart_reports import ChartReport, markdown, validate_image
from qf_platform.storage import PlatformError


def image_payload(**changes: Any) -> dict[str, Any]:
    stream = io.BytesIO()
    Image.new("RGB", (400, 240), "#182b25").save(stream, format="PNG")
    return {
        "symbol": "TEST",
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "horizon_sessions": 5,
        "image_base64": base64.b64encode(stream.getvalue()).decode(),
        **changes,
    }


def fixture_report(**changes: Any) -> ChartReport:
    return ChartReport.model_validate(
        {
            "image_quality": "READABLE",
            "recognized_symbol": None,
            "recognized_period": None,
            "summary": "软件夹具报告，不代表真实图片分析。",
            "observed_trend": "UNCLEAR",
            "outlook": "UNCLEAR",
            "observations": [
                {"observation": "夹具中只保留占位观察。", "image_region": "图像区域"}
            ],
            "support_zones": [],
            "resistance_zones": [],
            "volume_comment": "夹具未提供成交量。",
            "scenarios": [
                {
                    "direction": direction,
                    "trigger": "仅在条件成立时。",
                    "interpretation": "软件夹具情景。",
                    "invalidation": "条件未满足时失效。",
                }
                for direction in ("BULLISH", "RANGE", "BEARISH")
            ],
            "uncertainties": ["这是明确的软件夹具，不是模型或市场证据。"],
            **changes,
        }
    )


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A test attempted an unconfigured real vision call")

    monkeypatch.setattr(chart_provider, "analyze", fail)


def configure(app: ChartAnalysisService, amount: str = "10.00") -> None:
    app.configure(
        {"api_key": "fixture-only-not-a-real-api-key", "max_cost_cny": amount}
    )


def test_unconfigured_upload_is_durable_and_never_fabricates_a_report(
    tmp_path: Path,
) -> None:
    app = ChartAnalysisService(tmp_path)
    payload = image_payload()
    created = app.create(payload, "same-upload")
    assert created["status"] == "WAITING_FOR_MODEL" and created["report"] is None
    assert app.create(payload, "same-upload")["id"] == created["id"]
    assert not app.tick() and app.status()["calls_used"] == 0
    restored = ChartAnalysisService(tmp_path)
    assert restored.get(created["id"]) == created
    assert restored.image(created["id"])[0] == base64.b64decode(payload["image_base64"])
    with pytest.raises(PlatformError, match="IDEMPOTENCY_CONFLICT"):
        app.create(image_payload(symbol="OTHER"), "same-upload")


def test_budget_refusal_before_send_then_explicit_start_preserves_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = ChartAnalysisService(tmp_path)
    configure(app, "0.00")
    created = app.create(image_payload(), "budget")
    assert app.tick()
    assert app.get(created["id"])["error"] == "CHART_BUDGET_EXHAUSTED"
    assert app.status()["calls_used"] == 0
    app.configure({"max_cost_cny": "10.00"})
    monkeypatch.setattr(
        chart_provider,
        "analyze",
        lambda *args: (
            fixture_report(),
            {"prompt_tokens": 100, "completion_tokens": 200},
        ),
    )
    app.start(created["id"])
    assert app.tick()
    completed = app.get(created["id"])
    assert completed["status"] == "SUCCEEDED" and not completed["forecast_validated"]
    assert completed["live_authority"] == "NONE"
    assert completed["model_call"]["model"] == "deepseek-flash"
    assert app.status()["calls_used"] == 1 and Decimal(app.status()["reserved_cny"]) > 0
    assert not app.tick()
    app.configure({"max_cost_cny": "20.00"})
    assert app.status()["calls_used"] == 1
    with pytest.raises(PlatformError, match="CANNOT_RESET"):
        app.configure({"max_cost_cny": "10.00"})
    text = markdown(completed)
    assert completed["image"]["sha256"] in text and "不构成投资建议" in text


def test_configuration_never_echoes_or_journals_a_plaintext_key(tmp_path: Path) -> None:
    app = ChartAnalysisService(tmp_path)
    configure(app)
    assert "api_key" not in app.status() and "secret" not in app.status()
    with app.connect() as db:
        text = str(db.execute("SELECT payload FROM settings").fetchall()) + str(
            db.execute("SELECT payload FROM events").fetchall()
        )
    assert "fixture-only-not-a-real-api-key" not in text


def test_unknown_call_cannot_be_replayed_or_recreated_with_a_new_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = ChartAnalysisService(tmp_path)
    configure(app)
    calls: list[int] = []

    def unknown(*args: Any) -> Any:
        calls.append(1)
        raise chart_provider.UnknownChartCall("CHART_PROVIDER_EFFECT_UNKNOWN")

    monkeypatch.setattr(chart_provider, "analyze", unknown)
    payload = image_payload()
    created = app.create(payload, "unknown")
    assert app.tick() and app.get(created["id"])["status"] == "UNKNOWN"
    assert not ChartAnalysisService(tmp_path).tick() and calls == [1]
    assert app.create(payload, "unknown")["status"] == "UNKNOWN"
    with pytest.raises(PlatformError, match="REQUIRES_REVIEW"):
        app.create(payload, "different-key")
    from qf_platform import chart_analysis

    monkeypatch.setattr(chart_analysis, "STRATEGY_VERSION", "future-version-fixture")
    with pytest.raises(PlatformError, match="REQUIRES_REVIEW"):
        app.create(
            {**payload, "question": "修改问题不自动重发同一未知图片。"}, "after-upgrade"
        )
    with pytest.raises(PlatformError, match="REQUIRES_WAITING"):
        app.start(created["id"])
    assert app.status()["calls_used"] == 1


def test_process_interruption_retains_reservation_without_repeating_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = ChartAnalysisService(tmp_path)
    configure(app)
    calls: list[int] = []

    def interrupted(*args: Any) -> Any:
        calls.append(1)
        raise KeyboardInterrupt

    monkeypatch.setattr(chart_provider, "analyze", interrupted)
    created = app.create(image_payload(), "crash")
    with pytest.raises(KeyboardInterrupt):
        app.tick()
    assert app.get(created["id"])["status"] == "RUNNING"
    recovered = ChartAnalysisService(tmp_path)
    assert recovered.tick() and recovered.get(created["id"])["status"] == "UNKNOWN"
    assert (
        not recovered.tick() and calls == [1] and recovered.status()["calls_used"] == 1
    )


def test_competing_workers_cannot_overrun_the_shared_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = ChartAnalysisService(tmp_path)
    configure(app, "0.15")
    app.create(image_payload(), "first")
    app.create(image_payload(symbol="SECOND"), "second")
    calls: list[int] = []

    def complete(*args: Any) -> Any:
        calls.append(1)
        return fixture_report(), {}

    monkeypatch.setattr(chart_provider, "analyze", complete)
    other = ChartAnalysisService(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda service: service.tick(), [app, other]))
    app.tick()
    assert calls == [1] and app.status()["calls_used"] == 1
    assert Decimal(app.status()["reserved_cny"]) <= Decimal("0.15")


@pytest.mark.parametrize("kind", ["projection", "event", "image"])
def test_corrupt_state_and_original_image_are_rejected(
    tmp_path: Path, kind: str
) -> None:
    app = ChartAnalysisService(tmp_path)
    created = app.create(image_payload(), "tamper")
    if kind == "image":
        path = app.images / (created["image"]["sha256"] + ".image")
        path.write_bytes(b"tampered")
        with pytest.raises(PlatformError, match="INTEGRITY"):
            app.image(created["id"])
    else:
        with app.connect() as db:
            if kind == "event":
                db.execute("DROP TRIGGER chart_events_no_update")
                db.execute("UPDATE events SET payload='{}'")
            else:
                value = dict(created, status="SUCCEEDED")
                db.execute("UPDATE jobs SET payload=?", (json.dumps(value),))
        with pytest.raises(PlatformError, match="INTEGRITY"):
            app.get(created["id"])


@pytest.mark.parametrize(
    "data", [b"<svg><script>bad</script></svg>" * 3, b"not-an-image" * 5]
)
def test_non_raster_inputs_are_refused(data: bytes) -> None:
    with pytest.raises(PlatformError):
        validate_image(base64.b64encode(data).decode())


def test_unreadable_chart_cannot_claim_a_forecast() -> None:
    with pytest.raises(ValueError, match="UNREADABLE_IMAGE_CANNOT_HAVE_OUTLOOK"):
        fixture_report(image_quality="UNREADABLE", outlook="BULLISH")
    rejected = fixture_report(image_quality="UNREADABLE", scenarios=[], observations=[])
    assert rejected.outlook == "UNCLEAR"
    with pytest.raises(ValueError):
        fixture_report(win_probability=0.99)
