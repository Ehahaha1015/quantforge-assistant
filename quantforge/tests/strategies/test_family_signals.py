from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal, Inexact, localcontext
from typing import Any

import polars as pl
import pytest

from quantforge.backtest import SIGNAL_COLUMNS, SIGNAL_SCHEMA
from quantforge.data import BAR_SCHEMA
from quantforge.strategies.bollinger_reversion import (
    BollingerReversionConfig,
    generate_bollinger_reversion_signals,
)
from quantforge.strategies.donchian_breakout import (
    DonchianBreakoutConfig,
    generate_donchian_breakout_signals,
)
from quantforge.strategies.models import (
    generate_strategy_signals,
    required_observations,
)
from quantforge.strategies.time_series_momentum import (
    TimeSeriesMomentumConfig,
    generate_time_series_momentum_signals,
)


def bars(closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["X"] * len(closes),
            "timestamp": [
                datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i)
                for i in range(len(closes))
            ],
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        schema=BAR_SCHEMA,
    )


CASES = [
    (DonchianBreakoutConfig(3, 2), generate_donchian_breakout_signals),
    (
        BollingerReversionConfig(3, Decimal("100.0")),
        generate_bollinger_reversion_signals,
    ),
    (
        TimeSeriesMomentumConfig(3, Decimal("0.0")),
        generate_time_series_momentum_signals,
    ),
]


def test_donchian_hand_calculated_state_strict_equality_and_exit() -> None:
    frame = bars([10, 11, 10, 12, 13, 12, 9, 9, 14])
    assert generate_donchian_breakout_signals(frame, DonchianBreakoutConfig(3, 2))[
        "value"
    ].to_list() == [0, 0, 0, 1, 1, 1, 0, 0, 1]


def test_donchian_uses_prior_ohlc_extrema_and_excludes_current_high_low() -> None:
    frame = bars([10, 10, 11, 14, 9, 4]).with_columns(
        pl.Series("high", [12.0, 12.0, 12.0, 100.0, 9.0, 4.0]),
        pl.Series("low", [5.0, 5.0, 5.0, 5.0, 5.0, 1.0]),
    )
    # At t2, close 11 beats prior closes but cannot beat prior highs 12.
    # At t3, own high 100 is excluded; at t4, prior low 5 prevents an exit.
    assert generate_donchian_breakout_signals(frame, DonchianBreakoutConfig(2, 1))[
        "value"
    ].to_list() == [0, 0, 0, 1, 1, 0]


def test_bollinger_hand_calculated_entry_hold_and_mean_exit() -> None:
    frame = bars([10, 12, 8, 9, 11, 10])
    assert generate_bollinger_reversion_signals(
        frame, BollingerReversionConfig(3, Decimal(100))
    )["value"].to_list() == [0, 0, 1, 1, 0, 0]


def test_bollinger_population_variance_inclusive_bound_and_percent_units() -> None:
    # For [12, 8], population mean=10, standard deviation=2 and lower=8.
    frame = bars([12, 8, 12])
    assert generate_bollinger_reversion_signals(
        frame, BollingerReversionConfig(2, Decimal(100))
    )["value"].to_list() == [0, 1, 0]
    assert generate_bollinger_reversion_signals(
        frame, BollingerReversionConfig(2, Decimal(200))
    )["value"].to_list() == [0, 0, 0]
    assert generate_bollinger_reversion_signals(
        frame, BollingerReversionConfig(2, Decimal("2.0"))
    )["value"].to_list() == [0, 1, 0]


def test_momentum_hand_calculated_lag_exact_threshold_and_absolute_filter() -> None:
    frame = bars([100, 100, 110, 111, 100, 99])
    assert generate_time_series_momentum_signals(
        frame, TimeSeriesMomentumConfig(2, Decimal("10.0"))
    )["value"].to_list() == [0, 0, 0, 1, 0, 0]
    assert (
        generate_time_series_momentum_signals(
            bars([10, 9, 8, 7, 6]), TimeSeriesMomentumConfig(2, Decimal(0))
        )["value"].to_list()
        == [0] * 5
    )


