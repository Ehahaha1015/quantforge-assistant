from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import cast

import polars as pl

from quantforge.data.bars import canonicalize_bars, validate_bars

from .models import BacktestConfig, BacktestMetrics, BacktestResult

BAR_KEYS = ("symbol", "timestamp")
SIGNAL_COLUMNS = ("symbol", "timestamp", "value")
SIGNAL_SCHEMA = {
    "symbol": pl.String,
    "timestamp": pl.Datetime("us", "UTC"),
    "value": pl.Float64,
}
PERIOD_COLUMNS = (
    "symbol",
    "timestamp",
    "signal_value",
    "position_before_open",
    "position_after_open",
    "overnight_return",
    "intraday_return",
    "turnover",
    "cost_fraction",
    "strategy_return",
    "equity",
)


def _canonical_bars(bars: pl.DataFrame) -> pl.DataFrame:
    if not isinstance(bars, pl.DataFrame):
        raise TypeError("bars must be a Polars DataFrame")
    canonical = canonicalize_bars(bars)
    if canonical.is_empty():
        raise ValueError("at least one bar is required")
    # Avoid converting a Polars timezone-aware Series to Python datetimes here.
    # On minimal Python installations (including the CI image), the optional
    # ``tzdata`` package may be absent even though UTC timestamps are valid.
    # Comparing the underlying microsecond representation keeps validation
    # deterministic and does not require a system timezone database.
    maximum_microseconds = cast(int | None, canonical["timestamp"].cast(pl.Int64).max())
    if maximum_microseconds is None:
        raise ValueError("bar timestamps must not be null")
    try:
        maximum = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(maximum_microseconds))
    except (OverflowError, ValueError) as exc:
        raise ValueError("bar timestamps are outside the representable UTC range") from exc
    validate_bars(canonical, as_of=maximum)
    symbols = canonical.get_column("symbol").unique().to_list()
    if len(symbols) != 1:
        raise ValueError("BACKTEST-001 supports exactly one symbol")
    return canonical


def _validate_signals(signals: pl.DataFrame, bars: pl.DataFrame) -> pl.DataFrame:
    if not isinstance(signals, pl.DataFrame):
        raise TypeError("signals must be a Polars DataFrame")
    if set(signals.columns) != set(SIGNAL_COLUMNS) or any(
        signals.schema[name] != dtype for name, dtype in SIGNAL_SCHEMA.items()
    ):
        raise ValueError("signals must use the canonical symbol/timestamp/value Float64 schema")
    signals = signals.select(SIGNAL_COLUMNS)
    if signals.height != bars.height:
        raise ValueError("signals must contain exactly one row for every bar")
    if signals.select(pl.any_horizontal(pl.all().is_null()).any()).item():
        raise ValueError("signals must not contain nulls")
    if not signals.select(pl.col("value").is_finite().all()).item():
        raise ValueError("signal values must be finite")
    if not signals.select(pl.col("value").is_in([0.0, 1.0]).all()).item():
        raise ValueError("signal values must be exactly 0.0 or 1.0")
    if signals.select(pl.struct(BAR_KEYS).is_duplicated().any()).item():
        raise ValueError("duplicate (symbol, timestamp) signal key")

    ordered = signals.sort(list(BAR_KEYS))
    bar_keys = bars.select(list(BAR_KEYS))
    if not ordered.select(list(BAR_KEYS)).equals(bar_keys):
        raise ValueError("signals must match bar symbols and timestamps exactly")
    return ordered


def _sample_std(values: Iterable[float]) -> float:
    values_list = list(values)
    if len(values_list) < 2:
        return 0.0
    mean = math.fsum(values_list) / len(values_list)
    variance = math.fsum((value - mean) ** 2 for value in values_list) / (len(values_list) - 1)
    return math.sqrt(variance)


