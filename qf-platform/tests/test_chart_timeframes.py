import json
from datetime import date, datetime
from pathlib import Path

import pytest
from test_chart_analysis import fixture_report, image_payload

from qf_platform.chart_reports import (
    ChartRequest,
    assess_alignment,
    horizon_text,
    markdown,
)
from qf_platform.chart_timeframes import cutoff_at, parse_period


def hourly(rows):
    return json.dumps({"code": 0, "data": {"sh600519": {"m60": rows}}}).encode()


def test_legacy_defaults_and_frequency_specific_horizons_are_unambiguous():
    old = ChartRequest.model_validate(image_payload())
    assert old.timeframe == "1d" and old.horizon_bars == 5
    for kind, expected in (("60m", 4), ("1w", 1)):
        value = ChartRequest.model_validate(image_payload(timeframe=kind))
        assert value.horizon_bars == expected
        assert "根" in horizon_text(value.model_dump())
    assert ChartRequest.model_validate(
        image_payload(timeframe="1w", period_start="2023-10-01")
    )
    for changes in (
        {"timeframe": "60m", "horizon_bars": 20},
        {"timeframe": "1w", "horizon_bars": 5},
        {"period_end_time": "25:00"},
        {"purchase_price": "NaN"},
        {"purchase_price": "0"},
        {"held_bars": 2},
        {"held_bars": True},
    ):
        with pytest.raises(ValueError):
            ChartRequest.model_validate(image_payload(**changes))


def test_current_week_and_current_daily_candle_are_excluded_before_close():
    now = datetime.fromisoformat("2026-10-16T15:30:00+08:00")
    meta = {"period_end": "2026-10-16"}
    assert cutoff_at({**meta, "timeframe": "1d"}, now).date() == date(2026, 10, 15)
    assert cutoff_at({**meta, "timeframe": "1w"}, now).date() == date(2026, 10, 9)
    after = datetime.fromisoformat("2026-10-16T16:01:00+08:00")
    assert cutoff_at({**meta, "timeframe": "1w"}, after).date() == date(2026, 10, 16)
    assert cutoff_at(
        {"period_end": "2026-09-30", "timeframe": "1w"}, after
    ).date() == date(2026, 9, 25)


def test_hourly_parser_ignores_future_prices_and_rejects_bad_sessions():
    cutoff = datetime.fromisoformat("2026-10-16T10:45:00+08:00")
    past = ["202610161030", "100", "101", "102", "99", "1000"]
    future = ["202610161130", "not-a-price", None, True, False, {}]
    frame, discarded = parse_period(
        hourly([past, future]), "sh600519", date(2026, 10, 1), cutoff, "60m"
    )
    assert frame.height == 1 and discarded == 1
    assert frame["timestamp"][0].isoformat() == "2026-10-16T02:30:00+00:00"
    same, _ = parse_period(
        hourly([past, ["202610161130", "999999", "2", "3", "4", "5"]]),
        "sh600519",
        date(2026, 10, 1),
        cutoff,
        "60m",
    )
    assert same.equals(frame)
    with pytest.raises(ValueError):
        parse_period(hourly([past, past]), "sh600519", date(2026, 10, 1), cutoff, "60m")
    with pytest.raises(ValueError, match="时段"):
        parse_period(
            hourly([["202610161230", "100", "101", "102", "99", "1000"]]),
            "sh600519",
            date(2026, 10, 1),
            datetime.fromisoformat("2026-10-16T15:00:00+08:00"),
            "60m",
        )


def test_weekly_parser_never_accepts_current_partial_week_or_adjusted_fallback():
    cutoff = datetime.fromisoformat("2026-10-09T16:00:00+08:00")
    payload = {
        "code": 0,
        "data": {
            "sh600519": {
                "week": [
                    ["2026-10-09", "100", "101", "102", "99", "1000"],
                    ["2026-10-14", "bad"],
                ]
            }
        },
    }
    # Future row still needs its declared shape, but future prices are unused.
    payload["data"]["sh600519"]["week"][1] = [
        "2026-10-14",
        "bad",
        None,
        None,
        None,
        None,
    ]
    frame, discarded = parse_period(
        json.dumps(payload).encode(), "sh600519", date(2025, 1, 1), cutoff, "1w"
    )
    assert frame.height == 1 and discarded == 1
    payload["data"]["sh600519"]["qfqweek"] = payload["data"]["sh600519"].pop("week")
    with pytest.raises(KeyError):
        parse_period(
            json.dumps(payload).encode(), "sh600519", date(2025, 1, 1), cutoff, "1w"
        )


