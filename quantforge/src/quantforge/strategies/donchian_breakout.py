"""Causal prior-high/low-channel breakout with a stateful long/flat exit."""

from dataclasses import dataclass
from decimal import Decimal

import polars as pl

from ._common import signal_frame, validate_window
from .moving_average import _single_symbol_bars


@dataclass(frozen=True)
class DonchianBreakoutConfig:
    entry_window: int = 20
    exit_window: int = 10

    def __post_init__(self) -> None:
        validate_window(self.entry_window, "entry_window", minimum=2)
        validate_window(self.exit_window, "exit_window", minimum=1)
        if self.entry_window <= self.exit_window:
            raise ValueError("entry_window must be greater than exit_window")


def generate_donchian_breakout_signals(
    bars: pl.DataFrame, config: DonchianBreakoutConfig | None = None
) -> pl.DataFrame:
    """Current close breaks above prior highs, exits below prior lows.

    Both channel windows exclude the current observation. The first
    ``entry_window`` rows are flat; equality preserves the held state.
    """
    selected = DonchianBreakoutConfig() if config is None else config
    if not isinstance(selected, DonchianBreakoutConfig):
        raise TypeError("config must be a DonchianBreakoutConfig")
    canonical = _single_symbol_bars(bars)
    closes = [Decimal(str(v)) for v in canonical["close"]]
    highs = [Decimal(str(v)) for v in canonical["high"]]
    lows = [Decimal(str(v)) for v in canonical["low"]]
    held = False
    values: list[float] = []
    for index, close in enumerate(closes):
        if index >= selected.entry_window:
            if held:
                if close < min(lows[index - selected.exit_window : index]):
                    held = False
            elif close > max(highs[index - selected.entry_window : index]):
                held = True
        values.append(float(held))
    return signal_frame(canonical, values)
