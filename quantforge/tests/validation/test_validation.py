from __future__ import annotations

import dataclasses
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from quantforge.backtest import (
    SIGNAL_COLUMNS,
    BacktestConfig,
    run_vectorized_backtest,
)
from quantforge.backtest.engine import _metrics as backtest_metrics
from quantforge.data import ParquetDataCatalog
from quantforge.experiments import (
    ExperimentRecord,
    ExperimentStore,
    canonical_config_hash,
)
from quantforge.strategies import (
    MovingAverageCrossoverConfig,
    generate_moving_average_signals,
)
from quantforge.validation import (
    ChronologicalHoldoutConfig,
    ValidationResult,
    WalkForwardConfig,
    run_holdout_validation,
    run_walk_forward_validation,
)
from quantforge.validation.metrics import oos_metrics

START = datetime(2025, 1, 2, tzinfo=UTC)
COMMIT = "d83aafc43ab3f40843356a3959bc35669fdc4efe"
FAST = MovingAverageCrossoverConfig(short_window=1, long_window=2)
SLOW = MovingAverageCrossoverConfig(short_window=2, long_window=3)
FREE = BacktestConfig(initial_cash=1000.0, fee_bps=0.0)
COSTLY = BacktestConfig(initial_cash=1000.0, fee_bps=10.0)

HAND_PRICES = [10, 10, 10, 10, 10, 11, 12, 13, 14, 15]
MIXED_PRICES = [10, 11, 9, 12, 13, 8, 14, 15, 7, 16, 17, 6, 13, 12, 11, 10]
RISING_PRICES = [10, 11, 12, 13, 14, 15, 16, 17]
SPIKE_HISTORY = [10, 100, 100, 100, 100, 100, 100, 100, 100, 100]


def make_bars(prices: list[float], *, symbol: str = "XYZ") -> pl.DataFrame:
    """Daily canonical bars with open == close so returns are hand-checkable."""
    timestamps = [START + timedelta(days=index) for index in range(len(prices))]
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(prices),
            "timestamp": timestamps,
            "open": prices,
            "high": [value + 1.0 for value in prices],
            "low": [value - 1.0 for value in prices],
            "close": prices,
            "volume": [1000.0] * len(prices),
        },
        schema={
            "symbol": pl.String,
            "timestamp": pl.Datetime("us", "UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
        },
    )


def timestamp_microseconds(frame: pl.DataFrame) -> list[int]:
    return frame["timestamp"].cast(pl.Int64).to_list()


def expected_day(offset: int) -> datetime:
    return START + timedelta(days=offset)


def holdout(bars: pl.DataFrame, history: int, strategy=SLOW, backtest=FREE) -> ValidationResult:
    return run_holdout_validation(
        bars, strategy, backtest, ChronologicalHoldoutConfig(history_bars=history)
    )


def walk_forward(
    bars: pl.DataFrame,
    initial: int,
    window: int,
    strategy=SLOW,
    backtest=FREE,
) -> ValidationResult:
    return run_walk_forward_validation(
        bars,
        strategy,
        backtest,
        WalkForwardConfig(initial_history_bars=initial, oos_window_bars=window),
    )


def test_holdout_config_defaults_and_immutability() -> None:
    config = ChronologicalHoldoutConfig()
    assert config.history_bars == 252
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.history_bars = 10  # type: ignore[misc]


@pytest.mark.parametrize("invalid", [0, -1])
def test_holdout_config_rejects_non_positive_history(invalid: int) -> None:
    with pytest.raises(ValueError, match="history_bars"):
        ChronologicalHoldoutConfig(history_bars=invalid)


@pytest.mark.parametrize("invalid", [True, False, 2.0, "3", None])
def test_holdout_config_rejects_non_integer_history(invalid: object) -> None:
    with pytest.raises(TypeError, match="history_bars"):
        ChronologicalHoldoutConfig(history_bars=invalid)


