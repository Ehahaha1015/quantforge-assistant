from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import polars as pl

from ..data import canonicalize_bars, validate_bars

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
FACTOR_COLUMNS = ("symbol", "timestamp", "value")
FACTOR_SCHEMA: dict[str, object] = {
    "symbol": pl.String,
    "timestamp": pl.Datetime("us", "UTC"),
    "value": pl.Float64,
}


@dataclass(frozen=True)
class MomentumFactorConfig:
    """Immutable configuration for the trailing price-momentum factor."""

    lookback: int = 20

    def __post_init__(self) -> None:
        if isinstance(self.lookback, bool) or not isinstance(self.lookback, int):
            raise TypeError("lookback must be an integer")
        if self.lookback <= 0:
            raise ValueError("lookback must be greater than zero")


def _utc_from_microseconds(value: int) -> datetime:
    """Convert canonical microsecond timestamps without requiring ``tzdata``."""
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("bar timestamp is outside the representable UTC range") from exc


def _canonical_single_symbol_bars(bars: pl.DataFrame) -> pl.DataFrame:
    """Canonicalize and validate bars through the existing DATA-001/002 APIs.

    The ``as_of`` boundary is the maximum bar timestamp, exactly as
    BACKTEST-001, STRATEGY-001 and VALIDATION-001 define it; FACTOR-001 adds
    no second market-data validation, ordering rule, or dataset hash.
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
        raise ValueError("FACTOR-001 supports exactly one symbol")
    return canonical


def compute_momentum_factor(
    bars: pl.DataFrame,
    config: MomentumFactorConfig | None = None,
) -> pl.DataFrame:
    """Compute the deterministic trailing price-momentum factor.

    For canonical bar index ``t`` the factor is ``close[t] /
    close[t - lookback] - 1`` and is null for the first ``lookback`` rows,
    where causal history is insufficient.  The value is a continuous research
    factor: it may be negative, zero, or greater than one, and it is not a
    trading signal.  ``factor[t]`` uses close prices at or before ``t`` only;
    nothing is shifted forward, backfilled, or fabricated.
    """
    selected_config = MomentumFactorConfig() if config is None else config
    if not isinstance(selected_config, MomentumFactorConfig):
        raise TypeError("config must be a MomentumFactorConfig")
    canonical = _canonical_single_symbol_bars(bars)

    factor = canonical.select(
        pl.col("symbol"),
        pl.col("timestamp"),
        (pl.col("close") / pl.col("close").shift(selected_config.lookback) - 1.0).alias(
            "value"
        ),
    )
    available = factor.filter(pl.col("value").is_not_null())
    if available.height and not available.select(pl.col("value").is_finite().all()).item():
        raise ValueError("momentum factor derived a non-finite value")
    if dict(factor.schema) != FACTOR_SCHEMA:
        raise RuntimeError("momentum factor output does not match the declared schema")
    return factor.select(FACTOR_COLUMNS)
