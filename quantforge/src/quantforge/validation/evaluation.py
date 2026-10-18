from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import polars as pl

from ..backtest import (
    PERIOD_COLUMNS,
    BacktestConfig,
    BacktestResult,
    run_vectorized_backtest,
)
from ..data import canonicalize_bars, validate_bars
from ..strategies import (
    MovingAverageCrossoverConfig,
    generate_moving_average_signals,
)
from ..strategies.models import (
    STRATEGY_CONFIG_TYPES,
    StrategyConfig,
    generate_strategy_signals,
    required_observations,
)
from .metrics import oos_metrics, rebased_equity
from .models import (
    ChronologicalHoldoutConfig,
    OOSResult,
    ValidationResult,
    ValidationWindow,
    WalkForwardConfig,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _utc_from_microseconds(value: int) -> datetime:
    """Convert canonical microsecond timestamps without requiring ``tzdata``."""
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(
            "bar timestamp is outside the representable UTC range"
        ) from exc


def _canonical_single_symbol_bars(bars: pl.DataFrame) -> pl.DataFrame:
    """Canonicalize and validate bars through the existing DATA-001/002 APIs.

    The ``as_of`` boundary is the maximum bar timestamp, exactly as
    BACKTEST-001 and STRATEGY-001 define it; VALIDATION-001 adds no second
    market-data validation and no second hashing or ordering rule.
    """
    if not isinstance(bars, pl.DataFrame):
        raise TypeError("bars must be a Polars DataFrame")
    canonical = canonicalize_bars(bars)
    if canonical.is_empty():
        raise ValueError("at least one bar is required")
    maximum_microseconds = cast(int | None, canonical["timestamp"].cast(pl.Int64).max())
    if maximum_microseconds is None:
        raise ValueError("bar timestamps must not be null")
    validate_bars(canonical, as_of=_utc_from_microseconds(int(maximum_microseconds)))
    if canonical.get_column("symbol").n_unique() != 1:
        raise ValueError("VALIDATION-001 supports exactly one symbol")
    return canonical


def _score_prefix(
    prefix: pl.DataFrame,
    strategy_config: StrategyConfig,
    backtest_config: BacktestConfig,
) -> BacktestResult:
    """Run the fixed strategy and BACKTEST-001 over one causal prefix."""
    signals = (
        generate_moving_average_signals(prefix, strategy_config)
        if isinstance(strategy_config, MovingAverageCrossoverConfig)
        else generate_strategy_signals(prefix, strategy_config)
    )
    return run_vectorized_backtest(prefix, signals, backtest_config)


def _oos_result(
    backtest_result: BacktestResult,
    *,
    start: int,
    end: int,
    backtest_config: BacktestConfig,
) -> OOSResult:
    """Slice and rebase one scored out-of-sample region of a prefix result."""
    periods = backtest_result.periods.slice(start, end - start)
    if periods.height != end - start:
        raise RuntimeError("out-of-sample slice does not match the requested window")
    returns = periods["strategy_return"].to_list()
    turnover = periods["turnover"].to_list()
    rebased = periods.with_columns(
        pl.Series(
            "equity",
            rebased_equity(returns, initial_cash=backtest_config.initial_cash),
        )
    ).select(PERIOD_COLUMNS)
    timestamps = periods["timestamp"].cast(pl.Int64)
    return OOSResult(
        periods=rebased,
        metrics=oos_metrics(returns, turnover, backtest_config=backtest_config),
        oos_bars=periods.height,
        start_timestamp=_utc_from_microseconds(int(timestamps[0])),
        end_timestamp=_utc_from_microseconds(int(timestamps[-1])),
    )


def _aggregate_result(
    windows: tuple[ValidationWindow, ...], backtest_config: BacktestConfig
) -> OOSResult:
    """Stitch every non-overlapping OOS slice into one chronological stream."""
    periods = pl.concat([window.result.periods for window in windows], how="vertical")
    returns = periods["strategy_return"].to_list()
    turnover = periods["turnover"].to_list()
    rebased = periods.with_columns(
        pl.Series(
            "equity",
            rebased_equity(returns, initial_cash=backtest_config.initial_cash),
        )
    ).select(PERIOD_COLUMNS)
    return OOSResult(
        periods=rebased,
        metrics=oos_metrics(returns, turnover, backtest_config=backtest_config),
        oos_bars=periods.height,
        start_timestamp=windows[0].result.start_timestamp,
        end_timestamp=windows[-1].result.end_timestamp,
    )


def run_holdout_validation(
    bars: pl.DataFrame,
    strategy_config: StrategyConfig,
    backtest_config: BacktestConfig,
    validation_config: ChronologicalHoldoutConfig,
) -> ValidationResult:
    """Score one chronological holdout with a fixed strategy and backtest config.

    The strategy runs once over every canonical bar so the position entering
    the out-of-sample region is whatever the causal history established; only
    rows from ``history_bars`` onward are scored.
    """
    if not isinstance(backtest_config, BacktestConfig):
        raise TypeError("backtest_config must be a BacktestConfig")
    if not isinstance(strategy_config, STRATEGY_CONFIG_TYPES):
        raise TypeError("strategy_config must be a MovingAverageCrossoverConfig")
    if not isinstance(validation_config, ChronologicalHoldoutConfig):
        raise TypeError("validation_config must be a ChronologicalHoldoutConfig")
    canonical = _canonical_single_symbol_bars(bars)
    total = canonical.height
    history = validation_config.history_bars
    if history >= total:
        raise ValueError("history_bars must be less than the canonical bar count")
    if history < required_observations(strategy_config):
        requirement = (
            "long_window"
            if isinstance(strategy_config, MovingAverageCrossoverConfig)
            else "required_observations"
        )
        raise ValueError(f"history_bars must be at least the strategy {requirement}")

    scored = _score_prefix(canonical, strategy_config, backtest_config)
    result = _oos_result(
        scored, start=history, end=total, backtest_config=backtest_config
    )
    window = ValidationWindow(
        index=0,
        history_bars=history,
        oos_start_index=history,
        oos_end_index=total,
        result=result,
    )
    return ValidationResult(
        kind="holdout",
        windows=(window,),
        aggregate=result,
        strategy_config=strategy_config,
        backtest_config=backtest_config,
    )


def run_walk_forward_validation(
    bars: pl.DataFrame,
    strategy_config: StrategyConfig,
    backtest_config: BacktestConfig,
    validation_config: WalkForwardConfig,
) -> ValidationResult:
    """Score non-overlapping expanding-window walk-forward out-of-sample windows.

    Each window is produced from ``bars[:oos_end]`` only, so a window's result
    can never depend on rows after its own out-of-sample end.  History expands
    chronologically, windows never overlap or leave a gap, and a final partial
    window is included whenever at least one unscored bar remains.
    """
    if not isinstance(backtest_config, BacktestConfig):
        raise TypeError("backtest_config must be a BacktestConfig")
    if not isinstance(strategy_config, STRATEGY_CONFIG_TYPES):
        raise TypeError("strategy_config must be a MovingAverageCrossoverConfig")
    if not isinstance(validation_config, WalkForwardConfig):
        raise TypeError("validation_config must be a WalkForwardConfig")
    canonical = _canonical_single_symbol_bars(bars)
    total = canonical.height
    initial_history = validation_config.initial_history_bars
    if initial_history >= total:
        raise ValueError(
            "initial_history_bars must leave at least one out-of-sample bar"
        )
    if initial_history < required_observations(strategy_config):
        requirement = (
            "long_window"
            if isinstance(strategy_config, MovingAverageCrossoverConfig)
            else "required_observations"
        )
        raise ValueError(
            f"initial_history_bars must be at least the strategy {requirement}"
        )

    windows: list[ValidationWindow] = []
    start = initial_history
    while start < total:
        end = min(start + validation_config.oos_window_bars, total)
        # Information boundary: only rows through this window's OOS end exist
        # for this window, so later rows cannot influence it.
        prefix = canonical.head(end)
        scored = _score_prefix(prefix, strategy_config, backtest_config)
        windows.append(
            ValidationWindow(
                index=len(windows),
                history_bars=start,
                oos_start_index=start,
                oos_end_index=end,
                result=_oos_result(
                    scored, start=start, end=end, backtest_config=backtest_config
                ),
            )
        )
        start = end
    frozen_windows = tuple(windows)
    return ValidationResult(
        kind="walk_forward",
        windows=frozen_windows,
        aggregate=_aggregate_result(frozen_windows, backtest_config),
        strategy_config=strategy_config,
        backtest_config=backtest_config,
    )
