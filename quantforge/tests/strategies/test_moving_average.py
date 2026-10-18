from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from quantforge.backtest import (
    SIGNAL_COLUMNS,
    SIGNAL_SCHEMA,
    BacktestConfig,
    run_vectorized_backtest,
)
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

START = datetime(2025, 1, 2, tzinfo=UTC)
COMMIT = "25b49b99e3352a6e79ffb044a8115de76d2126a4"
SHORT_LONG = MovingAverageCrossoverConfig(short_window=2, long_window=3)


def make_bars(closes: list[float], *, symbol: str = "XYZ") -> pl.DataFrame:
    timestamps = [START + timedelta(days=index) for index in range(len(closes))]
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(closes),
            "timestamp": timestamps,
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
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


def oracle(closes: list[float], short_window: int, long_window: int) -> list[float]:
    """Independent trailing-window oracle: only closes through ``t`` are used."""
    expected: list[float] = []
    for index in range(len(closes)):
        if index + 1 < long_window:
            expected.append(0.0)
            continue
        short_mean = sum(closes[index - short_window + 1 : index + 1]) / short_window
        long_mean = sum(closes[index - long_window + 1 : index + 1]) / long_window
        expected.append(1.0 if short_mean > long_mean else 0.0)
    return expected


def values(bars: pl.DataFrame, config: MovingAverageCrossoverConfig) -> list[float]:
    return generate_moving_average_signals(bars, config)["value"].to_list()


