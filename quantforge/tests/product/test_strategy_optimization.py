from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from quantforge.data import BAR_SCHEMA
from quantforge.product import strategy_optimization as optimizer


def bars(count: int = 240, *, trend: bool = False) -> pl.DataFrame:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    prices = [
        100 + i * 0.1 if trend else 100 + 5 * math.sin(i * 0.25) + i * 0.01
        for i in range(count)
    ]
    return pl.DataFrame(
        {
            "symbol": ["TEST"] * count,
            "timestamp": [start + timedelta(days=i) for i in range(count)],
            "open": prices,
            "close": prices,
            "high": [value + 1 for value in prices],
            "low": [value - 1 for value in prices],
            "volume": [10000.0] * count,
        },
        schema=BAR_SCHEMA,
    )


def protocol() -> optimizer.OptimizationProtocol:
    return optimizer.OptimizationProtocol(
        training_bars=120,
        inner_history_bars=80,
        inner_window_bars=20,
        evaluation_window_bars=30,
    )


def test_future_prices_cannot_change_prior_parameter_choices() -> None:
    frame = bars()
    future = (
        frame.with_row_index("index")
        .with_columns(
            *[
                pl.when(pl.col("index") >= 150)
                .then(pl.col(name) * 5)
                .otherwise(pl.col(name))
                .alias(name)
                for name in ("open", "high", "low", "close")
            ]
        )
        .drop("index")
    )
    original = optimizer.evaluate_optimization(frame, protocol())
    changed = optimizer.evaluate_optimization(future, protocol())
    assert original["decisions"][:2] == changed["decisions"][:2]
    assert all(
        decision["train_end"] < decision["evaluation_start"]
        for decision in original["decisions"]
    )
    assert original["candidate_evaluations"] == 24
    assert not original["validated_alpha"]
    assert not original["live_eligible"]


def test_real_position_switches_are_charged_at_window_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = bars(trend=True)
    selections = iter(["momentum-10", "cash", "cash", "cash"])
    monkeypatch.setattr(
        optimizer,
        "select_parameters",
        lambda *_: {
            "selected": next(selections),
            "candidate_count": 6,
            "candidates": [],
        },
    )
    result = optimizer.evaluate_optimization(frame, protocol())
    cost = next(row for row in result["scenarios"] if row["fee_bps"] == 10)
    adaptive = next(row for row in cost["strategies"] if row["strategy"] == "optimized")
    assert adaptive["metrics"]["turnover"] == 2
    assert adaptive["periods"][0]["turnover"] == 1
    assert adaptive["periods"][30]["turnover"] == 1
    assert adaptive["periods"][31]["strategy_return"] == 0
    expected = float(frame["open"][150]) / float(frame["open"][120]) * 0.999**2 - 1
    assert adaptive["metrics"]["total_return"] == pytest.approx(expected)


def test_cash_fallback_and_complete_comparable_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        optimizer,
        "select_parameters",
        lambda *_: {"selected": "cash", "candidate_count": 6, "candidates": []},
    )
    result = optimizer.evaluate_optimization(bars(251), protocol())
    for scenario in result["scenarios"]:
        assert len(scenario["strategies"]) == 5
        for row in scenario["strategies"]:
            assert len(row["periods"]) == 131
            assert row["periods"][0]["timestamp"] == result["evaluation_start"]
            assert row["periods"][-1]["timestamp"] == result["evaluation_end"]
        cash = next(
            row for row in scenario["strategies"] if row["strategy"] == "optimized"
        )
        assert cash["metrics"]["total_return"] == cash["metrics"]["turnover"] == 0
    assert result["decisions"][-1]["end_index"] == 251


def test_training_validation_prefers_cash_on_degenerate_data() -> None:
    frame = (
        bars()
        .head(120)
        .with_columns(
            pl.lit(100.0).alias("open"),
            pl.lit(100.0).alias("close"),
            pl.lit(101.0).alias("high"),
            pl.lit(99.0).alias("low"),
        )
    )
    choice = optimizer.select_parameters(frame, protocol())
    assert choice["selected"] == "cash"
    assert len(choice["candidates"]) == 6
    assert all(
        [scenario["fee_bps"] for scenario in row["scenarios"]] == [10, 30]
        for row in choice["candidates"]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"training_bars": True},
        {"evaluation_window_bars": 0},
        {"inner_history_bars": 20},
        {"training_bars": 100},
    ],
)
def test_invalid_protocol_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        optimizer.OptimizationProtocol(**{**vars(protocol()), **changes})


def test_invalid_or_insufficient_bars_fail_closed() -> None:
    with pytest.raises(ValueError):
        optimizer.evaluate_optimization(bars(130), protocol())
    damaged = bars().with_columns(pl.lit(float("nan")).alias("close"))
    with pytest.raises(ValueError):
        optimizer.evaluate_optimization(damaged, protocol())
    duplicate = pl.concat([bars(), bars().head(1)])
    with pytest.raises(ValueError):
        optimizer.evaluate_optimization(duplicate, protocol())
