from datetime import UTC, date, datetime, timedelta

import polars as pl
from test_chart_suite import bars

from quantforge.strategies.chart_evaluation import classification_metrics
from quantforge.strategies.chart_quality import daily_quality, is_session


def sessions(count=100):
    values = []
    day = date(2024, 1, 2)
    while len(values) < count:
        if is_session(day):
            values.append(datetime(day.year, day.month, day.day, 7, tzinfo=UTC))
        day += timedelta(days=1)
    return bars([100.0] * count).with_columns(
        pl.Series("timestamp", values, dtype=pl.Datetime("us", "UTC"))
    )


def test_official_holidays_are_not_gaps_and_unknown_years_are_not_guessed():
    assert is_session(date(2026, 10, 1)) is False
    assert is_session(date(2026, 10, 8)) is True
    assert is_session(date(2024, 2, 9)) is False
    assert is_session(date(2027, 1, 4)) is None
    assert is_session(date(2026, 1, 5), date(2025, 11, 1)) is None
    assert daily_quality(sessions())["eligible"]
    frame = sessions().filter(pl.int_range(pl.len()) != 50)
    assert daily_quality(frame)["hard_flags"] == ["SESSION_GAPS"]


def test_metrics_do_not_hide_abstentions_in_balanced_accuracy():
    metrics = classification_metrics(
        ["BULLISH", "RANGE", "BEARISH"], ["BULLISH", "UNCLEAR", "UNCLEAR"]
    )
    assert metrics["balanced_accuracy_all"] == 1 / 3
    assert metrics["balanced_accuracy_covered"] == 1
    assert metrics["covered"] == 1 and metrics["abstained"] == 2