def test_walk_forward_config_defaults_and_validation() -> None:
    config = WalkForwardConfig()
    assert (config.initial_history_bars, config.oos_window_bars) == (252, 63)
    for invalid in (0, -1):
        with pytest.raises(ValueError, match="initial_history_bars"):
            WalkForwardConfig(initial_history_bars=invalid, oos_window_bars=3)
        with pytest.raises(ValueError, match="oos_window_bars"):
            WalkForwardConfig(initial_history_bars=3, oos_window_bars=invalid)
    for invalid in (True, 2.0, "3", None):
        with pytest.raises(TypeError, match="initial_history_bars"):
            WalkForwardConfig(initial_history_bars=invalid, oos_window_bars=3)
        with pytest.raises(TypeError, match="oos_window_bars"):
            WalkForwardConfig(initial_history_bars=3, oos_window_bars=invalid)


def test_holdout_split_boundaries_and_shape() -> None:
    bars = make_bars(MIXED_PRICES)
    result = holdout(bars, history=5)
    assert result.kind == "holdout"
    assert len(result.windows) == 1
    window = result.windows[0]
    assert (window.index, window.history_bars) == (0, 5)
    assert (window.oos_start_index, window.oos_end_index) == (5, bars.height)
    assert window.result.oos_bars == bars.height - 5
    assert window.result.periods.height == bars.height - 5
    assert window.result.start_timestamp == expected_day(5)
    assert window.result.end_timestamp == expected_day(bars.height - 1)
    assert result.aggregate.periods.equals(window.result.periods)
    assert result.aggregate.metrics == window.result.metrics


def test_holdout_rejects_history_at_or_above_dataset_size() -> None:
    bars = make_bars(MIXED_PRICES)
    with pytest.raises(ValueError, match="less than the canonical bar count"):
        holdout(bars, history=bars.height)
    with pytest.raises(ValueError, match="less than the canonical bar count"):
        holdout(bars, history=bars.height + 5)


def test_holdout_rejects_history_below_strategy_long_window() -> None:
    bars = make_bars(MIXED_PRICES)
    strategy = MovingAverageCrossoverConfig(short_window=4, long_window=6)
    with pytest.raises(ValueError, match="at least the strategy long_window"):
        holdout(bars, history=5, strategy=strategy)
    # The boundary itself is allowed once history equals long_window.
    assert holdout(bars, history=6, strategy=strategy).windows[0].history_bars == 6


def test_walk_forward_rejects_initial_history_below_strategy_long_window() -> None:
    bars = make_bars(MIXED_PRICES)
    strategy = MovingAverageCrossoverConfig(short_window=4, long_window=6)
    with pytest.raises(ValueError, match="at least the strategy long_window"):
        walk_forward(bars, initial=5, window=4, strategy=strategy)


def test_walk_forward_rejects_initial_history_without_oos_bars() -> None:
    bars = make_bars(MIXED_PRICES)
    with pytest.raises(ValueError, match="at least one out-of-sample bar"):
        walk_forward(bars, initial=bars.height, window=4)


@pytest.mark.parametrize(
    "bars",
    [
        make_bars([]),
        make_bars(MIXED_PRICES).drop("volume"),
    ],
)
def test_invalid_bar_frames_are_rejected(bars: pl.DataFrame) -> None:
    with pytest.raises(ValueError):
        holdout(bars, history=5)


def test_multiple_symbols_are_rejected() -> None:
    bars = make_bars(MIXED_PRICES)
    mixed = pl.concat([bars, bars.with_columns(pl.lit("OTHER").alias("symbol"))])
    with pytest.raises(ValueError, match="exactly one symbol"):
        holdout(mixed, history=5)


