"""Causal lower-band reversion with explicit percent and variance semantics."""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

import polars as pl

from ._common import signal_frame, validate_percent, validate_window
from .moving_average import _single_symbol_bars


@dataclass(frozen=True)
class BollingerReversionConfig:
    window: int = 20
    k_pct: Decimal = Decimal("200.0")

    def __post_init__(self) -> None:
        validate_window(self.window, "window", minimum=2)
        validate_percent(self.k_pct, "k_pct", maximum=1000, positive=True)


def generate_bollinger_reversion_signals(
    bars: pl.DataFrame, config: BollingerReversionConfig | None = None
) -> pl.DataFrame:
    """Enter at/below mean-(k_pct/100)*population std; exit at/above mean.

    The current close is included in the trailing window. Zero variance stays
    flat, avoiding contradictory entry/exit at coincident bands. The first
    ``window - 1`` observations are flat. Decimal arithmetic uses its own
    precision/rounding/trap context, independent of the caller's environment.
    """
    selected = BollingerReversionConfig() if config is None else config
    if not isinstance(selected, BollingerReversionConfig):
        raise TypeError("config must be a BollingerReversionConfig")
    canonical = _single_symbol_bars(bars)
    closes = [Decimal(str(v)) for v in canonical["close"]]
    held = False
    values: list[float] = []
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        for index, close in enumerate(closes):
            if index + 1 >= selected.window:
                window = closes[index + 1 - selected.window : index + 1]
                mean = sum(window, Decimal(0)) / selected.window
                variance = (
                    sum(((v - mean) ** 2 for v in window), Decimal(0)) / selected.window
                )
                if held:
                    if close >= mean:
                        held = False
                elif variance > 0:
                    lower = mean - selected.k_pct / Decimal(100) * variance.sqrt()
                    if close <= lower:
                        held = True
            values.append(float(held))
    return signal_frame(canonical, values)