def _metrics(strategy_returns: list[float], equity: list[float], config: BacktestConfig, turnover: list[float]) -> BacktestMetrics:
    executable_returns = strategy_returns[1:]
    volatility = _sample_std(executable_returns) * math.sqrt(config.periods_per_year)
    if volatility == 0.0:
        sharpe = 0.0
    else:
        mean_return = math.fsum(executable_returns) / len(executable_returns)
        sharpe = mean_return / _sample_std(executable_returns) * math.sqrt(config.periods_per_year)
    running_peak = config.initial_cash
    drawdowns: list[float] = []
    for value in equity:
        running_peak = max(running_peak, value)
        drawdowns.append(value / running_peak - 1.0)
    return BacktestMetrics(
        total_return=equity[-1] / config.initial_cash - 1.0,
        annualized_volatility=volatility,
        sharpe=sharpe,
        max_drawdown=min(drawdowns),
        turnover=math.fsum(turnover),
    )


def run_vectorized_backtest(
    bars: pl.DataFrame,
    signals: pl.DataFrame,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Run the long-only next-open strategy calculation without external I/O."""
    selected_config = BacktestConfig() if config is None else config
    if not isinstance(selected_config, BacktestConfig):
        raise TypeError("config must be a BacktestConfig")
    canonical_bars = _canonical_bars(bars)
    ordered_signals = _validate_signals(signals, canonical_bars)
    symbol = canonical_bars.get_column("symbol")[0]
    timestamps = canonical_bars.get_column("timestamp")
    closes = [float(value) for value in canonical_bars.get_column("close").to_list()]
    opens = [float(value) for value in canonical_bars.get_column("open").to_list()]
    signal_values = [float(value) for value in ordered_signals.get_column("value").to_list()]
    count = canonical_bars.height

    position_before = [0.0] * count
    position_after = [0.0] * count
    if count > 1:
        position_after[1:] = signal_values[:-1]
        position_before[1:] = position_after[:-1]
    overnight = [0.0] * count
    intraday = [0.0] * count
    if count > 1:
        overnight[1:] = [opens[index] / closes[index - 1] - 1.0 for index in range(1, count)]
        intraday[1:] = [
            float(canonical_bars.get_column("close")[index]) / opens[index] - 1.0
            for index in range(1, count)
        ]
    if not all(math.isfinite(value) for value in (*overnight, *intraday)):
        raise ValueError("derived returns are not finite")
    turnover = [abs(after - before) for before, after in zip(position_before, position_after, strict=True)]
    cost_fraction = [value * selected_config.fee_bps / 10000.0 for value in turnover]
    period_factor = [
        (1.0 + before * overnight_return)
        * (1.0 - cost)
        * (1.0 + after * intraday_return)
        for before, overnight_return, cost, after, intraday_return in zip(
            position_before, overnight, cost_fraction, position_after, intraday, strict=True
        )
    ]
    if not all(math.isfinite(value) for value in period_factor):
        raise ValueError("derived period factors are not finite")
    strategy_returns = [factor - 1.0 for factor in period_factor]
    equity = [selected_config.initial_cash]
    for factor in period_factor[1:]:
        equity.append(equity[-1] * factor)
    if not all(math.isfinite(value) for value in equity):
        raise ValueError("derived equity values are not finite")
    periods = pl.DataFrame(
        {
            "symbol": [symbol] * count,
            "timestamp": timestamps,
            "signal_value": signal_values,
            "position_before_open": position_before,
            "position_after_open": position_after,
            "overnight_return": overnight,
            "intraday_return": intraday,
            "turnover": turnover,
            "cost_fraction": cost_fraction,
            "strategy_return": strategy_returns,
            "equity": equity,
        },
        schema={
            "symbol": pl.String,
            "timestamp": pl.Datetime("us", "UTC"),
            **{name: pl.Float64 for name in PERIOD_COLUMNS[2:]},
        },
    ).select(PERIOD_COLUMNS)
    return BacktestResult(periods, _metrics(strategy_returns, equity, selected_config, turnover), selected_config, symbol)
