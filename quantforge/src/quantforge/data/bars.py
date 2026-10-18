from __future__ import annotations

import hashlib
import math
import struct
from datetime import UTC, datetime
from typing import Any

import polars as pl

BAR_COLUMNS = ("symbol", "timestamp", "open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("open", "high", "low", "close")
NUMERIC_COLUMNS = (*PRICE_COLUMNS, "volume")
BAR_SCHEMA: dict[str, Any] = {
    "symbol": pl.String,
    "timestamp": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
}


def normalize_utc(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} is outside the representable UTC range") from exc


def _require_columns(frame: pl.DataFrame) -> None:
    actual = set(frame.columns)
    expected = set(BAR_COLUMNS)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"bar columns do not match canonical schema; missing={missing}, extra={extra}")


def _safe_float64_column(frame: pl.DataFrame, name: str) -> pl.Expr:
    dtype = frame.schema[name]
    if dtype == pl.Boolean or not dtype.is_numeric():
        raise ValueError(f"{name} must contain numeric, non-boolean values")
    converted = pl.col(name).cast(pl.Float64, strict=True)
    if dtype not in (pl.Float32, pl.Float64):
        unsafe = frame.select(
            converted.cast(dtype, strict=False).ne_missing(pl.col(name)).any()
        ).item()
        if unsafe:
            raise ValueError(f"{name} cannot be represented exactly as Float64")
    return converted


def canonicalize_bars(frame: pl.DataFrame) -> pl.DataFrame:
    if not isinstance(frame, pl.DataFrame):
        raise TypeError("frame must be a Polars DataFrame")
    _require_columns(frame)

    timestamp_dtype = frame.schema["timestamp"]
    if not isinstance(timestamp_dtype, pl.Datetime) or timestamp_dtype.time_zone is None:
        raise ValueError("timestamp must be a timezone-aware Polars Datetime column")
    if timestamp_dtype.time_unit != "us":
        raise ValueError("timestamp must use microsecond (us) precision")
    if frame.schema["symbol"] != pl.String:
        raise ValueError("symbol must be a Polars String column")

    numeric_expressions = [_safe_float64_column(frame, name).alias(name) for name in NUMERIC_COLUMNS]
    canonical = frame.with_columns(
        pl.col("symbol").cast(pl.String, strict=True).str.strip_chars(),
        pl.col("timestamp").dt.convert_time_zone("UTC").cast(BAR_SCHEMA["timestamp"]),
        *numeric_expressions,
    )
    canonical = canonical.with_columns(
        *[
            pl.when(pl.col(name) == 0.0).then(pl.lit(0.0)).otherwise(pl.col(name)).alias(name)
            for name in NUMERIC_COLUMNS
        ]
    )
    return canonical.select(BAR_COLUMNS).sort(["symbol", "timestamp"])


def validate_bars(frame: pl.DataFrame, *, as_of: datetime) -> None:
    as_of_utc = normalize_utc(as_of, name="as_of")
    if frame.schema != BAR_SCHEMA:
        raise ValueError("bar frame is not canonical")
    if any(frame[name].null_count() for name in BAR_COLUMNS):
        raise ValueError("canonical bar columns must not contain nulls")
    if frame.select(pl.col("symbol").eq("").any()).item():
        raise ValueError("symbol must not be empty")
    if frame.select(~pl.all_horizontal(pl.col(*NUMERIC_COLUMNS).is_finite()).all()).item():
        raise ValueError("OHLCV values must be finite")
    if frame.select(~pl.all_horizontal(pl.col(*PRICE_COLUMNS) > 0).all()).item():
        raise ValueError("prices must be greater than zero")
    if frame.select((pl.col("volume") < 0).any()).item():
        raise ValueError("volume must be non-negative")
    invalid_ohlc = (
        (pl.col("low") > pl.col("open"))
        | (pl.col("low") > pl.col("close"))
        | (pl.col("high") < pl.col("open"))
        | (pl.col("high") < pl.col("close"))
        | (pl.col("high") < pl.col("low"))
    )
    if frame.select(invalid_ohlc.any()).item():
        raise ValueError("OHLC relationships are invalid")
    if frame.select(pl.struct("symbol", "timestamp").is_duplicated().any()).item():
        raise ValueError("duplicate (symbol, timestamp) market key")
    if frame.select((pl.col("timestamp") > as_of_utc).any()).item():
        raise ValueError("bar timestamp must not be after as_of")


def prepare_bars(frame: pl.DataFrame, *, as_of: datetime) -> pl.DataFrame:
    canonical = canonicalize_bars(frame)
    validate_bars(canonical, as_of=as_of)
    return canonical


def logical_content_hash(frame: pl.DataFrame) -> str:
    if frame.schema != BAR_SCHEMA:
        raise ValueError("bar frame is not canonical")
    if frame.select(pl.struct("symbol", "timestamp").is_duplicated().any()).item():
        raise ValueError("duplicate (symbol, timestamp) market key")
    digest = hashlib.sha256()
    digest.update(b"QuantForge:canonical-bars:v1\0")
    for name in BAR_COLUMNS:
        encoded = name.encode("ascii")
        digest.update(struct.pack(">H", len(encoded)))
        digest.update(encoded)
    ordered = frame.sort(["symbol", "timestamp"])
    digest.update(struct.pack(">Q", ordered.height))
    timestamps = ordered["timestamp"].cast(pl.Int64).to_list()
    symbols = ordered["symbol"].to_list()
    numeric_values = [ordered[name].to_list() for name in NUMERIC_COLUMNS]
    for index, (symbol, microseconds) in enumerate(zip(symbols, timestamps, strict=True)):
        if symbol is None or microseconds is None:
            raise ValueError("canonical bar keys must not contain nulls")
        encoded_symbol = symbol.encode("utf-8")
        digest.update(struct.pack(">Q", len(encoded_symbol)))
        digest.update(encoded_symbol)
        digest.update(struct.pack(">q", microseconds))
        for column in numeric_values:
            value = column[index]
            if value is None:
                raise ValueError("OHLCV values must not contain nulls")
            if not math.isfinite(value):
                raise ValueError("OHLCV values must be finite")
            digest.update(struct.pack(">d", 0.0 if value == 0.0 else value))
    return digest.hexdigest()
