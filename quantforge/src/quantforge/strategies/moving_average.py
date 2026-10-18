from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import polars as pl

from ..backtest import SIGNAL_COLUMNS, SIGNAL_SCHEMA
from ..data import canonicalize_bars, validate_bars


@dataclass(frozen=True)
class MovingAverageCrossoverConfig:
    """Immutable configuration for the deterministic moving-average baseline.

    ``short_window`` and ``long_window`` are trailing simple-moving-average
    window lengths over close prices.  The pair is validated once so the
    signal generator never has to reason about degenerate windows.
    """

    short_window: int = 20
    long_window: int = 50

    def __post_init__(self) -> None:
        for name, window in (
            ("short_window", self.short_window),
            ("long_window", self.long_window),
        ):
            if isinstance(window, bool) or not isinstance(window, int):
                raise TypeError(f"{name} must be an integer")
        if self.short_window <= 0:
            raise ValueError("short_window must be greater than zero")
        if self.long_window <= 0:
            raise ValueError("long_window must be greater than zero")
        if self.short_window >= self.long_window:
            raise ValueError("short_window must be less than long_window")


def _single_symbol_bars(bars: pl.DataFrame) -> pl.DataFrame:
    """Canonicalize and validate the single-symbol research boundary.

    The data layer stays authoritative: this helper reuses
    ``canonicalize_bars`` and ``validate_bars`` instead of reimplementing any
    market-data rule, and it rejects the multi-symbol shapes that BACKTEST-001
    also refuses.
    """
    if not isinstance(bars, pl.DataFrame):
        raise TypeError("bars must be a Polars DataFrame")
    canonical = canonicalize_bars(bars)
    if canonical.is_empty():
        raise ValueError("at least one bar is required")
    # Compare the underlying microsecond representation instead of converting
    # the timezone-aware column to Python datetimes, which would require the
    # optional ``tzdata`` package on minimal installations.
    maximum_microseconds = cast(int | None, canonical["timestamp"].cast(pl.Int64).max())
    if maximum_microseconds is None:
        raise ValueError("bar timestamps must not be null")
    try:
        as_of = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            microseconds=int(maximum_microseconds)
        )
    except (OverflowError, ValueError) as exc:
        raise ValueError("bar timestamps are outside the representable UTC range") from exc
    validate_bars(canonical, as_of=as_of)
    if canonical.get_column("symbol").n_unique() != 1:
        raise ValueError("STRATEGY-001 supports exactly one symbol")
    return canonical


def generate_moving_average_signals(
    bars: pl.DataFrame,
    config: MovingAverageCrossoverConfig | None = None,
) -> pl.DataFrame:
    """Generate the deterministic point-in-time moving-average state signal.

    The rule is a trailing simple moving average of close prices through and
    including each bar: the signal is ``1.0`` while the short window averages
    above the long window, and ``0.0`` while it is below or equal, during
    warm-up, and for every bar before ``long_window`` observations exist.

    Signal[t] depends only on market information through ``close[t]``.  The
    execution shift (``signal[t]`` traded at ``open[t+1]``) stays entirely in
    BACKTEST-001; this layer emits the state sequence unchanged.
    """
    selected_config = MovingAverageCrossoverConfig() if config is None else config
    if not isinstance(selected_config, MovingAverageCrossoverConfig):
        raise TypeError("config must be a MovingAverageCrossoverConfig")
    canonical = _single_symbol_bars(bars)

    closes = pl.col("close")
    short_average = closes.rolling_mean(
        window_size=selected_config.short_window,
        min_samples=selected_config.short_window,
    )
    long_average = closes.rolling_mean(
        window_size=selected_config.long_window,
        min_samples=selected_config.long_window,
    )
    signals = canonical.select(
        pl.col("symbol"),
        pl.col("timestamp"),
        pl.when(short_average > long_average)
        .then(pl.lit(1.0))
        .otherwise(pl.lit(0.0))
        .cast(pl.Float64)
        .alias("value"),
    )
    if dict(signals.schema) != SIGNAL_SCHEMA:
        raise RuntimeError("strategy output does not match the BACKTEST-001 signal schema")
    return signals.select(SIGNAL_COLUMNS)
