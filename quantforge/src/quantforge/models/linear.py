from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self, cast

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..data import canonicalize_bars, validate_bars

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)

FACTOR_COLUMNS = ("symbol", "timestamp", "value")
FACTOR_SCHEMA: dict[str, object] = {
    "symbol": pl.String,
    "timestamp": pl.Datetime("us", "UTC"),
    "value": pl.Float64,
}
PREDICTION_COLUMNS = ("symbol", "timestamp", "prediction")
PREDICTION_SCHEMA: dict[str, object] = {
    "symbol": pl.String,
    "timestamp": pl.Datetime("us", "UTC"),
    "prediction": pl.Float64,
}


class ModelError(ValueError):
    """A model-layer alignment, causality, or numeric-safety failure."""


def _utc_from_microseconds(value: int) -> datetime:
    """Convert canonical microsecond timestamps without requiring ``tzdata``."""
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, ValueError) as exc:
        raise ModelError("bar timestamp is outside the representable UTC range") from exc


def _microseconds(value: datetime) -> int:
    delta = value - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


@dataclass(frozen=True)
class LinearModelFitConfig:
    """Immutable fit configuration: the causal training cutoff.

    A training sample is eligible only when its label bar (``t + 1``) has
    completed at or before ``train_end_timestamp``.
    """

    train_end_timestamp: datetime

    def __post_init__(self) -> None:
        value = self.train_end_timestamp
        if not isinstance(value, datetime):
            raise TypeError("train_end_timestamp must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("train_end_timestamp must be timezone-aware")
        try:
            normalized = value.astimezone(UTC)
        except (OverflowError, ValueError) as exc:
            raise ValueError(
                "train_end_timestamp is outside the representable UTC range"
            ) from exc
        object.__setattr__(self, "train_end_timestamp", normalized)


class FittedLinearModel(BaseModel):
    """Immutable audit record for one deterministic one-factor OLS fit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intercept: float
    coefficient: float
    train_end_timestamp: datetime
    training_rows: int = Field(strict=True)

    @field_validator("intercept", "coefficient")
    @classmethod
    def _finite(cls, value: float) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("fitted coefficients must be finite")
        return number

    @field_validator("train_end_timestamp")
    @classmethod
    def _train_end(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("train_end_timestamp must be timezone-aware")
        try:
            return value.astimezone(UTC)
        except (OverflowError, ValueError) as exc:
            raise ValueError(
                "train_end_timestamp is outside the representable UTC range"
            ) from exc

    @field_validator("training_rows")
    @classmethod
    def _training_rows(cls, value: int) -> int:
        if value < 2:
            raise ValueError("training_rows must be at least two")
        return value

    @classmethod
    def from_json(cls, value: str) -> Self:
        return cls.model_validate_json(value)


def _canonical_single_symbol_bars(bars: pl.DataFrame) -> pl.DataFrame:
    """Canonicalize and validate bars through the existing DATA-001/002 APIs."""
    if not isinstance(bars, pl.DataFrame):
        raise TypeError("bars must be a Polars DataFrame")
    canonical = canonicalize_bars(bars)
    if canonical.is_empty():
        raise ModelError("at least one bar is required")
    maximum_microseconds = cast(int | None, canonical["timestamp"].cast(pl.Int64).max())
    if maximum_microseconds is None:
        raise ModelError("bar timestamps must not be null")
    validate_bars(canonical, as_of=_utc_from_microseconds(int(maximum_microseconds)))
    if canonical.get_column("symbol").n_unique() != 1:
        raise ModelError("MODEL-001 supports exactly one symbol")
    return canonical


def _validated_factor(factor: pl.DataFrame) -> pl.DataFrame:
    """Validate the FACTOR-001 output contract without trusting row position."""
    if not isinstance(factor, pl.DataFrame):
        raise TypeError("factor must be a Polars DataFrame")
    if set(factor.columns) != set(FACTOR_COLUMNS):
        raise ModelError("factor columns must be exactly symbol, timestamp, value")
    if dict(factor.schema) != FACTOR_SCHEMA:
        raise ModelError("factor must use the canonical symbol/timestamp/value schema")
    if factor.is_empty():
        raise ModelError("factor must not be empty")
    keys = factor.select("symbol", "timestamp")
    if keys.is_duplicated().any():
        raise ModelError("factor contains duplicate (symbol, timestamp) keys")
    if factor.get_column("symbol").n_unique() != 1:
        raise ModelError("factor must contain exactly one symbol")
    return factor.select(FACTOR_COLUMNS)


def _aligned_factor(canonical: pl.DataFrame, factor: pl.DataFrame) -> pl.DataFrame:
    aligned = _validated_factor(factor)
    if aligned.height != canonical.height:
        raise ModelError("factor must contain exactly one row per canonical bar")
    bar_keys = canonical.select("symbol", "timestamp").sort(["symbol", "timestamp"])
    factor_keys = aligned.select("symbol", "timestamp").sort(["symbol", "timestamp"])
    if not factor_keys.equals(bar_keys):
        raise ModelError("factor keys must match the canonical bar keys exactly")
    return aligned


def fit_linear_model(
    bars: pl.DataFrame,
    factor: pl.DataFrame,
    config: LinearModelFitConfig,
) -> FittedLinearModel:
    """Fit the deterministic one-factor OLS model with an intercept.

    The training label for canonical bar ``t`` is the next-bar intraday return
    ``close[t + 1] / open[t + 1] - 1``.  A sample is eligible only when that
    label bar has completed at or before ``train_end_timestamp``, so the
    feature row at the cutoff itself can never consume a future label, and
    rows strictly after the cutoff have no influence on the fit.  Warm-up null
    factors mean the sample does not exist.
    """
    if not isinstance(config, LinearModelFitConfig):
        raise TypeError("config must be a LinearModelFitConfig")
    canonical = _canonical_single_symbol_bars(bars)
    aligned_factor = _aligned_factor(canonical, factor)
    cutoff_microseconds = _microseconds(config.train_end_timestamp)
    bar_microseconds = canonical["timestamp"].cast(pl.Int64)
    if cutoff_microseconds not in set(bar_microseconds.to_list()):
        raise ModelError("train_end_timestamp must be an existing canonical bar timestamp")

    frame = (
        canonical.select("symbol", "timestamp", "open", "close")
        .join(aligned_factor, on=["symbol", "timestamp"], how="inner")
        .sort(["symbol", "timestamp"])
        .with_columns(
            (pl.col("close").shift(-1) / pl.col("open").shift(-1) - 1.0).alias("target"),
            pl.col("timestamp").shift(-1).cast(pl.Int64).alias("label_microseconds"),
        )
    )
    eligible = frame.filter(
        pl.col("value").is_not_null()
        & pl.col("target").is_not_null()
        & (pl.col("label_microseconds") <= cutoff_microseconds)
    )
    features = [float(value) for value in eligible["value"].to_list()]
    targets = [float(value) for value in eligible["target"].to_list()]
    if len(features) < 2:
        raise ModelError("at least two eligible training rows are required")
    if not all(math.isfinite(value) for value in features) or not all(
        math.isfinite(value) for value in targets
    ):
        raise ModelError("training data must be finite")

    rows = len(features)
    try:
        feature_mean = math.fsum(features) / rows
        target_mean = math.fsum(targets) / rows
        feature_variance = math.fsum((value - feature_mean) ** 2 for value in features)
        covariance = math.fsum(
            (feature - feature_mean) * (target - target_mean)
            for feature, target in zip(features, targets, strict=True)
        )
    except OverflowError as exc:
        raise ModelError("fitted coefficients must be finite") from exc
    if feature_variance == 0.0:
        raise ModelError("feature variance is zero; the model cannot be fitted")
    coefficient = covariance / feature_variance
    intercept = target_mean - coefficient * feature_mean
    if not math.isfinite(coefficient) or not math.isfinite(intercept):
        raise ModelError("fitted coefficients must be finite")
    return FittedLinearModel(
        intercept=intercept,
        coefficient=coefficient,
        train_end_timestamp=config.train_end_timestamp,
        training_rows=rows,
    )


def predict_linear_model(
    factor: pl.DataFrame,
    model: FittedLinearModel,
) -> pl.DataFrame:
    """Apply a fixed fitted model to point-in-time factor values.

    Warm-up null factors stay null.  Predictions are continuous research
    values; they are never thresholded into signals, positions, or orders.
    """
    if not isinstance(model, FittedLinearModel):
        raise TypeError("model must be a FittedLinearModel")
    validated = _validated_factor(factor)
    ordered = validated.sort(["symbol", "timestamp"])
    predictions = ordered.with_columns(
        pl.when(pl.col("value").is_null())
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(model.intercept + model.coefficient * pl.col("value"))
        .cast(pl.Float64)
        .alias("prediction")
    ).select(PREDICTION_COLUMNS)
    available = predictions.filter(pl.col("prediction").is_not_null())
    if available.height and not available.select(
        pl.col("prediction").is_finite().all()
    ).item():
        raise ModelError("prediction is not finite")
    if dict(predictions.schema) != PREDICTION_SCHEMA:
        raise RuntimeError("prediction output does not match the declared schema")
    return predictions