def test_wrong_argument_types_are_rejected() -> None:
    bars = make_bars(MIXED_PRICES)
    with pytest.raises(TypeError, match="Polars DataFrame"):
        run_holdout_validation(
            [1, 2, 3], SLOW, FREE, ChronologicalHoldoutConfig(history_bars=5)
        )  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ChronologicalHoldoutConfig"):
        run_holdout_validation(bars, SLOW, FREE, WalkForwardConfig(5, 3))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="WalkForwardConfig"):
        run_walk_forward_validation(
            bars, SLOW, FREE, ChronologicalHoldoutConfig(history_bars=5)
        )  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="MovingAverageCrossoverConfig"):
        run_holdout_validation(
            bars, {"short_window": 2}, FREE, ChronologicalHoldoutConfig(history_bars=5)
        )  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="BacktestConfig"):
        run_holdout_validation(
            bars, SLOW, {"initial_cash": 1.0}, ChronologicalHoldoutConfig(history_bars=5)
        )  # type: ignore[arg-type]


def test_holdout_history_returns_are_excluded_from_oos_metrics() -> None:
    bars = make_bars(SPIKE_HISTORY)
    result = holdout(bars, history=5, strategy=FAST)
    # The 10 -> 100 spike happens entirely before the boundary; if context
    # performance leaked into the OOS metrics the return would be ~9.0.
    assert result.aggregate.metrics.total_return == 0.0
    assert result.aggregate.metrics.turnover == 0.0
    assert result.aggregate.periods["strategy_return"].to_list() == [0.0] * 5
    assert result.aggregate.periods["equity"].to_list() == [1000.0] * 5


def test_holdout_does_not_reset_position_at_the_boundary() -> None:
    bars = make_bars(RISING_PRICES)
    result = holdout(bars, history=4, strategy=FAST)
    periods = result.windows[0].result.periods
    # Entered at open[2], so the first out-of-sample bar is already long.
    assert periods["position_before_open"][0] == 1.0
    assert periods["position_after_open"][0] == 1.0
    assert periods["turnover"][0] == 0.0
    assert periods["strategy_return"][0] == pytest.approx(1.0 / 13.0)
    assert result.aggregate.metrics.total_return > 0.0
    assert result.aggregate.metrics.turnover == 0.0


def test_holdout_period_rows_match_the_backtest001_slice() -> None:
    bars = make_bars(MIXED_PRICES)
    signals = generate_moving_average_signals(bars, SLOW)
    engine = run_vectorized_backtest(bars, signals, FREE)
    result = holdout(bars, history=5)
    periods = result.windows[0].result.periods
    assert periods.columns == engine.periods.columns
    reference = engine.periods.slice(5, bars.height - 5)
    for column in (
        "symbol",
        "signal_value",
        "position_before_open",
        "position_after_open",
        "overnight_return",
        "intraday_return",
        "turnover",
        "cost_fraction",
        "strategy_return",
    ):
        assert periods[column].to_list() == reference[column].to_list()
    assert timestamp_microseconds(periods) == timestamp_microseconds(reference)
    # The validation layer rebases equity onto the OOS start: it equals the
    # sequential compounding of the scored out-of-sample returns.
    rebased: list[float] = []
    value = FREE.initial_cash
    for period_return in periods["strategy_return"].to_list():
        value = value * (1.0 + period_return)
        rebased.append(value)
    assert periods["equity"].to_list() == pytest.approx(rebased)


def test_rebased_equity_excludes_context_gains() -> None:
    bars = make_bars(RISING_PRICES)
    signals = generate_moving_average_signals(bars, FAST)
    engine = run_vectorized_backtest(bars, signals, FREE)
    result = holdout(bars, history=4, strategy=FAST)
    raw_first = engine.periods["equity"][4]
    rebased_first = result.windows[0].result.periods["equity"][0]
    # Context compounded 1000 -> ~1166.67 before the boundary; the scored path
    # restarts at initial_cash and compounds only the OOS return.
    assert raw_first == pytest.approx(1000.0 * 14.0 / 12.0)
    assert rebased_first == pytest.approx(1000.0 * (1.0 + 1.0 / 13.0))
    assert rebased_first != raw_first


