import copy
import json
from datetime import date, datetime

import pytest
from test_chart_algorithms import daily_rows, raw
from test_chart_analysis import fixture_report, image_payload

from qf_platform import chart_algorithms as algorithms
from qf_platform import chart_provider
from qf_platform.chart_analysis import ChartAnalysisService
from qf_platform.chart_channels import short_reply
from qf_platform.chart_conclusions import conclusion
from qf_platform.chart_market_cache import cached_fetch
from qf_platform.chart_reports import markdown
from qf_platform.storage import PlatformError


def test_context_cannot_read_today_daily_close_from_a_historical_hourly_cutoff(
    tmp_path, monkeypatch
):
    seen = []

    def period(code, start, cutoff, kind):
        seen.append((kind, cutoff.isoformat()))
        if kind == "1w":
            raise OSError("explicit missing weekly data")
        rows = [["202610161030", "100", "101", "102", "99", "1000"]]
        return json.dumps(
            {"code": 0, "data": {code: {"m60": rows}}}
        ).encode(), "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519"

    def daily(code, start, end):
        seen.append(("1d", end.isoformat()))
        raise OSError("explicit missing daily data")

    monkeypatch.setattr(algorithms, "fetch_period", period)
    monkeypatch.setattr(algorithms, "fetch_daily", daily)
    result = algorithms.analyze(
        {
            "symbol": "600519",
            "timeframe": "60m",
            "period_start": "2026-10-16",
            "period_end": "2026-10-16",
            "period_end_time": "10:45",
        },
        tmp_path,
        as_of=datetime.fromisoformat("2026-10-17T12:00:00+08:00"),
    )
    assert result["status"] == "AVAILABLE"
    assert ("1d", "2026-10-15") in seen
    weekly = next(x for x in seen if x[0] == "1w")
    assert weekly[1].startswith("2026-10-09")
    assert result["cross_timeframe"]["status"] == "MISSING"
    assert result["synthesis"]["direction"] == "UNCLEAR"


