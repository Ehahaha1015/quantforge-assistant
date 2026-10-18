import copy
import json
import math
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from quantforge.strategies.chart_quality import is_session
from test_chart_analysis import fixture_report, image_payload

from qf_platform import chart_algorithms, chart_provider
from qf_platform.chart_analysis import ChartAnalysisService
from qf_platform.chart_reports import ChartRequest, assess_alignment
from qf_platform.storage import PlatformError


def test_daily_five_report_shares_plan_replay_and_keeps_linked_prices(
    tmp_path, monkeypatch
):
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(251)]
    days = [day for day in days if is_session(day)]

    def fetch(code, start, end):
        rows = []
        for index, day in enumerate(days):
            value = 100 + math.sin(index / 4)
            if start <= day <= end:
                rows.append(
                    [
                        day.isoformat(),
                        str(value),
                        str(value),
                        str(value + 1),
                        str(value - 1),
                        "1000",
                    ]
                )
        return json.dumps(
            {"code": 0, "data": {code: {"day": rows}}}
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
    payload = image_payload(
        symbol="600519",
        period_start="2026-08-15",
        period_end="2026-09-08",
        adjustment="RAW",
        include_context=False,
        purchase_price="100",
        max_holding_bars=10,
    )
    created = app.create(payload, "daily-five")
    assert app.tick()
    first = app.get(created["id"])
    algo = first["algorithm_analysis"]
    assert (
        first["status"] == "SUCCEEDED"
        and first["image_alignment"]["status"] == "CONSISTENT"
    )
    assert algo["daily_research"]["release_status"] == "NOT_PROMOTED"
    assert Decimal(algo["daily_research"]["features"]["relative_return5"]) == 0
    assert algo["quality"]["calendar_verified"]
    plan = algo["trade_plan"]
    assert (
        plan["version"] == "conditional-entry-exit-v3"
        and plan["forecast_horizon_bars"] == 5
    )
    assert plan["max_observation_bars"] == 10 and plan["review_interval_bars"] == 1
    assert plan["levels"] is not None
    replay = algo["report_plan_replay"]
    assert [x["friction_bps_each_side"] for x in replay["summaries"]] == [10, 30]
    source = next(
        x
        for x in algo["research_sources"]
        if x.get("provider") == "DETERMINISTIC_REPORT_PLAN_REPLAY"
    )
    retained = json.loads((app.market_data / source["artifact"]).read_bytes())
    assert retained["execution_qualified"] is False and retained["events"]
    original = copy.deepcopy(first)
    linked = app.create(
        {**payload, "held_bars": 7, "position_reference_id": first["id"]},
        "linked-daily-five",
    )
    assert app.tick()
    linked = app.get(linked["id"])
    assert linked["algorithm_analysis"]["trade_plan"]["levels"] == plan["levels"]
    assert linked["algorithm_analysis"]["trade_plan"]["remaining_observation_bars"] == 3
    assert app.get(first["id"]) == original
    (app.market_data / source["artifact"]).write_bytes(b"tampered replay")
    with pytest.raises(PlatformError, match="MARKET_DATA_INTEGRITY"):
        app.get(first["id"])


def test_adjustment_requires_both_user_metadata_and_image_and_keeps_legacy_gate():
    metadata = {
        "symbol": "600519",
        "timeframe": "1d",
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
    }
    report = fixture_report(
        recognized_symbol="600519",
        recognized_timeframe="1d",
        recognized_period="2026-09-01 至 2026-09-30",
        recognized_adjustment="RAW",
    )
    assert assess_alignment(metadata, report)["status"] == "CONSISTENT"
    for user, image, expected in [
        ("RAW", "RAW", "CONSISTENT"),
        ("UNKNOWN", "RAW", "UNCONFIRMED"),
        ("RAW", None, "UNCONFIRMED"),
        ("QFQ", "QFQ", "MISMATCH"),
    ]:
        changed = report.model_copy(update={"recognized_adjustment": image})
        assert (
            assess_alignment({**metadata, "adjustment": user}, changed)["status"]
            == expected
        )
    for timeframe in ("60m", "1w"):
        with pytest.raises(ValidationError):
            ChartRequest.model_validate(
                image_payload(timeframe=timeframe, max_holding_bars=10)
            )


@pytest.mark.parametrize(
    "error_code",
    [
        "CHART_PROVIDER_INVALID_REPORT",
        "CHART_PROVIDER_REJECTED",
        "CHART_PROVIDER_RESPONSE_TOO_LARGE",
    ],
)
def test_failed_provider_diagnosis_keeps_code_and_reserve_without_retry(
    tmp_path, monkeypatch, error_code
):
    monkeypatch.setattr(
        chart_algorithms,
        "analyze",
        lambda *args, **kwargs: {
            "status": "UNAVAILABLE",
            "reason": "explicit software fixture",
        },
    )
    calls = []

    def rejected(*args):
        calls.append(1)
        raise PlatformError(error_code)

    monkeypatch.setattr(chart_provider, "analyze", rejected)
    app = ChartAnalysisService(tmp_path)
    app.configure({"api_key": "fixture-only-key", "max_cost_cny": "10.00"})
    job = app.create(image_payload(), "known-failure")
    assert app.tick()
    result = app.get(job["id"])
    assert result["status"] == "FAILED" and result["error"] == error_code
    assert result["failure_stage"] == "MODEL_REQUEST_OR_RESPONSE"
    before = app.status()
    assert not app.tick() and app.status() == before and calls == [1]
    assert before["calls_used"] == 1 and Decimal(before["reserved_cny"]) > 0


def test_failed_raw_provider_response_is_archived_privately_without_retry(
    tmp_path, monkeypatch
):
    from test_chart_provider import response_payload

    monkeypatch.setattr(
        chart_algorithms,
        "analyze",
        lambda *args, **kwargs: {"status": "UNAVAILABLE", "reason": "software fixture"},
    )
    value = response_payload()
    value["choices"][0]["message"]["content"] = '{"private_bad_response":'
    raw = json.dumps(value).encode()
    calls = []

    def malformed(*args):
        calls.append(1)
        return chart_provider.parse_response(raw)

    monkeypatch.setattr(chart_provider, "analyze", malformed)
    app = ChartAnalysisService(tmp_path)
    app.configure({"api_key": "fixture-only-key", "max_cost_cny": "10.00"})
    created = app.create(image_payload(), "diagnostic-once")
    assert app.tick()
    job = app.get(created["id"])
    diagnostic = job["provider_diagnostics"]
    assert (
        diagnostic["reason"] == "CONTENT_JSON_INVALID"
        and diagnostic["archive_status"] == "SAVED"
    )
    archived = app.root / "provider-responses" / diagnostic["response_artifact"]
    assert archived.read_bytes() == raw and "private_bad_response" not in json.dumps(
        job
    )
    before = app.status()
    assert not app.tick() and app.status() == before and calls == [1]
    assert app.get(job["id"]) == job
