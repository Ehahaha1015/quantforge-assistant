from __future__ import annotations

import math
from decimal import Decimal

import polars as pl
import pytest

from quantforge.product.fixed_portfolio import replay_fixed_portfolio
from quantforge.product.single_factor_study import buffer_targets, evaluate_buffer_study
from quantforge.product.strategy_stability import (
    block_log_mean_interval,
    evaluate_stability,
    neighborhood_signals,
)
from tests.product.test_execution_replay import policy
from tests.product.test_strategy_optimization import bars


def test_buffer_and_neighborhood_do_not_read_future() -> None:
    frame = bars(300)
    future = (
        frame.with_row_index("i")
        .with_columns(
            *[
                pl.when(pl.col("i") >= 230)
                .then(pl.col(key) * 2)
                .otherwise(pl.col(key))
                .alias(key)
                for key in ("open", "close", "high", "low")
            ]
        )
        .drop("i")
    )
    for band in ("0", "0.5", "1", "2"):
        assert (
            buffer_targets(frame, band, history_bars=120)
            .head(230)
            .equals(buffer_targets(future, band, history_bars=120).head(230))
        )
    for lookback in (48, 60, 72):
        assert (
            neighborhood_signals(frame, lookback, "1")
            .head(230)
            .equals(neighborhood_signals(future, lookback, "1").head(230))
        )


def test_strict_attribution_has_same_periods_and_reconciles() -> None:
    result = evaluate_buffer_study(bars(300), history_bars=120)
    for scenario in result["scenarios"]:
        assert {len(row["periods"]) for row in scenario["variants"]} == {180}
        assert scenario["variants"][0]["periods"][0]["before"] == 0
    for row in result["attribution"]:
        assert (
            abs(
                row["net_log_gap"]
                - row["zero_fee_timing_log_gap"]
                - row["relative_cost_drag_log_gap"]
            )
            < 1e-12
        )
    assert not result["validated_alpha"]


def test_block_interval_reproducible_and_rejects_invalid() -> None:
    values = [math.sin(index) * 0.01 for index in range(100)]
    assert block_log_mean_interval(values) == block_log_mean_interval(values)
    zero = block_log_mean_interval([0.0] * 100)
    assert zero["lower_95"] == zero["upper_95"] == 0
    with pytest.raises(ValueError):
        block_log_mean_interval([float("nan")] * 100)


def test_shared_cash_fees_cap_missing_quotes_and_future_prefix() -> None:
    frame = bars(40).with_columns(pl.lit(1e7).alias("volume"))
    second = frame.with_columns(pl.lit("OTHER").alias("symbol")).slice(0, 20)
    second = pl.concat(
        [second, frame.with_columns(pl.lit("OTHER").alias("symbol")).slice(21)]
    )
    assets = {"TEST": frame, "OTHER": second}
    start = frame["timestamp"][1].date()
    value = replay_fixed_portfolio(
        assets,
        {"TEST": Decimal("0.4"), "OTHER": Decimal("0.4")},
        policy(),
        start_session=start,
        max_gross_weight=Decimal("0.8"),
    )
    assert value["paused_sessions"] == 1
    paused = value["periods"][19]
    assert paused["missing_assets"] == ["OTHER"]
    assert not any(row["session"] == paused["session"] for row in value["orders"])
    assert all(Decimal(row["cash"]) >= 0 for row in value["periods"])
    assert any(row["status"] == "PARTIAL" for row in value["orders"])
    assert Decimal(value["fee_total"]) > 0
    truncated = replay_fixed_portfolio(
        {key: item.head(10) for key, item in assets.items()},
        {"TEST": Decimal("0.4"), "OTHER": Decimal("0.4")},
        policy(),
        start_session=start,
        max_gross_weight=Decimal("0.8"),
    )
    assert value["periods"][:9] == truncated["periods"]


def test_fixed_cash_and_oversubscribed_weights() -> None:
    frame = bars(30)
    value = replay_fixed_portfolio(
        {"TEST": frame},
        {"TEST": Decimal(0)},
        policy(),
        start_session=frame["timestamp"][1].date(),
    )
    assert not value["orders"] and Decimal(value["net_return"]) == 0
    with pytest.raises(ValueError):
        replay_fixed_portfolio(
            {"TEST": frame},
            {"TEST": Decimal("1.01")},
            policy(),
            start_session=frame["timestamp"][1].date(),
        )


def test_stability_training_choices_ignore_all_later_returns() -> None:
    frame = bars(820)
    future = (
        frame.with_row_index("i")
        .with_columns(
            *[
                pl.when(pl.col("i") >= 756)
                .then(pl.col(key) * 1.5)
                .otherwise(pl.col(key))
                .alias(key)
                for key in ("open", "close", "high", "low")
            ]
        )
        .drop("i")
    )
    original, changed = evaluate_stability(frame), evaluate_stability(future)
    assert len(original["rows"]) == 81
    assert sum(original["selection_frequencies"].values()) == 9
    for left, right in zip(original["selections"], changed["selections"], strict=True):
        assert left["selected"] == right["selected"]
        assert left["ranking"] == right["ranking"]
        assert left["training_last_timestamp"] < left["evaluation_first_timestamp"]
