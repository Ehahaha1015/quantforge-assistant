"""Causal trailing absolute-return filter for long/flat research."""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

import polars as pl

from ._common import signal_frame, validate_percent, validate_window
from .moving_average import _single_symbol_bars


@dataclass(frozen=True)
class TimeSeriesMomentumConfig:
    lookback: int = 20
    threshold_pct: Decimal = Decimal("0.0")

    def __post_init__(self) -> None:
        validate_window(self.lookback, "lookback", minimum=2)
        validate_percent(
            self.threshold_pct, "threshold_pct", maximum=100, positive=False
        )


def generate_time_series_momentum_signals(
    bars: pl.DataFrame, config: TimeSeriesMomentumConfig | None = None
) -> pl.DataFrame:
    """Enter iff 100*(close[t]/close[t-lookback]-1) > threshold_pct.

    ``lookback`` leading rows are flat. Positive canonical prices permit the
    equivalent cross-multiplied comparison, avoiding division rounding at an
    exact threshold. The local Decimal context is independent of ambient state.
    """
    selected = TimeSeriesMomentumConfig() if config is None else config
    if not isinstance(selected, TimeSeriesMomentumConfig):
        raise TypeError("config must be a TimeSeriesMomentumConfig")
    canonical = _single_symbol_bars(bars)
    closes = [Decimal(str(v)) for v in canonical["close"]]
    values: list[float] = []
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        for index, close in enumerate(closes):
            held = False
            if index >= selected.lookback:
                previous = closes[index - selected.lookback]
                held = (close - previous) * Decimal(
                    100
                ) > previous * selected.threshold_pct
            values.append(float(held))
    return signal_frame(canonical, values)
