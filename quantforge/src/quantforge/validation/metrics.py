from __future__ import annotations

import math
from collections.abc import Sequence

from ..backtest import BacktestConfig, BacktestMetrics


def _sample_std(values: Sequence[float]) -> float:
    """Sample standard deviation using the BACKTEST-001 definition."""
    if len(values) < 2:
        return 0.0
    mean = math.fsum(values) / len(values)
    variance = math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def equity_path(strategy_returns: Sequence[float], *, initial_cash: float) -> list[float]:
    """Rebased equity path: starts at ``initial_cash`` and compounds OOS returns."""
    path = [initial_cash]
    for value in strategy_returns:
        path.append(path[-1] * (1.0 + value))
    return path


def rebased_equity(strategy_returns: Sequence[float], *, initial_cash: float) -> list[float]:
    """Equity path without the leading pre-period value, for period rows."""
    return equity_path(strategy_returns, initial_cash=initial_cash)[1:]


def oos_metrics(
    strategy_returns: Sequence[float],
    turnover: Sequence[float],
    *,
    backtest_config: BacktestConfig,
) -> BacktestMetrics:
    """Score a stitched out-of-sample stream with BACKTEST-001 metric definitions.

    Every supplied period is executable: a validation window's first
    out-of-sample bar can already carry a position established by its causal
    history, so no period is dropped the way BACKTEST-001 drops the
    structurally flat first bar of a fresh dataset.  The statistical
    definitions (sample deviation, annualisation, zero-volatility Sharpe,
    peak-to-trough drawdown, realised turnover) are identical to BACKTEST-001.
    """
    returns = [float(value) for value in strategy_returns]
    turnovers = [float(value) for value in turnover]
    if len(returns) != len(turnovers):
        raise ValueError("strategy_returns and turnover must have the same length")
    if not returns:
        raise ValueError("at least one out-of-sample period is required")

    path = equity_path(returns, initial_cash=backtest_config.initial_cash)
    annualization = math.sqrt(backtest_config.periods_per_year)
    volatility = _sample_std(returns) * annualization
    if volatility == 0.0:
        sharpe = 0.0
    else:
        mean_return = math.fsum(returns) / len(returns)
        sharpe = mean_return / _sample_std(returns) * annualization
    running_peak = backtest_config.initial_cash
    drawdowns: list[float] = []
    for value in path:
        running_peak = max(running_peak, value)
        drawdowns.append(value / running_peak - 1.0)
    return BacktestMetrics(
        total_return=path[-1] / backtest_config.initial_cash - 1.0,
        annualized_volatility=volatility,
        sharpe=sharpe,
        max_drawdown=min(drawdowns),
        turnover=math.fsum(turnovers),
    )