def test_cache_reuses_and_rejects_corrupted_original_without_refetching(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return b'{"software":"fixture"}', "https://example.invalid/frozen-fixture"

    first = cached_fetch(tmp_path, {"timeframe": "1d"}, fetch)
    second = cached_fetch(tmp_path, {"timeframe": "1d"}, fetch)
    assert first[:3] == second[:3] and not first[3] and second[3] and calls == [1]
    artifact = next(
        x for x in tmp_path.glob("*.json") if not x.name.startswith("query-")
    )
    artifact.write_bytes(b"changed")
    with pytest.raises(ValueError, match="摘要"):
        cached_fetch(tmp_path, {"timeframe": "1d"}, fetch)
    assert calls == [1]


def test_final_conclusion_is_backend_owned_even_when_model_claims_opposite(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        algorithms,
        "fetch_daily",
        lambda *args: (
            raw(daily_rows()),
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519",
        ),
    )

    def unavailable(*args):
        raise OSError("missing weekly fixture")

    monkeypatch.setattr(algorithms, "fetch_period", unavailable)
    model_report = fixture_report(
        recognized_symbol="600519",
        recognized_timeframe="1d",
        recognized_adjustment="RAW",
        recognized_period="2026-08-15 至 2026-09-08",
        outlook="BEARISH",
        summary="模型原始叙述夹具，不能覆盖后端综合结论。",
    )
    monkeypatch.setattr(
        chart_provider,
        "analyze",
        lambda *args: (model_report, {"prompt_tokens": 1, "completion_tokens": 1}),
    )
    app = ChartAnalysisService(tmp_path)
    app.configure({"api_key": "fixture-only-key", "max_cost_cny": "10.00"})
    job = app.create(
        image_payload(
            symbol="600519",
            period_start="2026-08-15",
            period_end="2026-09-08",
            purchase_price="180",
            horizon_bars=1,
            adjustment="RAW",
        ),
        "synthesis-fixture",
    )
    assert app.tick()
    job = app.get(job["id"])
    combined = job["conclusion"]
    assert (
        combined == conclusion(job)
        and combined["direction"] == job["algorithm_analysis"]["synthesis"]["direction"]
    )
    assert combined["model_commentary_status"] == "ARCHIVED_ONLY"
    assert model_report.summary not in combined["headline"]
    assert combined["headline"] in markdown(job) and combined[
        "headline"
    ] in short_reply(job)
    old_bytes = copy.deepcopy(job)
    linked = app.create(
        image_payload(
            symbol="600519",
            period_start="2026-08-15",
            period_end="2026-09-08",
            purchase_price="180",
            horizon_bars=1,
            adjustment="RAW",
            position_reference_id=job["id"],
        ),
        "linked-fixture",
    )
    assert linked["request"]["position_reference_id"] == job["id"]
    assert app.tick()
    linked = app.get(linked["id"])
    assert (
        linked["algorithm_analysis"]["trade_plan"]["levels"]
        == job["algorithm_analysis"]["trade_plan"]["levels"]
    )
    assert app.get(job["id"]) == old_bytes
    with pytest.raises(PlatformError, match="POSITION_REFERENCE"):
        app.create(
            image_payload(
                symbol="600519",
                period_start="2026-08-15",
                period_end="2026-09-08",
                purchase_price="181",
                horizon_bars=1,
                adjustment="RAW",
                position_reference_id=job["id"],
            ),
            "invalid-linked",
        )
    facts = chart_provider.model_facts(job["algorithm_analysis"])
    assert "strategies" not in facts and len(facts["synthesis"]["evidence"]) == 8
    assert app.status()["calls_used"] == 2
    # Follow-up uses public bars only, never mutates original jobs or model usage.
    from datetime import UTC, datetime

    from qf_platform import chart_followup

    before = copy.deepcopy(app.get(job["id"]))
    budget = app.status()
    calls = []

    def future(*args):
        calls.append(1)
        count = (datetime.now(UTC).date() - date(2026, 6, 1)).days
        return raw(
            [
                row
                for row in daily_rows(count)
                if args[1].isoformat() <= row[0] <= args[2].isoformat()
            ]
        ), "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519"

    monkeypatch.setattr(chart_followup, "fetch_daily", future)
    observed = chart_followup.observe(job, app.market_data)
    assert observed["status"] == "MATURED" and observed["kind"] == "RETROSPECTIVE"
    assert chart_followup.observe(job, app.market_data) == observed and calls == [1]
    assert app.get(job["id"]) == before and app.status() == budget
    contradictory = copy.deepcopy(job)
    contradictory["report"]["recognized_period"] = "2024-01-01 至 2024-01-31"
    rejected = conclusion(contradictory)
    assert rejected["status"] == "NEEDS_REVIEW" and rejected["direction"] == "UNCLEAR"
    assert rejected["levels"] is None and rejected["scenarios"] == []


def test_context_raw_artifact_integrity_is_checked_together_with_primary(
    tmp_path, monkeypatch
):
    from datetime import timedelta

    monkeypatch.setattr(
        algorithms,
        "fetch_daily",
        lambda *args: (
            raw(daily_rows()),
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519",
        ),
    )
    rows = [
        [
            (date(2026, 9, 4) - timedelta(weeks=99 - i)).isoformat(),
            "100",
            "101",
            "102",
            "99",
            "1000",
        ]
        for i in range(100)
    ]
    weekly = json.dumps({"code": 0, "data": {"sh600519": {"week": rows}}}).encode()
    monkeypatch.setattr(
        algorithms,
        "fetch_period",
        lambda *args: (
            weekly,
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,week",
        ),
    )
    monkeypatch.setattr(
        chart_provider,
        "analyze",
        lambda *args: (fixture_report(), {"prompt_tokens": 1, "completion_tokens": 1}),
    )
    app = ChartAnalysisService(tmp_path)
    app.configure({"api_key": "fixture-only-key", "max_cost_cny": "10.00"})
    job = app.create(
        image_payload(
            symbol="600519", period_start="2026-08-15", period_end="2026-09-08"
        ),
        "context-integrity",
    )
    assert app.tick()
    job = app.get(job["id"])
    context = job["algorithm_analysis"]["cross_timeframe"]["items"][0]
    assert context["status"] == "AVAILABLE"
    (app.market_data / context["source"]["artifact"]).write_bytes(
        b"corrupted weekly source"
    )
    with pytest.raises(PlatformError, match="MARKET_DATA_INTEGRITY"):
        app.get(job["id"])
