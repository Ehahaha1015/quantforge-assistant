import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_chart_analysis import fixture_report, image_payload

from qf_platform import chart_algorithms as algorithms
from qf_platform import chart_provider
from qf_platform.chart_analysis import ChartAnalysisService
from qf_platform.chart_reports import markdown
from qf_platform.storage import PlatformError


@pytest.fixture(autouse=True)
def no_unconfigured_context_network(monkeypatch):
    def unavailable(*args):
        raise OSError("Unit tests require explicitly supplied context data")

    monkeypatch.setattr(algorithms, "fetch_period", unavailable)


def daily_rows(count: int = 100) -> list[list[str]]:
    start = date(2026, 6, 1)
    return [
        [
            (start + timedelta(days=index)).isoformat(),
            str(100 + index),
            str(101 + index),
            str(102 + index),
            str(99 + index),
            "1000",
        ]
        for index in range(count)
    ]


def raw(rows: list[list[str]]) -> bytes:
    return json.dumps({"code": 0, "data": {"sh600519": {"day": rows}}}).encode()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("600519", "sh600519"),
        ("000001", "sz000001"),
        ("510300.SH", "sh510300"),
        ("sz159915", "sz159915"),
    ],
)
def test_symbol_market_resolution(value: str, expected: str) -> None:
    assert algorithms.symbol_code(value) == expected


@pytest.mark.parametrize(
    "value",
    ["贵州茅台", "sh000001", "sz600519", "399001", "https://example.com/600519"],
)
def test_ambiguous_or_unsupported_identity_is_not_fetched(value: str) -> None:
    with pytest.raises(ValueError):
        algorithms.symbol_code(value)


def test_cutoff_corruption_and_adjusted_substitution_are_rejected() -> None:
    rows = daily_rows()
    start, cutoff = date(2026, 6, 1), date(2026, 9, 8)
    for bad in [
        rows + [rows[-1]],
        rows + [["2026-09-09", "1", "1", "1", "1", "1"]],
        [[*rows[0][:1], "nan", *rows[0][2:]], *rows[1:]],
    ]:
        with pytest.raises(ValueError):
            algorithms.parse_daily(raw(bad), "sh600519", start, cutoff)
    with pytest.raises(KeyError):
        algorithms.parse_daily(
            b'{"code":0,"data":{"sh600519":{"qfqday":[]}}}', "sh600519", start, cutoff
        )


def test_eight_actual_core_strategies_and_cost_stress_with_no_cold_start_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = algorithms.parse_daily(
        raw(daily_rows()), "sh600519", date(2026, 6, 1), date(2026, 9, 8)
    )
    original = algorithms.generate_chart_signals
    calls = []

    def observe(*args: Any) -> Any:
        calls.append(args[1].id)
        return original(*args)

    monkeypatch.setattr(algorithms, "generate_chart_signals", observe)
    result = algorithms.evaluate(frame, date(2026, 8, 15))
    assert len(set(calls)) == 8
    assert all(item["status"] == "COMPUTED" for item in result["strategies"])
    for item in result["strategies"]:
        low, high = item["replay"]
        assert high["price_return"] <= low["price_return"]
    cold = algorithms.evaluate(frame.head(10), date(2026, 6, 1))
    assert all(
        item["latest_state"] is None and item["replay"] == []
        for item in cold["strategies"]
    )


def test_data_unavailable_degrades_without_fabrication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: Any) -> Any:
        raise OSError("offline")

    monkeypatch.setattr(algorithms, "fetch_daily", unavailable)
    result = algorithms.analyze(
        {"symbol": "600519", "period_start": "2026-09-01", "period_end": "2026-09-30"},
        tmp_path,
    )
    assert result["status"] == "UNAVAILABLE" and result["source"] is None
    assert result["strategies"] == [] and not result["forecast_validated"]


def test_real_pipeline_passes_persisted_core_facts_to_model_and_checks_raw_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = raw(daily_rows())
    monkeypatch.setattr(
        algorithms,
        "fetch_daily",
        lambda *args: (
            data,
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519",
        ),
    )
    received = []

    def model(*args: Any) -> Any:
        received.append(args[4])
        assert args[4]["status"] == "AVAILABLE"
        assert len(args[4]["strategies"]) == 8
        return fixture_report(), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(chart_provider, "analyze", model)
    app = ChartAnalysisService(tmp_path)
    app.configure({"api_key": "fixture-only-model-key", "max_cost_cny": "10.00"})
    created = app.create(
        image_payload(
            symbol="600519", period_start="2026-08-15", period_end="2026-09-08"
        ),
        "core-integration",
    )
    assert app.tick()
    job = app.get(created["id"])
    assert job["status"] == "SUCCEEDED" and len(received) == 1
    assert job["algorithm_analysis"] == received[0]
    text = markdown(job)
    assert "内置算法分析" in text and "持有基线" in text and "价格回放" in text
    assert ChartAnalysisService(tmp_path).get(job["id"]) == job
    artifact = app.market_data / job["algorithm_analysis"]["source"]["artifact"]
    artifact.write_bytes(b"corrupted")
    with pytest.raises(PlatformError, match="MARKET_DATA_INTEGRITY"):
        app.get(job["id"])


def test_today_is_excluded_before_daily_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 9, 9, 6, tzinfo=UTC).astimezone(tz)

    calls = []

    def fetch(code: str, start: date, end: date) -> tuple[bytes, str]:
        calls.append(end)
        return raw(
            daily_rows()
        ), "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519"

    monkeypatch.setattr(algorithms, "datetime", Clock)
    monkeypatch.setattr(algorithms, "fetch_daily", fetch)
    result = algorithms.analyze(
        {"symbol": "600519", "period_start": "2026-09-01", "period_end": "2026-09-09"},
        tmp_path,
    )
    assert result["status"] == "AVAILABLE"
    assert calls == [date(2026, 9, 8)] * 2  # Primary and market proxy share cutoff.