def test_config_defaults_and_immutability() -> None:
    config = MovingAverageCrossoverConfig()
    assert (config.short_window, config.long_window) == (20, 50)
    assert dataclasses.asdict(MovingAverageCrossoverConfig(2, 3)) == {
        "short_window": 2,
        "long_window": 3,
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.short_window = 5  # type: ignore[misc]


@pytest.mark.parametrize("invalid", [2.0, "3", True, False, None])
@pytest.mark.parametrize("field", ["short_window", "long_window"])
def test_config_rejects_non_integer_windows(field: str, invalid: object) -> None:
    with pytest.raises(TypeError, match=field):
        MovingAverageCrossoverConfig(**{field: invalid})


@pytest.mark.parametrize("invalid", [0, -1])
@pytest.mark.parametrize("field", ["short_window", "long_window"])
def test_config_rejects_non_positive_windows(field: str, invalid: int) -> None:
    arguments: dict[str, int] = {"short_window": 2, "long_window": 3}
    arguments[field] = invalid
    with pytest.raises(ValueError, match=field):
        MovingAverageCrossoverConfig(**arguments)


@pytest.mark.parametrize("windows", [(3, 3), (5, 3), (1, 1)])
def test_config_rejects_short_window_not_below_long_window(windows: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="less than long_window"):
        MovingAverageCrossoverConfig(short_window=windows[0], long_window=windows[1])


def test_fewer_bars_than_long_window_is_flat() -> None:
    assert values(make_bars([2]), SHORT_LONG) == [0.0]
    assert values(make_bars([2, 3]), SHORT_LONG) == [0.0, 0.0]


def test_exactly_long_window_bars_evaluates_the_first_comparison() -> None:
    # At the third bar the long window is exactly full: short mean 3.5 > long mean 3.0.
    assert values(make_bars([2, 3, 4]), SHORT_LONG) == [0.0, 0.0, 1.0]


def test_short_average_above_long_average_is_long() -> None:
    assert values(make_bars([2, 3, 4, 5, 6]), SHORT_LONG) == [0.0, 0.0, 1.0, 1.0, 1.0]


def test_short_average_below_long_average_is_flat() -> None:
    assert values(make_bars([8, 7, 6, 5, 4]), SHORT_LONG) == [0.0, 0.0, 0.0, 0.0, 0.0]


def test_equal_moving_averages_are_flat() -> None:
    assert values(make_bars([5.0, 5.0, 5.0, 5.0]), SHORT_LONG) == [0.0] * 4
    # (3 + 4 + 5) / 3 == (4 + 3 + 4 + 5) / 4 == 4, so the full-window comparison is flat.
    equality = MovingAverageCrossoverConfig(short_window=3, long_window=4)
    assert values(make_bars([4, 3, 4, 5]), equality) == [0.0] * 4


def test_warmup_boundary_matches_independent_oracle() -> None:
    closes = [10, 11, 9, 12, 13, 8, 14, 15, 7, 16, 17, 6]
    config = MovingAverageCrossoverConfig(short_window=3, long_window=5)
    assert values(make_bars(closes), config) == oracle(closes, 3, 5)
    assert values(make_bars(closes), config) == [
        0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0,
    ]


def test_transition_flat_to_long() -> None:
    assert values(make_bars([5, 5, 5, 5, 7, 8, 9, 10]), SHORT_LONG) == [
        0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0,
    ]


def test_transition_long_to_flat() -> None:
    assert values(make_bars([5, 5, 5, 5, 7, 8, 9, 10, 4, 3, 2]), SHORT_LONG) == [
        0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0,
    ]


def test_prolonged_long_state() -> None:
    closes = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    assert values(make_bars(closes), SHORT_LONG) == [0.0, 0.0] + [1.0] * 9


def test_prolonged_flat_state() -> None:
    assert values(make_bars([5.0] * 8), SHORT_LONG) == [0.0] * 8


def test_output_shape_symbols_timestamps_and_values() -> None:
    bars = make_bars([5, 5, 5, 5, 7, 8, 9, 10, 4, 3, 2], symbol="MSFT")
    signals = generate_moving_average_signals(bars, SHORT_LONG)
    assert signals.columns == list(SIGNAL_COLUMNS)
    assert dict(signals.schema) == SIGNAL_SCHEMA
    assert signals.height == bars.height
    assert signals["symbol"].equals(bars["symbol"])
    assert signals["timestamp"].equals(bars["timestamp"])
    assert signals["value"].null_count() == 0
    assert set(signals["value"].to_list()) <= {0.0, 1.0}
    assert signals["value"].is_finite().all()


def test_explicit_default_config_matches_none() -> None:
    bars = make_bars([float(value) for value in range(2, 60)])
    assert generate_moving_average_signals(bars, None).equals(
        generate_moving_average_signals(bars, MovingAverageCrossoverConfig())
    )


def test_deterministic_rerun_equality() -> None:
    bars = make_bars([10, 11, 9, 12, 13, 8, 14, 15, 7, 16, 17, 6])
    first = generate_moving_average_signals(bars, SHORT_LONG)
    second = generate_moving_average_signals(bars.clone(), SHORT_LONG)
    assert first.equals(second)


def test_input_row_order_does_not_change_the_result() -> None:
    closes = [10, 11, 9, 12, 13, 8, 14, 15, 7, 16, 17, 6]
    bars = make_bars(closes)
    shuffled = pl.concat([bars[3:], bars[:3]])
    assert generate_moving_average_signals(bars, SHORT_LONG).equals(
        generate_moving_average_signals(shuffled, SHORT_LONG)
    )


def test_column_order_does_not_change_the_result() -> None:
    bars = make_bars([10, 11, 9, 12, 13, 8, 14, 15, 7, 16, 17, 6])
    reordered = bars.select("close", "symbol", "volume", "open", "high", "low", "timestamp")
    assert generate_moving_average_signals(bars, SHORT_LONG).equals(
        generate_moving_average_signals(reordered, SHORT_LONG)
    )


def test_future_row_mutation_cannot_alter_earlier_signals() -> None:
    closes = [10, 11, 9, 12, 13, 8, 14, 15, 7, 16, 17, 6]
    baseline = values(make_bars(closes), SHORT_LONG)
    mutated = values(make_bars([*closes[:-1], 500.0]), SHORT_LONG)
    assert mutated[:-1] == baseline[:-1]
    assert mutated[-1] != baseline[-1]


def test_future_row_append_cannot_alter_earlier_signals() -> None:
    closes = [10, 11, 9, 12, 13, 8, 14, 15, 7, 16, 17, 6]
    baseline = values(make_bars(closes), SHORT_LONG)
    appended = values(make_bars([*closes, 20.0]), SHORT_LONG)
    assert appended[: len(closes)] == baseline


def test_future_row_removal_cannot_alter_earlier_signals() -> None:
    closes = [10, 11, 9, 12, 13, 8, 14, 15, 7, 16, 17, 6]
    baseline = values(make_bars(closes), SHORT_LONG)
    shortened = values(make_bars(closes[:-1]), SHORT_LONG)
    assert shortened == baseline[:-1]


def test_every_signal_equals_the_signal_recomputed_from_its_own_prefix() -> None:
    closes = [10, 11, 9, 12, 13, 8, 14, 15, 7, 16, 17, 6]
    full = values(make_bars(closes), SHORT_LONG)
    for index in range(1, len(closes) + 1):
        prefix = values(make_bars(closes[:index]), SHORT_LONG)
        assert prefix[-1] == full[index - 1]


def test_multiple_symbols_are_rejected() -> None:
    bars = make_bars([2, 3, 4, 5])
    mixed = pl.concat([bars, bars.with_columns(pl.lit("OTHER").alias("symbol"))])
    with pytest.raises(ValueError, match="exactly one symbol"):
        generate_moving_average_signals(mixed, SHORT_LONG)


def test_empty_bars_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least one bar"):
        generate_moving_average_signals(make_bars([]), SHORT_LONG)


def test_non_dataframe_input_and_wrong_config_type_are_rejected() -> None:
    with pytest.raises(TypeError, match="Polars DataFrame"):
        generate_moving_average_signals([1, 2, 3], SHORT_LONG)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="MovingAverageCrossoverConfig"):
        generate_moving_average_signals(make_bars([2, 3, 4]), {"short_window": 2})  # type: ignore[arg-type]


