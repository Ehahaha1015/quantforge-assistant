from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from quantforge.backtest import BacktestConfig, run_vectorized_backtest


def make_bars(
    *,
    opens: list[float] | None = None,
    closes: list[float] | None = None,
    symbol: str = "XYZ",
) -> pl.DataFrame:
    opens = opens or [100.0, 104.0, 107.0, 105.0]
    closes = closes or [102.0, 108.0, 106.0, 103.0]
    timestamps = [datetime(2024, 1, 2, tzinfo=UTC) + timedelta(days=i) for i in range(len(opens))]
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(opens),
            "timestamp": timestamps,
            "open": opens,
            "high": [max(open_, close) + 1.0 for open_, close in zip(opens, closes, strict=True)],
            "low": [min(open_, close) - 1.0 for open_, close in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1000.0] * len(opens),
        }
    ).with_columns(pl.col("timestamp").cast(pl.Datetime("us", "UTC")))


def make_signals(values: list[float], *, symbol: str = "XYZ", timestamps: list[datetime] | None = None) -> pl.DataFrame:
    timestamps = timestamps or [datetime(2024, 1, 2, tzinfo=UTC) + timedelta(days=i) for i in range(len(values))]
    return pl.DataFrame(
        {"symbol": [symbol] * len(values), "timestamp": timestamps, "value": values}
    ).with_columns(
        pl.col("timestamp").cast(pl.Datetime("us", "UTC")), pl.col("value").cast(pl.Float64)
    )


def test_golden_next_open_timing_and_fee() -> None:
    result = run_vectorized_backtest(
        make_bars(), make_signals([1.0, 1.0, 0.0, 0.0]), BacktestConfig(initial_cash=1000.0, fee_bps=10.0)
    )
    periods = result.periods
    assert periods["position_before_open"][0] == 0.0
    assert periods["position_after_open"][0] == 0.0
    assert periods["turnover"][0] == 0.0
    assert periods["strategy_return"][0] == 0.0
    assert periods["equity"][0] == 1000.0

    # Hand calculation: row 1 enters at 104 after signal[0], paying 10 bps;
    # row 2 holds; row 3 exits at 105 after signal[2], paying another 10 bps.
    expected_row_1 = 1000.0 * (1.0 - 0.001) * (108.0 / 104.0)
    expected_row_2 = expected_row_1 * (107.0 / 108.0) * (106.0 / 107.0)
    expected_row_3 = expected_row_2 * (105.0 / 106.0) * (1.0 - 0.001)
    assert periods["position_after_open"][1] == 1.0
    assert periods["turnover"][1] == 1.0
    assert periods["overnight_return"][1] == pytest.approx(104.0 / 102.0 - 1.0)
    assert periods["intraday_return"][1] == pytest.approx(108.0 / 104.0 - 1.0)
    assert periods["equity"][1] == pytest.approx(expected_row_1)
    assert periods["position_before_open"][2] == 1.0
    assert periods["position_after_open"][2] == 1.0
    assert periods["turnover"][2] == 0.0
    assert periods["equity"][2] == pytest.approx(expected_row_2)
    assert periods["position_before_open"][3] == 1.0
    assert periods["position_after_open"][3] == 0.0
    assert periods["turnover"][3] == 1.0
    assert periods["equity"][3] == pytest.approx(expected_row_3)
    assert result.metrics.turnover == 2.0


def test_flat_and_final_signal_cannot_trade() -> None:
    flat = run_vectorized_backtest(make_bars(), make_signals([0.0] * 4))
    assert flat.periods.get_column("equity").to_list() == [100000.0] * 4
    final = run_vectorized_backtest(make_bars(), make_signals([0.0, 0.0, 0.0, 1.0]))
    assert final.periods.get_column("turnover").to_list() == [0.0] * 4
    assert final.periods.get_column("position_after_open").to_list() == [0.0] * 4


def test_single_bar_is_explicitly_handled_without_execution() -> None:
    bars = make_bars(opens=[100.0], closes=[101.0])
    result = run_vectorized_backtest(bars, make_signals([1.0]))
    assert result.periods.height == 1
    assert result.periods["position_after_open"][0] == 0.0
    assert result.periods["turnover"][0] == 0.0
    assert result.periods["strategy_return"][0] == 0.0
    assert result.periods["equity"][0] == 100000.0
    assert result.metrics.turnover == 0.0


def test_multiple_symbols_are_rejected() -> None:
    bars = make_bars().with_columns(
        pl.when(pl.arange(0, 4) == 3).then(pl.lit("OTHER")).otherwise(pl.col("symbol")).alias("symbol")
    )
    with pytest.raises(ValueError, match="exactly one symbol"):
        run_vectorized_backtest(bars, make_signals([0.0] * 4))


def test_signal_columns_may_be_reordered_but_schema_is_canonical() -> None:
    signals = make_signals([0.0, 1.0, 1.0, 0.0]).select("value", "symbol", "timestamp")
    result = run_vectorized_backtest(make_bars(), signals)
    assert result.periods["signal_value"].to_list() == [0.0, 1.0, 1.0, 0.0]