def test_walk_forward_window_plan_is_contiguous_and_complete() -> None:
    bars = make_bars(MIXED_PRICES)
    result = walk_forward(bars, initial=5, window=4)
    assert result.kind == "walk_forward"
    assert [(w.oos_start_index, w.oos_end_index) for w in result.windows] == [
        (5, 9),
        (9, 13),
        (13, 16),
    ]
    assert [w.index for w in result.windows] == [0, 1, 2]
    assert all(not (history >= len(MIXED_PRICES)) for history in (5, 9, 13))
    assert [w.history_bars for w in result.windows] == [5, 9, 13]
    scored: list[int] = []
    for window in result.windows:
        scored.extend(range(window.oos_start_index, window.oos_end_index))
    assert scored == list(range(5, 16))


def test_walk_forward_first_window_boundary_and_expanding_history() -> None:
    bars = make_bars(MIXED_PRICES)
    result = walk_forward(bars, initial=5, window=4)
    first = result.windows[0]
    assert first.history_bars == 5
    assert first.oos_start_index == 5
    assert first.result.start_timestamp == expected_day(5)
    for previous, current in zip(result.windows, result.windows[1:]):
        assert current.history_bars == previous.oos_end_index
        assert current.oos_start_index == previous.oos_end_index
        assert current.history_bars > previous.history_bars


def test_walk_forward_includes_final_partial_window() -> None:
    bars = make_bars(MIXED_PRICES[:13])
    result = walk_forward(bars, initial=5, window=5)
    assert [(w.oos_start_index, w.oos_end_index) for w in result.windows] == [
        (5, 10),
        (10, 13),
    ]
    assert result.windows[-1].result.oos_bars == 3
    assert result.aggregate.periods.height == 8


def test_walk_forward_collapses_to_one_window_when_remaining_bars_are_short() -> None:
    bars = make_bars(MIXED_PRICES)
    result = walk_forward(bars, initial=5, window=100)
    assert len(result.windows) == 1
    assert (result.windows[0].oos_start_index, result.windows[0].oos_end_index) == (
        5,
        bars.height,
    )


def test_each_window_result_equals_its_own_prefix_recomputation() -> None:
    bars = make_bars(MIXED_PRICES)
    result = walk_forward(bars, initial=5, window=4)
    for window in result.windows:
        prefix = bars.head(window.oos_end_index)
        signals = generate_moving_average_signals(prefix, SLOW)
        engine = run_vectorized_backtest(prefix, signals, FREE)
        reference = engine.periods.slice(
            window.oos_start_index, window.oos_end_index - window.oos_start_index
        )
        for column in ("position_before_open", "position_after_open", "strategy_return", "turnover"):
            assert window.result.periods[column].to_list() == reference[column].to_list()


@pytest.mark.parametrize("tail_change", ["mutate", "append", "remove"])
def test_future_rows_cannot_change_a_completed_window(tail_change: str) -> None:
    bars = make_bars(MIXED_PRICES)
    baseline = walk_forward(bars, initial=5, window=4)
    if tail_change == "mutate":
        changed = make_bars([*MIXED_PRICES[:-1], 500.0])
    elif tail_change == "append":
        changed = make_bars([*MIXED_PRICES, 42.0])
    else:
        changed = make_bars(MIXED_PRICES[:-1])
    altered = walk_forward(changed, initial=5, window=4)
    # Window 0 ends at canonical index 8, so a tail change beyond it is invisible.
    for column in (
        "position_before_open",
        "position_after_open",
        "turnover",
        "strategy_return",
        "equity",
    ):
        assert (
            altered.windows[0].result.periods[column].to_list()
            == baseline.windows[0].result.periods[column].to_list()
        )
    assert altered.windows[0].result.metrics == baseline.windows[0].result.metrics


def test_earlier_oos_result_is_independent_of_later_oos_data() -> None:
    bars = make_bars(MIXED_PRICES)
    baseline = walk_forward(bars, initial=5, window=4)
    changed = make_bars([*MIXED_PRICES[:13], 9.0, 9.0])
    altered = walk_forward(changed, initial=5, window=4)
    assert (
        altered.windows[1].result.periods["strategy_return"].to_list()
        == baseline.windows[1].result.periods["strategy_return"].to_list()
    )
    assert changed.height != bars.height


