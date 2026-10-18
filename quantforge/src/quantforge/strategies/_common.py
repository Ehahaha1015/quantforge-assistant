"""Shared strict parameter and signal boundaries for new research families."""

from decimal import Decimal

import polars as pl

from quantforge.backtest import SIGNAL_COLUMNS, SIGNAL_SCHEMA

MAX_WINDOW = 252


def validate_window(value: int, name: str, *, minimum: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be a strict integer")
    if not minimum <= value <= MAX_WINDOW:
        raise ValueError(f"{name} must be between {minimum} and {MAX_WINDOW}")


def validate_percent(
    value: Decimal, name: str, *, maximum: int, positive: bool
) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")
    if not value.is_finite() or value.is_signed():
        raise ValueError(f"{name} must be a finite unsigned Decimal")
    if not (value > 0 if positive else value >= 0) or value > maximum:
        raise ValueError(f"{name} is outside the declared percent bounds")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -4:
        raise ValueError(f"{name} supports at most four fractional digits")


def signal_frame(canonical: pl.DataFrame, values: list[float]) -> pl.DataFrame:
    signals = canonical.select("symbol", "timestamp").with_columns(
        pl.Series("value", values, dtype=pl.Float64)
    )
    if dict(signals.schema) != SIGNAL_SCHEMA:
        raise RuntimeError("strategy output does not match BACKTEST-001 signal schema")
    return signals.select(SIGNAL_COLUMNS)
