from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from quantforge.data import ParquetDataCatalog
from quantforge.factors import (
    FACTOR_COLUMNS,
    FACTOR_SCHEMA,
    MomentumFactorConfig,
    compute_momentum_factor,
)

START = datetime(2025, 1, 2, tzinfo=UTC)
CONFIG = MomentumFactorConfig(lookback=2)
SERIES = [100.0, 110.0, 121.0, 108.9, 130.0, 90.0, 90.0, 180.0]


def make_bars(closes: list[float], *, symbol: str = "XYZ") -> pl.DataFrame:
    """Daily canonical bars with open == close and a +/-1 high/low band."""
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


def make_flat_bars(closes: list[float], *, symbol: str = "XYZ") -> pl.DataFrame:
    """Bars where open == high == low == close, for extreme finite values."""
    timestamps = [START + timedelta(days=index) for index in range(len(closes))]
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(closes),
            "timestamp": timestamps,
            "open": closes,
            "high": closes,
            "low": closes,
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


def factor_values(
    bars: pl.DataFrame, config: MomentumFactorConfig = CONFIG
) -> list[float | None]:
    return compute_momentum_factor(bars, config)["value"].to_list()


def oracle(closes: list[float], lookback: int) -> list[float | None]:
    expected: list[float | None] = []
    for index in range(len(closes)):
        if index < lookback:
            expected.append(None)
        else:
            expected.append(closes[index] / closes[index - lookback] - 1.0)
    return expected


def test_config_defaults_and_immutability() -> None:
    config = MomentumFactorConfig()
    assert config.lookback == 20
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.lookback = 5  # type: ignore[misc]


@pytest.mark.parametrize("invalid", [True, False])
def test_boolean_lookback_is_rejected(invalid: bool) -> None:
    with pytest.raises(TypeError, match="lookback"):
        MomentumFactorConfig(lookback=invalid)


@pytest.mark.parametrize("invalid", [2.0, "2", None, [2]])
def test_non_integer_lookback_is_rejected(invalid: object) -> None:
    with pytest.raises(TypeError, match="lookback"):
        MomentumFactorConfig(lookback=invalid)


@pytest.mark.parametrize("invalid", [0, -1, -20])
def test_non_positive_lookback_is_rejected(invalid: int) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        MomentumFactorConfig(lookback=invalid)


def test_fewer_bars_than_lookback_are_all_null() -> None:
    assert factor_values(make_bars([100.0])) == [None]
    assert factor_values(make_bars([100.0, 110.0]), MomentumFactorConfig(lookback=5)) == [
        None,
        None,
    ]


def test_exactly_lookback_bars_have_no_available_factor() -> None:
    assert factor_values(make_bars([100.0, 110.0])) == [None, None]
    assert factor_values(make_bars([100.0, 110.0, 121.0]), MomentumFactorConfig(lookback=3)) == [
        None,
        None,
        None,
    ]


def test_lookback_plus_one_bars_produces_the_first_factor_at_index_lookback() -> None:
    # This is the off-by-one boundary: index 2 with lookback 2 divides by close[0].
    values = factor_values(make_bars([100.0, 110.0, 121.0]))
    assert values[0] is None
    assert values[1] is None
    assert values[2] == pytest.approx(121.0 / 100.0 - 1.0)


def test_null_mask_is_exactly_the_warmup_prefix() -> None:
    bars = make_bars(SERIES)
    factor = compute_momentum_factor(bars, MomentumFactorConfig(lookback=3))
    nulls = factor["value"].is_null().to_list()
    assert nulls == [True, True, True] + [False] * (bars.height - 3)


def test_contract_example_is_reproduced() -> None:
    values = factor_values(make_bars([100.0, 110.0, 121.0, 108.9]))
    assert values[0] is None
    assert values[1] is None
    assert values[2] == pytest.approx(0.21)
    assert values[3] == pytest.approx(-0.01)


def test_exact_binary_momentum_values() -> None:
    single = MomentumFactorConfig(lookback=1)
    assert factor_values(make_bars([100.0, 200.0, 100.0, 400.0]), single) == [
        None,
        1.0,
        -0.5,
        3.0,
    ]


def test_negative_momentum_is_accepted() -> None:
    values = factor_values(make_bars([100.0, 50.0]), MomentumFactorConfig(lookback=1))
    assert values == [None, -0.5]


def test_zero_momentum_is_accepted() -> None:
    values = factor_values(make_bars([100.0, 100.0]), MomentumFactorConfig(lookback=1))
    assert values == [None, 0.0]