def test_no_forced_flatten_or_liquidation_at_window_boundaries() -> None:
    bars = make_bars(RISING_PRICES)
    result = walk_forward(bars, initial=4, window=3, strategy=FAST)
    assert [(w.oos_start_index, w.oos_end_index) for w in result.windows] == [
        (4, 7),
        (7, 8),
    ]
    first, second = result.windows
    assert first.result.periods["position_before_open"][0] == 1.0
    assert first.result.periods["turnover"].to_list() == [0.0, 0.0, 0.0]
    assert first.result.periods["position_after_open"][-1] == 1.0
    assert second.result.periods["position_before_open"][0] == 1.0
    assert second.result.periods["position_after_open"][0] == 1.0
    assert result.aggregate.periods["turnover"].to_list() == [0.0] * 4
    assert result.aggregate.metrics.turnover == 0.0


def test_validation_does_not_introduce_same_bar_execution() -> None:
    bars = make_bars(MIXED_PRICES)
    result = holdout(bars, history=5)
    periods = result.windows[0].result.periods
    signals = generate_moving_average_signals(bars, SLOW)["value"].to_list()
    for offset in range(periods.height):
        canonical_index = 5 + offset
        assert periods["signal_value"][offset] == signals[canonical_index]
        assert periods["position_after_open"][offset] == signals[canonical_index - 1]


def test_aggregate_stitches_every_oos_bar_exactly_once() -> None:
    bars = make_bars(MIXED_PRICES)
    result = walk_forward(bars, initial=5, window=4)
    canonical_microseconds = timestamp_microseconds(bars)
    aggregate_microseconds = timestamp_microseconds(result.aggregate.periods)
    assert aggregate_microseconds == canonical_microseconds[5:]
    assert len(set(aggregate_microseconds)) == len(aggregate_microseconds)
    assert result.aggregate.oos_bars == bars.height - 5
    assert result.aggregate.start_timestamp == expected_day(5)
    assert result.aggregate.end_timestamp == expected_day(bars.height - 1)


def test_aggregate_equity_compounds_only_oos_returns() -> None:
    bars = make_bars(MIXED_PRICES)
    result = walk_forward(bars, initial=5, window=4)
    returns = result.aggregate.periods["strategy_return"].to_list()
    expected: list[float] = []
    value = 1000.0
    for period_return in returns:
        value = value * (1.0 + period_return)
        expected.append(value)
    assert result.aggregate.periods["equity"].to_list() == pytest.approx(expected)
    assert result.aggregate.metrics.total_return == pytest.approx(value / 1000.0 - 1.0)


def test_hand_calculated_holdout_oracle() -> None:
    bars = make_bars(HAND_PRICES)
    result = holdout(bars, history=5, strategy=FAST)
    metrics = result.aggregate.metrics
    # OOS returns: 0, 0, 1/12, 1/13, 1/14 -> equity 1000 * (15/12) = 1250.
    assert metrics.total_return == pytest.approx(0.25)
    assert metrics.turnover == pytest.approx(1.0)
    assert metrics.max_drawdown == pytest.approx(0.0)
    returns = [0.0, 0.0, 1 / 12, 1 / 13, 1 / 14]
    mean = math.fsum(returns) / len(returns)
    variance = math.fsum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    deviation = math.sqrt(variance)
    assert metrics.annualized_volatility == pytest.approx(deviation * math.sqrt(252))
    assert metrics.sharpe == pytest.approx(mean / deviation * math.sqrt(252))
    assert result.aggregate.periods["strategy_return"].to_list() == pytest.approx(returns)


