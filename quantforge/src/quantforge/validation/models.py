from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import polars as pl

from ..backtest import BacktestConfig, BacktestMetrics
from ..strategies.models import StrategyConfig

ValidationKind = Literal["holdout", "walk_forward"]


@dataclass(frozen=True)
class ChronologicalHoldoutConfig:
    """Immutable split for one chronological out-of-sample holdout.

    Canonical rows ``0 .. history_bars - 1`` are causal history; rows
    ``history_bars .. N - 1`` are the scored out-of-sample region.
    """

    history_bars: int = 252

    def __post_init__(self) -> None:
        if isinstance(self.history_bars, bool) or not isinstance(
            self.history_bars, int
        ):
            raise TypeError("history_bars must be an integer")
        if self.history_bars <= 0:
            raise ValueError("history_bars must be greater than zero")


@dataclass(frozen=True)
class WalkForwardConfig:
    """Immutable configuration for expanding-window walk-forward validation."""

    initial_history_bars: int = 252
    oos_window_bars: int = 63

    def __post_init__(self) -> None:
        for name, value in (
            ("initial_history_bars", self.initial_history_bars),
            ("oos_window_bars", self.oos_window_bars),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if self.initial_history_bars <= 0:
            raise ValueError("initial_history_bars must be greater than zero")
        if self.oos_window_bars <= 0:
            raise ValueError("oos_window_bars must be greater than zero")


@dataclass(frozen=True)
class OOSResult:
    """One scored out-of-sample slice with validation-rebased equity.

    ``periods`` reuses the BACKTEST-001 period representation; the only
    validation-layer change is that ``equity`` is rebased from
    ``BacktestConfig.initial_cash`` at the beginning of this slice so the
    drawdown and total-return path contains no historical/context return.
    """

    periods: pl.DataFrame
    metrics: BacktestMetrics
    oos_bars: int
    start_timestamp: datetime
    end_timestamp: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods", self.periods.clone())


@dataclass(frozen=True)
class ValidationWindow:
    """One validation window: causal context boundary plus its scored OOS slice.

    ``oos_start_index`` is inclusive and ``oos_end_index`` is exclusive, both
    in canonical bar order.
    """

    index: int
    history_bars: int
    oos_start_index: int
    oos_end_index: int
    result: OOSResult


@dataclass(frozen=True)
class ValidationResult:
    """Completed deterministic validation run over one fixed configuration pair."""

    kind: ValidationKind
    windows: tuple[ValidationWindow, ...]
    aggregate: OOSResult
    strategy_config: StrategyConfig
    backtest_config: BacktestConfig