def test_signal_one_enters_next_bar_and_no_same_bar_execution() -> None:
    result = run_vectorized_backtest(make_bars(), make_signals([1.0] * 4), BacktestConfig(initial_cash=100.0))
    assert result.periods["position_after_open"][0] == 0.0
    assert result.periods["equity"][0] == 100.0
    assert result.periods["position_after_open"][1] == 1.0
    assert result.periods["turnover"][1] == 1.0


def test_future_bar_mutation_does_not_change_earlier_results() -> None:
    bars = make_bars()
    baseline = run_vectorized_backtest(bars, make_signals([1.0, 1.0, 0.0, 0.0]))
    changed = bars.with_columns(
        pl.when(pl.col("timestamp") == datetime(2024, 1, 5, tzinfo=UTC))
        .then(pl.lit(999.0))
        .otherwise(pl.col("close"))
        .alias("close"),
        pl.when(pl.col("timestamp") == datetime(2024, 1, 5, tzinfo=UTC))
        .then(pl.lit(1000.0))
        .otherwise(pl.col("high"))
        .alias("high"),
        pl.when(pl.col("timestamp") == datetime(2024, 1, 5, tzinfo=UTC))
        .then(pl.lit(100.0))
        .otherwise(pl.col("low"))
        .alias("low"),
    )
    mutated = run_vectorized_backtest(changed, make_signals([1.0, 1.0, 0.0, 0.0]))
    assert baseline.periods.head(3).equals(mutated.periods.head(3))


def test_future_signal_mutation_does_not_change_earlier_results() -> None:
    bars = make_bars()
    baseline = run_vectorized_backtest(bars, make_signals([1.0, 1.0, 0.0, 0.0]))
    changed_signals = make_signals([1.0, 1.0, 0.0, 1.0])
    mutated = run_vectorized_backtest(bars, changed_signals)
    assert baseline.periods.head(3).equals(mutated.periods.head(3))


def test_input_order_does_not_change_output() -> None:
    bars = make_bars()
    signals = make_signals([1.0, 1.0, 0.0, 0.0])
    ordered = run_vectorized_backtest(bars, signals)
    shuffled = run_vectorized_backtest(bars.reverse(), signals.reverse())
    assert ordered.periods.equals(shuffled.periods)
    assert ordered.metrics == shuffled.metrics


@pytest.mark.parametrize("value", [-0.1, 1.1, 0.5, float("nan"), float("inf")])
def test_invalid_signal_values_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        run_vectorized_backtest(make_bars(), make_signals([0.0, value, 0.0, 0.0]))


def test_duplicate_and_mismatched_signal_keys_rejected() -> None:
    bars = make_bars()
    signals = make_signals([0.0, 0.0, 0.0, 0.0])
    duplicate = pl.concat([signals, signals.head(1)])
    with pytest.raises(ValueError):
        run_vectorized_backtest(bars, duplicate)
    wrong_timestamps = signals.with_columns(
        pl.when(pl.arange(0, 4) == 0)
        .then(pl.lit(datetime(2024, 2, 1, tzinfo=UTC)))
        .otherwise(pl.col("timestamp"))
        .alias("timestamp")
    )
    with pytest.raises(ValueError):
        run_vectorized_backtest(bars, wrong_timestamps)
    with pytest.raises(ValueError):
        run_vectorized_backtest(bars, signals.with_columns(pl.lit("OTHER").alias("symbol")))


def test_signal_row_count_and_null_rejected() -> None:
    with pytest.raises(ValueError):
        run_vectorized_backtest(make_bars(), make_signals([0.0, 0.0, 0.0]))
    null_signal = make_signals([0.0, 0.0, 0.0, 0.0]).with_columns(
        pl.when(pl.arange(0, 4) == 2).then(None).otherwise(pl.col("value")).alias("value")
    )
    with pytest.raises(ValueError):
        run_vectorized_backtest(make_bars(), null_signal)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial_cash": 0.0},
        {"initial_cash": float("inf")},
        {"fee_bps": -1.0},
        {"fee_bps": float("nan")},
        {"periods_per_year": 0},
    ],
)
def test_invalid_config_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        BacktestConfig(**kwargs)


def test_zero_volatility_sharpe_is_zero_and_metrics_are_deterministic() -> None:
    config = BacktestConfig(initial_cash=1000.0, periods_per_year=252)
    first = run_vectorized_backtest(make_bars(), make_signals([0.0] * 4), config)
    second = run_vectorized_backtest(make_bars(), make_signals([0.0] * 4), config)
    assert first.metrics == second.metrics
    assert first.metrics.total_return == 0.0
    assert first.metrics.annualized_volatility == 0.0
    assert first.metrics.sharpe == 0.0
    assert first.metrics.max_drawdown == 0.0
    assert first.metrics.turnover == 0.0