def test_hand_calculated_walk_forward_oracle_matches_holdout() -> None:
    bars = make_bars(HAND_PRICES)
    hold = holdout(bars, history=5, strategy=FAST)
    walked = walk_forward(bars, initial=5, window=2, strategy=FAST)
    assert len(walked.windows) == 3
    assert walked.aggregate.periods["strategy_return"].to_list() == pytest.approx(
        hold.windows[0].result.periods["strategy_return"].to_list()
    )
    assert walked.aggregate.metrics == hold.aggregate.metrics
    assert walked.aggregate.metrics.total_return == pytest.approx(0.25)
    assert walked.aggregate.metrics.turnover == pytest.approx(1.0)
    assert walked.aggregate.metrics.max_drawdown == pytest.approx(0.0)


def test_holdout_and_single_window_walk_forward_agree() -> None:
    bars = make_bars(MIXED_PRICES)
    hold = holdout(bars, history=5)
    walked = walk_forward(bars, initial=5, window=bars.height - 5)
    assert len(walked.windows) == 1
    assert walked.aggregate.periods.equals(hold.aggregate.periods)
    assert walked.aggregate.metrics == hold.aggregate.metrics


def test_zero_volatility_sharpe_is_zero() -> None:
    bars = make_bars([10.0] * 10)
    for result in (holdout(bars, history=4, strategy=FAST), walk_forward(bars, initial=4, window=3, strategy=FAST)):
        metrics = result.aggregate.metrics
        assert metrics.annualized_volatility == 0.0
        assert metrics.sharpe == 0.0
        assert metrics.total_return == 0.0
        assert metrics.max_drawdown == 0.0
        assert metrics.turnover == 0.0


@pytest.mark.parametrize(
    "returns,turnovers",
    [
        ([0.01, -0.02, 0.03, 0.0], [1.0, 0.0, 1.0, 0.0]),
        ([0.0, 0.0, 0.0], [1.0, 0.0, 0.0]),
        ([0.05], [1.0]),
        ([-0.1, -0.05], [0.0, 1.0]),
    ],
)
def test_oos_metrics_reproduce_backtest001_definitions(
    returns: list[float], turnovers: list[float]
) -> None:
    path = [FREE.initial_cash]
    for period_return in returns:
        path.append(path[-1] * (1.0 + period_return))
    reference = backtest_metrics(
        [0.0, *returns],
        [FREE.initial_cash, *path[1:]],
        FREE,
        [0.0, *turnovers],
    )
    assert oos_metrics(returns, turnovers, backtest_config=FREE) == reference


def test_oos_metrics_match_a_real_backtest_result() -> None:
    bars = make_bars(MIXED_PRICES)
    signals = generate_moving_average_signals(bars, SLOW)
    engine = run_vectorized_backtest(bars, signals, COSTLY)
    # Drop the structurally flat first bar of the fresh dataset, then score the
    # remaining executable periods with the validation metric definitions.
    returns = engine.periods["strategy_return"].to_list()[1:]
    turnovers = engine.periods["turnover"].to_list()[1:]
    metrics = oos_metrics(returns, turnovers, backtest_config=COSTLY)
    assert metrics.turnover == engine.metrics.turnover
    assert metrics.total_return == pytest.approx(engine.metrics.total_return)
    assert metrics.annualized_volatility == pytest.approx(engine.metrics.annualized_volatility)
    assert metrics.sharpe == pytest.approx(engine.metrics.sharpe)
    assert metrics.max_drawdown == pytest.approx(engine.metrics.max_drawdown)


def test_validation_is_deterministic_and_does_not_mutate_inputs() -> None:
    bars = make_bars(MIXED_PRICES)
    snapshot = bars.clone()
    first = walk_forward(bars, initial=5, window=4)
    second = walk_forward(bars, initial=5, window=4)
    assert bars.equals(snapshot)
    assert first.aggregate.periods.equals(second.aggregate.periods)
    assert first.aggregate.metrics == second.aggregate.metrics
    assert [w.result.metrics for w in first.windows] == [
        w.result.metrics for w in second.windows
    ]