def test_factor_values_are_not_constrained_to_unit_interval() -> None:
    values = factor_values(
        make_bars([100.0, 400.0, 25.0]), MomentumFactorConfig(lookback=1)
    )
    assert values[1] == 3.0
    assert values[2] == pytest.approx(25.0 / 400.0 - 1.0)
    assert set(values[1:]) - {0.0, 1.0}


def test_denominator_is_close_at_t_minus_lookback() -> None:
    closes = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    lookback = 3
    values = factor_values(make_bars(closes), MomentumFactorConfig(lookback=lookback))
    for index in range(len(closes)):
        if index < lookback:
            assert values[index] is None
        else:
            assert values[index] == pytest.approx(
                closes[index] / closes[index - lookback] - 1.0
            )
    assert values == oracle(closes, lookback)


def test_output_shape_schema_and_columns() -> None:
    bars = make_bars(SERIES)
    factor = compute_momentum_factor(bars, CONFIG)
    assert factor.columns == list(FACTOR_COLUMNS)
    assert dict(factor.schema) == FACTOR_SCHEMA
    assert factor.height == bars.height


def test_symbols_and_timestamps_are_preserved() -> None:
    bars = make_bars(SERIES, symbol="MSFT")
    factor = compute_momentum_factor(bars, CONFIG)
    assert factor["symbol"].equals(bars["symbol"])
    assert factor["timestamp"].cast(pl.Int64).to_list() == bars["timestamp"].cast(
        pl.Int64
    ).to_list()


def test_available_values_are_finite_and_ordered_chronologically() -> None:
    bars = make_bars(SERIES)
    factor = compute_momentum_factor(bars, CONFIG)
    available = factor.filter(pl.col("value").is_not_null())
    assert available.height == bars.height - 2
    assert available.select(pl.col("value").is_finite().all()).item()
    timestamps = factor["timestamp"].cast(pl.Int64).to_list()
    assert timestamps == sorted(timestamps)


def test_derived_non_finite_values_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from quantforge.factors import momentum

    # The data layer rejects non-finite prices, so bypass it here to exercise
    # the factor's own derived-value guard.
    monkeypatch.setattr(momentum, "validate_bars", lambda *args, **kwargs: None)
    for bad in (float("nan"), float("inf"), float("-inf")):
        bars = make_flat_bars([2.0, bad])
        with pytest.raises(ValueError, match="non-finite"):
            compute_momentum_factor(bars, MomentumFactorConfig(lookback=1))


def test_overflowing_derived_value_is_rejected() -> None:
    # Finite inputs whose ratio overflows Float64 must be rejected, not stored.
    bars = make_flat_bars([1e-300, 1e300])
    with pytest.raises(ValueError, match="non-finite"):
        compute_momentum_factor(bars, MomentumFactorConfig(lookback=1))


def test_extreme_but_finite_values_are_accepted() -> None:
    bars = make_flat_bars([2.0, 1e308])
    values = factor_values(bars, MomentumFactorConfig(lookback=1))
    assert values[0] is None
    assert values[1] == pytest.approx(5e307)


def test_future_row_mutation_cannot_alter_earlier_values() -> None:
    baseline = factor_values(make_bars(SERIES))
    mutated = factor_values(make_bars([*SERIES[:-1], 12.5]))
    assert mutated[:-1] == baseline[:-1]
    assert mutated[-1] != baseline[-1]


def test_future_row_append_cannot_alter_earlier_values() -> None:
    baseline = factor_values(make_bars(SERIES))
    appended = factor_values(make_bars([*SERIES, 42.0]))
    assert appended[: len(SERIES)] == baseline


def test_future_row_removal_cannot_alter_earlier_values() -> None:
    baseline = factor_values(make_bars(SERIES))
    shortened = factor_values(make_bars(SERIES[:-1]))
    assert shortened == baseline[:-1]


def test_future_row_replacement_cannot_alter_earlier_values() -> None:
    baseline = factor_values(make_bars(SERIES))
    replaced = factor_values(make_bars([*SERIES[:-2], 7.0, 3.0]))
    assert replaced[:-2] == baseline[:-2]
    assert replaced[-2:] != baseline[-2:]


def test_every_prefix_recomputation_agrees_with_the_full_series() -> None:
    bars = make_bars(SERIES)
    full = factor_values(bars)
    for cutoff in range(1, bars.height + 1):
        prefix = factor_values(bars.head(cutoff))
        assert prefix == full[:cutoff]


