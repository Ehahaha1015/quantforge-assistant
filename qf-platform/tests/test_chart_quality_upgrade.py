import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_chart_analysis import fixture_report, image_payload
from test_chart_synthesis import daily_rows

from qf_platform import chart_algorithms, chart_provider, chart_review
from qf_platform.chart_analysis import ChartAnalysisService
from qf_platform.chart_channels import short_reply
from qf_platform.chart_reports import markdown
from qf_platform.storage import PlatformError


def test_new_semantics_and_latest_review_preserve_original_and_budget(
    tmp_path, monkeypatch
):
    rows = daily_rows(110)

    def fetch(code, start, end):
        values = [r for r in rows if start.isoformat() <= r[0] <= end.isoformat()]
        return json.dumps(
            {"code": 0, "data": {code: {"day": values}}}
        ).encode(), "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + code

    monkeypatch.setattr(chart_algorithms, "fetch_daily", fetch)
    monkeypatch.setattr(
        chart_provider,
        "analyze",
        lambda *args: (
            fixture_report(
                recognized_symbol="600519",
                recognized_period="2026-08-15 至 2026-09-08",
                recognized_timeframe="1d",
                recognized_adjustment="RAW",
            ),
            {"prompt_tokens": 1, "completion_tokens": 1},
        ),
    )
    app = ChartAnalysisService(tmp_path)
    app.configure({"api_key": "fixture-only-key", "max_cost_cny": "10.00"})
    request = image_payload(
        symbol="600519",
        period_start="2026-08-15",
        period_end="2026-09-08",
        include_context=False,
        horizon_bars=1,
        adjustment="RAW",
        purchase_price="180",
    )
    created = app.create(request, "quality-report")
    assert app.tick()
    original = deepcopy(app.get(created["id"]))
    budget = app.status()
    combined = original["conclusion"]
    assert combined["schema_version"] == "chart-conclusion-v2"
    assert combined["uncertainty_note"] in markdown(original)
    assert combined["uncertainty_note"] in short_reply(original)
    assert "周期分歧本身不能证明" in combined["scenarios"][1]["interpretation"]
    monkeypatch.setattr(
        chart_provider, "analyze", lambda *args: pytest.fail("Review called model")
    )
    value = chart_review.review(
        original, app.market_data, now=datetime(2026, 9, 15, 9, tzinfo=UTC)
    )
    assert value["status"] in {"CHANGED", "UNCHANGED"}
    assert value["model_calls"] == 0
    assert value["held_bars"] == 7
    assert value["levels"] == original["algorithm_analysis"]["trade_plan"]["levels"]
    assert app.get(original["id"]) == original and app.status() == budget
    path = next((app.market_data / "reviews").glob("*.json"))
    archived = json.loads(path.read_bytes())
    archived["latest_algorithm"]["status"] = "corrupted"
    path.write_text(json.dumps(archived), encoding="utf-8")
    with pytest.raises(ValueError, match="archive"):
        chart_review.review(
            original, app.market_data, now=datetime(2026, 9, 15, 9, tzinfo=UTC)
        )
    bad = deepcopy(original)
    bad["status"] = "UNKNOWN"
    with pytest.raises(PlatformError, match="ALIGNED"):
        chart_review.review(bad, app.market_data)
    original["algorithm_analysis"]["synthesis"]["indicators"]["reference_close"] = "1"
    rejected = chart_review.review(
        original, app.market_data, now=datetime(2026, 9, 16, 9, tzinfo=UTC)
    )
    assert rejected["status"] == "DATA_UNAVAILABLE" and rejected["levels"] is None


def test_review_expected_bars_respect_session_clock_and_unknown_calendar():
    assert (
        chart_review.latest_expected(
            {"timeframe": "1d", "period_end": "2026-10-18"},
            datetime(2026, 10, 18, 9, tzinfo=UTC),
        )
        .date()
        .isoformat()
        == "2026-10-16"
    )
    hourly = chart_review.latest_expected(
        {"timeframe": "60m", "period_end": "2026-10-19", "period_end_time": "10:45"},
        datetime(2026, 10, 19, 2, 45, tzinfo=UTC),
    )
    assert hourly.strftime("%H:%M") == "10:30"
    assert (
        chart_review.latest_expected(
            {"timeframe": "1d", "period_end": "2027-01-04"},
            datetime(2027, 1, 4, 9, tzinfo=UTC),
        )
        is None
    )