def test_input_row_order_does_not_change_results() -> None:
    bars = make_bars(MIXED_PRICES)
    shuffled = pl.concat([bars[7:], bars[:7]])
    assert walk_forward(bars, initial=5, window=4).aggregate.periods.equals(
        walk_forward(shuffled, initial=5, window=4).aggregate.periods
    )
    assert holdout(bars, history=5).aggregate.periods.equals(
        holdout(shuffled, history=5).aggregate.periods
    )


def test_result_reports_the_fixed_configurations_and_minimal_surface() -> None:
    from quantforge import validation

    bars = make_bars(MIXED_PRICES)
    result = holdout(bars, history=5)
    assert result.strategy_config == SLOW
    assert result.backtest_config == FREE
    assert set(validation.__all__) == {
        "ChronologicalHoldoutConfig",
        "OOSResult",
        "ValidationResult",
        "ValidationWindow",
        "WalkForwardConfig",
        "run_holdout_validation",
        "run_walk_forward_validation",
    }
    for forbidden in (
        "optimize",
        "search",
        "fit",
        "rank",
        "select",
        "strategy_registry",
        "ValidatorRegistry",
    ):
        assert not hasattr(validation, forbidden)


def test_experiment_store_preserves_validation_provenance(tmp_path: Path) -> None:
    bars = make_bars(MIXED_PRICES)
    validation_config = WalkForwardConfig(initial_history_bars=5, oos_window_bars=4)
    result = run_walk_forward_validation(bars, SLOW, COSTLY, validation_config)
    catalog = ParquetDataCatalog(tmp_path / "catalog")
    last_microseconds = int(bars["timestamp"].cast(pl.Int64).max())
    as_of = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=last_microseconds, days=1
    )
    snapshot = catalog.write(
        "bars.XYZ-1d",
        bars,
        source="validation-test",
        schema_version="1",
        as_of=as_of,
    )
    config_payload = {
        "strategy": dataclasses.asdict(SLOW),
        "backtest": dataclasses.asdict(COSTLY),
        "validation": dataclasses.asdict(validation_config),
    }
    record = ExperimentRecord.create(
        experiment_id="validation-001-run",
        git_commit=COMMIT,
        dataset_id=snapshot.dataset_id,
        dataset_hash=snapshot.content_hash,
        backtest_config=config_payload,
        random_seed=7,
        started_at=datetime(2025, 2, 1, 9, 30, tzinfo=UTC),
        metrics=dataclasses.asdict(result.aggregate.metrics),
        artifact_refs=[
            {"name": "oos-periods", "uri": "artifacts/validation-001/oos-periods.parquet"}
        ],
    )
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    try:
        store.save(record)
    finally:
        store.close()
    reopened = ExperimentStore(tmp_path / "experiments.sqlite")
    try:
        loaded = reopened.get("validation-001-run")
    finally:
        reopened.close()
    assert loaded == record
    assert loaded.dataset_id == snapshot.dataset_id
    assert loaded.dataset_hash == snapshot.content_hash
    assert loaded.backtest_config.to_payload() == config_payload
    assert loaded.config_hash == canonical_config_hash(config_payload)
    assert loaded.metrics.to_payload() == dataclasses.asdict(result.aggregate.metrics)
    assert loaded.replay_descriptor()["dataset_hash"] == snapshot.content_hash


def test_validation_uses_strategy_and_backtest_layers_directly() -> None:
    bars = make_bars(MIXED_PRICES)
    config = WalkForwardConfig(initial_history_bars=5, oos_window_bars=4)
    result = run_walk_forward_validation(bars, SLOW, FREE, config)
    signals = generate_moving_average_signals(bars, SLOW)
    assert signals.columns == list(SIGNAL_COLUMNS)
    expected = run_vectorized_backtest(bars, signals, FREE)
    hold = run_holdout_validation(
        bars, SLOW, FREE, ChronologicalHoldoutConfig(history_bars=5)
    )
    assert hold.aggregate.periods["strategy_return"].to_list() == expected.periods.slice(
        5, bars.height - 5
    )["strategy_return"].to_list()
    assert result.aggregate.periods.height == bars.height - 5
