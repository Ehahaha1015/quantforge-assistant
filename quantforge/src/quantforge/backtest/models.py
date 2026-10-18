from __future__ import annotations

import math
from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class BacktestConfig:
    """Immutable configuration for the deterministic long-only backtester."""

    initial_cash: float = 100_000.0
    fee_bps: float = 0.0
    periods_per_year: int = 252

    def __post_init__(self) -> None:
        if isinstance(self.initial_cash, bool) or not isinstance(self.initial_cash, (int, float)):
            raise TypeError("initial_cash must be a real number")
        if isinstance(self.fee_bps, bool) or not isinstance(self.fee_bps, (int, float)):
            raise TypeError("fee_bps must be a real number")
        if isinstance(self.periods_per_year, bool) or not isinstance(self.periods_per_year, int):
            raise TypeError("periods_per_year must be an integer")
        initial_cash = float(self.initial_cash)
        fee_bps = float(self.fee_bps)
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            raise ValueError("initial_cash must be finite and greater than zero")
        if not math.isfinite(fee_bps) or fee_bps < 0:
            raise ValueError("fee_bps must be finite and non-negative")
        if self.periods_per_year <= 0:
            raise ValueError("periods_per_year must be greater than zero")
        object.__setattr__(self, "initial_cash", initial_cash)
        object.__setattr__(self, "fee_bps", fee_bps)


@dataclass(frozen=True)
class BacktestMetrics:
    """The five deliberately limited metrics exposed by BACKTEST-001.

    ``total_return`` is final equity divided by initial cash minus one.
    ``annualized_volatility`` is the sample standard deviation of executable
    period returns (excluding the non-trading first row), annualized with
    ``periods_per_year``.  ``sharpe`` uses a zero risk-free rate and the same
    sample standard deviation; it is zero when volatility is zero.
    ``max_drawdown`` is the minimum percentage drawdown from a running equity
    peak (therefore normally non-positive), and ``turnover`` is the sum of
    absolute binary position changes at opens.
    """

    total_return: float
    annualized_volatility: float
    sharpe: float
    max_drawdown: float
    turnover: float


@dataclass(frozen=True)
class BacktestResult:
    """Period-level audit table and aggregate metrics for one symbol."""

    periods: pl.DataFrame
    metrics: BacktestMetrics
    config: BacktestConfig
    symbol: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods", self.periods.clone())