def test_input_row_order_does_not_change_the_result() -> None:
    bars = make_bars(SERIES)
    shuffled = pl.concat([bars[5:], bars[:5]])
    assert compute_momentum_factor(bars, CONFIG).equals(
        compute_momentum_factor(shuffled, CONFIG)
    )


def test_input_column_order_does_not_change_the_result() -> None:
    bars = make_bars(SERIES)
    reordered = bars.select("close", "volume", "symbol", "timestamp", "open", "low", "high")
    assert compute_momentum_factor(bars, CONFIG).equals(
        compute_momentum_factor(reordered, CONFIG)
    )


def test_input_dataframe_is_not_mutated() -> None:
    bars = make_bars(SERIES)
    snapshot = bars.clone()
    compute_momentum_factor(bars, CONFIG)
    assert bars.equals(snapshot)


def test_deterministic_rerun_equality() -> None:
    bars = make_bars(SERIES)
    assert compute_momentum_factor(bars, CONFIG).equals(
        compute_momentum_factor(bars.clone(), CONFIG)
    )
    assert compute_momentum_factor(bars, None).equals(
        compute_momentum_factor(bars, MomentumFactorConfig())
    )


def test_multiple_symbols_are_rejected() -> None:
    bars = make_bars(SERIES)
    mixed = pl.concat([bars, bars.with_columns(pl.lit("OTHER").alias("symbol"))])
    with pytest.raises(ValueError, match="exactly one symbol"):
        compute_momentum_factor(mixed)


def test_empty_bars_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least one bar"):
        compute_momentum_factor(make_bars([]))


def test_wrong_argument_types_are_rejected() -> None:
    with pytest.raises(TypeError, match="Polars DataFrame"):
        compute_momentum_factor([1.0, 2.0])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="MomentumFactorConfig"):
        compute_momentum_factor(make_bars(SERIES), {"lookback": 2})  # type: ignore[arg-type]


def test_non_canonical_bars_are_rejected_by_the_data_layer() -> None:
    with pytest.raises(ValueError, match="bar columns"):
        compute_momentum_factor(make_bars(SERIES).drop("volume"))
    naive = make_bars(SERIES).with_columns(pl.col("timestamp").dt.replace_time_zone(None))
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_momentum_factor(naive)
    with pytest.raises(ValueError, match="finite"):
        compute_momentum_factor(
            make_bars(SERIES).with_columns(pl.lit(float("nan")).alias("close"))
        )
    with pytest.raises(ValueError, match="nulls"):
        compute_momentum_factor(
            make_bars(SERIES).with_columns(pl.lit(None, dtype=pl.Float64).alias("close"))
        )
    duplicated = pl.concat([make_bars(SERIES), make_bars(SERIES).head(1)])
    with pytest.raises(ValueError, match="duplicate"):
        compute_momentum_factor(duplicated)


def test_catalog_read_path_matches_in_memory_factor(tmp_path: Path) -> None:
    bars = make_bars(SERIES, symbol="AAPL")
    catalog = ParquetDataCatalog(tmp_path / "catalog")
    last_microseconds = int(bars["timestamp"].cast(pl.Int64).max())
    as_of = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=last_microseconds, days=1
    )
    snapshot = catalog.write(
        "bars.AAPL-1d",
        bars,
        source="factor-test",
        schema_version="1",
        as_of=as_of,
    )
    read_back = catalog.read(snapshot.dataset_id)
    assert read_back.equals(bars)
    assert compute_momentum_factor(read_back, CONFIG).equals(
        compute_momentum_factor(bars, CONFIG)
    )


def test_public_surface_is_minimal() -> None:
    from quantforge import factors

    assert set(factors.__all__) == {
        "FACTOR_COLUMNS",
        "FACTOR_SCHEMA",
        "MomentumFactorConfig",
        "compute_momentum_factor",
    }
    for forbidden in (
        "FactorRegistry",
        "register",
        "Factor",
        "feature_store",
        "cache",
    ):
        assert not hasattr(factors, forbidden)


def test_factor_layer_does_not_depend_on_lower_layers() -> None:
    from quantforge.factors import momentum

    source = Path(momentum.__file__).read_text(encoding="utf-8")
    forbidden = re.compile(
        r"^\s*(from|import)\s+.*(strategies|backtest|validation|experiments|"
        r"portfolio|execution|risk|models)",
        re.MULTILINE,
    )
    assert forbidden.search(source) is None