@pytest.mark.parametrize(("config", "generate"), CASES)
def test_family_schema_warmup_determinism_input_order_and_immutability(
    config: Any, generate: Any
) -> None:
    frame = bars([10, 12, 8, 9, 11, 13, 10, 15])
    before = frame.clone()
    output = generate(frame, config)
    assert output.columns == list(SIGNAL_COLUMNS)
    assert dict(output.schema) == SIGNAL_SCHEMA
    assert set(output["value"]) <= {0.0, 1.0}
    assert output["value"].head(required_observations(config) - 1).to_list() == [
        0.0
    ] * (required_observations(config) - 1)
    assert generate(frame.head(required_observations(config) - 1), config)[
        "value"
    ].to_list() == [0.0] * (required_observations(config) - 1)
    assert generate(frame, config).equals(output)
    assert generate(frame.reverse(), config).equals(output)
    assert generate_strategy_signals(frame, config).equals(output)
    assert frame.equals(before)
    with pytest.raises(FrozenInstanceError):
        config.test_field = 1


@pytest.mark.parametrize(("config", "generate"), CASES)
def test_future_perturbations_cannot_change_any_earlier_signal(
    config: Any, generate: Any
) -> None:
    frame = bars([10, 12, 8, 9, 11, 13, 10, 15, 12, 18, 8, 21])
    original = generate(frame, config)
    for boundary in range(1, frame.height):
        changed = frame.with_columns(
            [
                pl.when(pl.int_range(pl.len()) >= boundary)
                .then(pl.col(c) * 17)
                .otherwise(pl.col(c))
                .alias(c)
                for c in ("open", "high", "low", "close")
            ]
        )
        assert generate(changed, config).head(boundary).equals(original.head(boundary))
        assert generate(frame.head(boundary), config).equals(original.head(boundary))


@pytest.mark.parametrize(("config", "generate"), CASES)
def test_constant_price_negative_control_stays_flat(config: Any, generate: Any) -> None:
    assert generate(bars([100.0] * 30), config)["value"].to_list() == [0.0] * 30


@pytest.mark.parametrize(("config", "generate"), CASES)
def test_shared_market_data_boundary_fails_closed(config: Any, generate: Any) -> None:
    frame = bars([10, 12, 8, 9, 11])
    for invalid in (
        frame.head(0),
        frame.drop("volume"),
        frame.with_columns(pl.lit(float("nan")).alias("close")),
        frame.with_columns(pl.lit(-1.0).alias("volume")),
        pl.concat([frame, frame.head(1)]),
        pl.concat([frame, frame.with_columns(pl.lit("OTHER").alias("symbol"))]),
    ):
        with pytest.raises(ValueError):
            generate(invalid, config)
    with pytest.raises(TypeError):
        generate([1, 2, 3], config)
    with pytest.raises(TypeError):
        generate(frame, object())


@pytest.mark.parametrize(("config", "generate"), CASES)
def test_decimal_context_does_not_control_family_results(
    config: Any, generate: Any
) -> None:
    frame = bars([100.2, 101.3, 98.7, 96.2, 102.9, 100.1, 104.7])
    expected = generate(frame, config)
    with localcontext() as arithmetic:
        arithmetic.prec = 3
        arithmetic.rounding = ROUND_UP
        arithmetic.traps[Inexact] = True
        assert generate(frame, config).equals(expected)
        assert arithmetic.prec == 3 and arithmetic.traps[Inexact]


@pytest.mark.parametrize(
    ("factory", "fields"),
    [
        (DonchianBreakoutConfig, {"entry_window": 3, "exit_window": 2}),
        (BollingerReversionConfig, {"window": 3, "k_pct": Decimal(100)}),
        (TimeSeriesMomentumConfig, {"lookback": 3, "threshold_pct": Decimal(0)}),
    ],
)
def test_config_exact_types_and_window_boundaries(
    factory: Any, fields: dict[str, Any]
) -> None:
    for name, current in fields.items():
        if isinstance(current, int):
            for invalid in (True, 2.0, "2", None):
                with pytest.raises(TypeError):
                    factory(**{**fields, name: invalid})
            for invalid in (0, -1, 253):
                with pytest.raises(ValueError):
                    factory(**{**fields, name: invalid})
        else:
            for invalid in ("2.0", 2.0, 2, True, None):
                with pytest.raises(TypeError):
                    factory(**{**fields, name: invalid})
            for invalid in (
                Decimal("NaN"),
                Decimal("Infinity"),
                Decimal("-0"),
                Decimal(-1),
                Decimal("0.00001"),
                Decimal(1001),
            ):
                with pytest.raises(ValueError):
                    factory(**{**fields, name: invalid})
    with pytest.raises(ValueError):
        DonchianBreakoutConfig(3, 3)
    with pytest.raises(ValueError):
        BollingerReversionConfig(2, Decimal(0))
    with pytest.raises(ValueError):
        TimeSeriesMomentumConfig(2, Decimal("100.0001"))
