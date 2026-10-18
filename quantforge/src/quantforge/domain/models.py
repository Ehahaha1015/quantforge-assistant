from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

Side = Literal["BUY", "SELL"]


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("datetime is outside the representable UTC range") from exc


def _finite_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("value must be finite")
    sign, digits, raw_exponent = value.as_tuple()
    exponent = cast(int, raw_exponent)
    if value.is_zero():
        return Decimal(0)
    while digits and digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1
    return Decimal((sign, digits, exponent))


class DomainModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )


class Bar(DomainModel):
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    _timestamp_utc = field_validator("timestamp", mode="after")(_utc_datetime)
    _finite_values = field_validator("open", "high", "low", "close", "volume", mode="after")(
        _finite_decimal
    )

    @field_validator("open", "high", "low", "close")
    @classmethod
    def _positive_price(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("price must be greater than zero")
        return value

    @field_validator("volume")
    @classmethod
    def _non_negative_volume(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("volume must be non-negative")
        return value

    @model_validator(mode="after")
    def _valid_ohlc(self) -> Bar:
        if self.low > self.open or self.low > self.close:
            raise ValueError("low must be less than or equal to open and close")
        if self.high < self.open or self.high < self.close or self.high < self.low:
            raise ValueError("high must be greater than or equal to open, close, and low")
        return self


class Signal(DomainModel):
    strategy_id: str
    symbol: str
    timestamp: datetime
    value: Decimal

    _timestamp_utc = field_validator("timestamp", mode="after")(_utc_datetime)
    _finite_value = field_validator("value", mode="after")(_finite_decimal)

    @field_validator("value")
    @classmethod
    def _bounded_value(cls, value: Decimal) -> Decimal:
        if not -1 <= value <= 1:
            raise ValueError("signal value must be between -1 and 1")
        return value


class OrderIntent(DomainModel):
    strategy_id: str
    symbol: str
    timestamp: datetime
    side: Side
    quantity: Decimal

    _timestamp_utc = field_validator("timestamp", mode="after")(_utc_datetime)
    _finite_quantity = field_validator("quantity", mode="after")(_finite_decimal)

    @field_validator("quantity")
    @classmethod
    def _positive_quantity(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("quantity must be greater than zero")
        return value


class Fill(DomainModel):
    order_id: str
    symbol: str
    timestamp: datetime
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal

    _timestamp_utc = field_validator("timestamp", mode="after")(_utc_datetime)
    _finite_values = field_validator("quantity", "price", "fee", mode="after")(_finite_decimal)

    @field_validator("quantity", "price")
    @classmethod
    def _positive_value(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("quantity and price must be greater than zero")
        return value

    @field_validator("fee")
    @classmethod
    def _non_negative_fee(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("fee must be non-negative")
        return value


class Position(DomainModel):
    symbol: str
    quantity: Decimal
    average_price: Decimal

    _finite_values = field_validator("quantity", "average_price", mode="after")(_finite_decimal)


class Experiment(DomainModel):
    experiment_id: str
    git_commit: str
    dataset_hash: str
    random_seed: int
    started_at: datetime

    _started_at_utc = field_validator("started_at", mode="after")(_utc_datetime)