def test_non_canonical_bars_are_rejected_by_the_data_layer() -> None:
    with pytest.raises(ValueError, match="bar columns"):
        generate_moving_average_signals(make_bars([2, 3, 4]).drop("volume"), SHORT_LONG)
    naive = make_bars([2, 3, 4]).with_columns(pl.col("timestamp").dt.replace_time_zone(None))
    with pytest.raises(ValueError, match="timezone-aware"):
        generate_moving_average_signals(naive, SHORT_LONG)
    with pytest.raises(ValueError, match="finite"):
        generate_moving_average_signals(
            make_bars([2, 3, 4]).with_columns(pl.lit(float("nan")).alias("close")), SHORT_LONG
        )
    with pytest.raises(ValueError, match="nulls"):
        generate_moving_average_signals(
            make_bars([2, 3, 4]).with_columns(pl.lit(None, dtype=pl.Float64).alias("close")),
            SHORT_LONG,
        )
    duplicated = pl.concat([make_bars([2, 3, 4]), make_bars([2, 3, 4]).head(1)])
    with pytest.raises(ValueError, match="duplicate"):
        generate_moving_average_signals(duplicated, SHORT_LONG)


def test_signals_feed_backtest_without_adapters() -> None:
    bars = make_bars([5, 5, 5, 5, 7, 8, 9, 10, 4, 3, 2])
    signals = generate_moving_average_signals(bars, SHORT_LONG)
    assert signals["value"].to_list() == [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]
    result = run_vectorized_backtest(bars, signals, BacktestConfig(initial_cash=1000.0, fee_bps=10.0))
    periods = result.periods
    assert periods.height == bars.height
    # BACKTEST-001, not the strategy, performs the signal[t] -> open[t+1] shift.
    assert periods["position_before_open"][0] == 0.0
    assert periods["position_after_open"][0] == 0.0
    assert periods["turnover"][0] == 0.0
    for index in range(1, bars.height):
        assert periods["position_after_open"][index] == signals["value"][index - 1]