def test_visual_identity_and_period_mismatch_block_the_purchase_conclusion():
    meta = {
        "symbol": "600519",
        "timeframe": "60m",
        "period_start": "2026-10-01",
        "period_end": "2026-10-16",
        "period_end_time": "15:00",
    }
    matching = fixture_report(
        recognized_symbol="贵州茅台600519",
        recognized_timeframe="60m",
        recognized_period="2026-10-01 至 2026-10-16 截止15:00",
    )
    assert assess_alignment(meta, matching)["status"] == "CONSISTENT"
    assert (
        assess_alignment(
            meta, fixture_report(recognized_symbol="000001", recognized_timeframe="60m")
        )["status"]
        == "MISMATCH"
    )
    assert (
        assess_alignment(
            meta, fixture_report(recognized_symbol="600519", recognized_timeframe="1d")
        )["status"]
        == "MISMATCH"
    )
    assert assess_alignment(meta, fixture_report())["status"] == "UNCONFIRMED"


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"recognized_period": "2024-10-01 至 2024-10-16 截止15:00"}, "MISMATCH"),
        (
            {"recognized_period": "2026年10月1日至2026年10月16日 截止15:00"},
            "CONSISTENT",
        ),
        ({"recognized_period": "2026-10-01 至 2026-10-16 截止11:30"}, "MISMATCH"),
        ({"recognized_period": "2026-10-01 至 2026-10-16"}, "UNCONFIRMED"),
        ({"recognized_period": "2026-10-01 至 2026-10-15 截止15:00"}, "UNCONFIRMED"),
        ({"recognized_period": None}, "UNCONFIRMED"),
        ({"recognized_symbol": "600519 / 000001"}, "UNCONFIRMED"),
    ],
)
def test_purchase_gate_requires_dates_and_unambiguous_identity(changes, status):
    meta = {
        "symbol": "600519",
        "timeframe": "60m",
        "period_start": "2026-10-01",
        "period_end": "2026-10-16",
    }
    values = {
        "recognized_symbol": "600519",
        "recognized_timeframe": "60m",
        "recognized_period": "2026-10-01 至 2026-10-16 截止15:00",
    }
    assert (
        assess_alignment(meta, fixture_report(**{**values, **changes}))["status"]
        == status
    )


def test_markdown_rechecks_old_stored_alignment_without_changing_the_record():
    job = {
        "request": {
            "symbol": "600519",
            "period_start": "2026-10-01",
            "period_end": "2026-10-16",
            "horizon_sessions": 5,
        },
        "report": fixture_report(
            recognized_symbol="600519",
            recognized_timeframe="1d",
            recognized_period="2024-10-01 至 2024-10-16",
        ).model_dump(),
        "image_alignment": {"status": "CONSISTENT", "note": "旧版核对"},
        "updated_at": "2026-10-17",
        "strategy_version": "old",
        "image": {"sha256": "a" * 64},
        "algorithm_analysis": {
            "status": "UNAVAILABLE",
            "reason": "软件夹具",
            "trade_plan": {
                "action": "CONSIDER_ENTRY",
                "levels": {"reference": "12345.6789"},
                "execution_constraint": "T+1",
                "basis": "软件夹具",
            },
        },
    }
    before = json.dumps(job, ensure_ascii=False, sort_keys=True)
    text = markdown(job)
    assert (
        "不一致" in text
        and "12345.6789" not in text
        and "条件满足，可考虑购入" not in text
    )
    assert json.dumps(job, ensure_ascii=False, sort_keys=True) == before


def test_missing_hourly_data_is_not_replaced_by_daily_data(tmp_path: Path, monkeypatch):
    from qf_platform import chart_algorithms

    def unavailable(*args):
        raise OSError("hour source unavailable")

    def daily_forbidden(*args):
        raise AssertionError("Hourly requests cannot use daily bars")

    monkeypatch.setattr(chart_algorithms, "fetch_period", unavailable)
    monkeypatch.setattr(chart_algorithms, "fetch_daily", daily_forbidden)
    result = chart_algorithms.analyze(
        {
            "symbol": "600519",
            "timeframe": "60m",
            "period_start": "2026-10-01",
            "period_end": "2026-10-16",
        },
        tmp_path,
    )
    assert result["status"] == "UNAVAILABLE" and result["trade_plan"] is None
