from __future__ import annotations

from decimal import Decimal, localcontext

import polars as pl
import pytest

from quantforge.product.trend_refinement import (
    BufferedTrendConfig,
    buffered_signals,
    evaluate_refinements,
)
from tests.product.test_strategy_optimization import bars


@pytest.mark.parametrize("family", ["moving_average_crossover", "time_series_momentum"])
@pytest.mark.parametrize("trend", [0, 120])
def test_buffered_signals_are_causal_and_context_independent(
    family: str,
    trend: int,
) -> None:
    config = BufferedTrendConfig(family, Decimal("0.5"), trend)  # type: ignore[arg-type]
    frame = bars(260)
    prefix = buffered_signals(frame.head(170), config)
    future = (
        frame.with_row_index("index")
        .with_columns(
            *[
                pl.when(pl.col("index") >= 170)
                .then(pl.col(name) * 4)
                .otherwise(pl.col(name))
                .alias(name)
                for name in ("open", "high", "low", "close")
            ]
        )
        .drop("index")
    )
    assert buffered_signals(future, config).head(170).equals(prefix)
    with localcontext() as context:
        context.prec = 8
        low_precision = buffered_signals(frame, config)
    assert low_precision.equals(buffered_signals(frame, config))
    assert set(prefix["value"].to_list()) <= {0.0, 1.0}
    if trend:
        assert prefix["value"].head(119).sum() == 0


def test_momentum_retains_position_inside_buffer_then_exits() -> None:
    frame = bars(180).with_columns(
        pl.Series("close", [100.0] * 60 + [102.0, 100.5, 99.0] + [100.0] * 117),
        pl.lit(100.0).alias("open"),
        pl.lit(104.0).alias("high"),
        pl.lit(98.0).alias("low"),
    )
    signal = buffered_signals(
        frame, BufferedTrendConfig("time_series_momentum", Decimal(1))
    )
    assert signal["value"][59] == 0
    assert signal["value"][60] == 1
    assert signal["value"][61] == 1
    assert signal["value"][62] == 0


def test_equal_entry_threshold_does_not_enter() -> None:
    frame = bars(160).with_columns(
        pl.Series("close", [100.0] * 60 + [101.0] * 100),
        pl.lit(100.0).alias("open"),
        pl.lit(103.0).alias("high"),
        pl.lit(99.0).alias("low"),
    )
    values = buffered_signals(
        frame, BufferedTrendConfig("time_series_momentum", Decimal(1))
    )["value"]
    assert values[60] == 0


def test_fixed_evaluation_costs_delay_and_exposure() -> None:
    result = evaluate_refinements(bars(180, trend=True), history_bars=120)
    assert result["variant_evaluations"] == 10
    assert result["scenario_evaluations"] == 40
    assert not result["validated_alpha"] and not result["live_eligible"]
    for scenario in result["scenarios"]:
        assert len(scenario["strategies"]) == 10
        for row in scenario["strategies"]:
            assert len(row["periods"]) == 60
            assert 0 <= row["exposure_fraction"] <= 1
            assert 0 <= row["flat_up_day_fraction"] <= 1
        cash = next(row for row in scenario["strategies"] if row["strategy"] == "cash")
        assert cash["exposure_fraction"] == 0
        assert cash["flat_up_day_fraction"] == 1
        assert cash["metrics"]["total_return"] == 0
    delayed = result["scenarios"][-1]
    held = next(
        row for row in delayed["strategies"] if row["strategy"] == "buy_and_hold"
    )
    assert held["periods"][0]["turnover"] == 0
    assert held["periods"][1]["turnover"] == 1


@pytest.mark.parametrize("buffer", [0.5, Decimal("NaN"), Decimal(-1), Decimal(0)])
def test_invalid_buffers_are_rejected(buffer: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        BufferedTrendConfig("time_series_momentum", buffer)  # type: ignore[arg-type]


def test_damaged_data_and_invalid_history_are_rejected() -> None:
    with pytest.raises(ValueError):
        evaluate_refinements(bars(180), history_bars=True)
    with pytest.raises(ValueError):
        buffered_signals(
            bars(180).with_columns(pl.lit(float("nan")).alias("close")),
            BufferedTrendConfig("time_series_momentum", Decimal(1)),
        )