def test_final_signal_cannot_execute_without_a_following_bar() -> None:
    bars = make_bars([5, 5, 5, 5, 7])
    signals = generate_moving_average_signals(bars, SHORT_LONG)
    assert signals["value"].to_list() == [0.0, 0.0, 0.0, 0.0, 1.0]
    result = run_vectorized_backtest(bars, signals)
    # The last signal has no following open, so the last position reflects signal[-2] only.
    assert result.periods["position_after_open"][-1] == 0.0


def test_end_to_end_strategy_to_backtest_is_deterministic() -> None:
    bars = make_bars([10, 11, 9, 12, 13, 8, 14, 15, 7, 16, 17, 6])
    config = MovingAverageCrossoverConfig(short_window=3, long_window=5)
    first = run_vectorized_backtest(bars, generate_moving_average_signals(bars, config))
    second = run_vectorized_backtest(bars, generate_moving_average_signals(bars, config))
    assert first.periods.equals(second.periods)
    assert first.metrics == second.metrics


def test_existing_experiment_record_preserves_run_provenance(tmp_path: Path) -> None:
    bars = make_bars([5, 5, 5, 5, 7, 8, 9, 10, 4, 3, 2])
    config = MovingAverageCrossoverConfig(short_window=2, long_window=3)
    signals = generate_moving_average_signals(bars, config)
    result = run_vectorized_backtest(bars, signals, BacktestConfig(initial_cash=1000.0))

    catalog = ParquetDataCatalog(tmp_path / "catalog")
    # Compare microseconds instead of converting the timezone-aware column to
    # Python datetimes, which would require the optional ``tzdata`` package.
    last_microseconds = int(bars["timestamp"].cast(pl.Int64).max())
    as_of = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=last_microseconds, days=1
    )
    snapshot = catalog.write(
        "bars.XYZ-1d",
        bars,
        source="strategy-test",
        schema_version="1",
        as_of=as_of,
    )
    config_payload = dataclasses.asdict(config)
    record = ExperimentRecord.create(
        experiment_id="strategy-001-baseline",
        git_commit=COMMIT,
        dataset_id=snapshot.dataset_id,
        dataset_hash=snapshot.content_hash,
        backtest_config=config_payload,
        random_seed=7,
        started_at=datetime(2025, 2, 1, 9, 30, tzinfo=UTC),
        metrics=dataclasses.asdict(result.metrics),
        artifact_refs=[{"name": "signals", "uri": "artifacts/strategy-001/signals.parquet"}],
    )
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    try:
        store.save(record)
    finally:
        store.close()

    reopened = ExperimentStore(tmp_path / "experiments.sqlite")
    try:
        loaded = reopened.get("strategy-001-baseline")
    finally:
        reopened.close()
    assert loaded == record
    assert loaded.git_commit == COMMIT
    assert loaded.dataset_id == snapshot.dataset_id
    assert loaded.dataset_hash == snapshot.content_hash
    assert loaded.backtest_config.to_payload() == config_payload
    assert loaded.config_hash == canonical_config_hash(config_payload)
    assert loaded.metrics.to_payload() == dataclasses.asdict(result.metrics)
    descriptor = loaded.replay_descriptor()
    assert descriptor["dataset_hash"] == snapshot.content_hash
    assert descriptor["backtest_config"] == config_payload


def test_public_surface_stays_minimal() -> None:
    from quantforge import strategies

    assert set(strategies.__all__) == {
        "MovingAverageCrossoverConfig",
        "generate_moving_average_signals",
    }
    for forbidden in ("StrategyRegistry", "register", "Strategy", "load_strategy"):
        assert not hasattr(strategies, forbidden)
