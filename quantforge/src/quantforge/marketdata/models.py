"""Frozen PRICE-001 models (contract sections 8 and 9)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from ..provenance import RiskPriceFact
from .canonical import _observation_id
from .errors import (
    PriceArgumentError,
    PriceCoverageError,
    PriceEvidenceError,
    PriceFactError,
    PriceObservationError,
    PriceObservationIdentityError,
)

_OPAQUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_OBSERVATION_SCHEMA = "marketdata-observation-v1"
_REQUEST_SCHEMA = "marketdata-request-v1"


def _typed_validation_error(
    exc: ValidationError, *, default: type[PriceEvidenceError], label: str
) -> PriceEvidenceError:
    """Return the typed package error a validator raised, else a default one."""
    for error in exc.errors():
        context = error.get("ctx")
        if isinstance(context, dict):
            candidate = context.get("error")
            if isinstance(candidate, PriceEvidenceError):
                return candidate
    return default(f"{label} is invalid: {exc}")


def _utf8(symbol: str, *, error: type[PriceEvidenceError], message: str) -> bytes:
    try:
        return symbol.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise error(message) from exc


class _MarketDataValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    _default_error: ClassVar[type[PriceEvidenceError]] = PriceEvidenceError
    _label: ClassVar[str] = "market data value"

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate(obj, *args, **kwargs)
        except ValidationError as exc:
            raise _typed_validation_error(
                exc, default=cls._default_error, label=cls._label
            ) from exc

    @classmethod
    def model_validate_json(cls, json_data: Any, *args: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate_json(json_data, *args, **kwargs)
        except ValidationError as exc:
            raise _typed_validation_error(
                exc, default=cls._default_error, label=cls._label
            ) from exc


class PriceObservationRequest(_MarketDataValue):
    """One canonical, non-annotated request for a price observation."""

    _default_error: ClassVar[type[PriceEvidenceError]] = PriceArgumentError
    _label: ClassVar[str] = "price observation request"

    schema: Literal["marketdata-request-v1"]  # type: ignore[assignment]
    symbols: tuple[str, ...]

    @field_validator("symbols", mode="before")
    @classmethod
    def _canonical_symbols(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, tuple) or not value:
            raise PriceArgumentError("symbols must be a non-empty tuple of strings")
        seen: set[str] = set()
        for symbol in value:
            if not isinstance(symbol, str) or not symbol:
                raise PriceArgumentError("symbols must be non-empty strings")
            if symbol in seen:
                raise PriceArgumentError("symbols must not contain duplicates")
            seen.add(symbol)
            _utf8(
                symbol, error=PriceArgumentError, message="symbols must be valid UTF-8"
            )
        expected = tuple(sorted(value, key=lambda item: item.encode("utf-8")))
        if value != expected:
            raise PriceArgumentError("symbols must be in canonical UTF-8 order")
        return value

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise _typed_validation_error(
                exc, default=PriceArgumentError, label="price observation request"
            ) from exc


class VerifiedPriceObservation(_MarketDataValue):
    """One source-attested observation with deterministic observation identity."""

    _default_error: ClassVar[type[PriceEvidenceError]] = PriceObservationError
    _label: ClassVar[str] = "verified price observation"

    schema: Literal["marketdata-observation-v1"]  # type: ignore[assignment]
    observation_id: str
    market_source: str
    source_observation_id: str
    observed_at: datetime
    maximum_age: timedelta
    risk_prices: tuple[RiskPriceFact, ...]

    @field_validator(
        "observation_id",
        "market_source",
        "source_observation_id",
        mode="before",
    )
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        name = str(info.field_name)
        if not isinstance(value, str) or _OPAQUE_PATTERN.fullmatch(value) is None:
            raise PriceObservationError(f"{name} is malformed")
        return value

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed_at(cls, value: Any) -> datetime:
        if not isinstance(value, datetime):
            raise PriceObservationError("observed_at must be a datetime")
        try:
            if value.tzinfo is None or value.utcoffset() is None:
                raise PriceObservationError("observed_at must be timezone-aware")
            return value.astimezone(UTC)
        except PriceObservationError:
            raise
        except (OverflowError, TypeError, ValueError) as exc:
            raise PriceObservationError(
                "observed_at is outside the representable UTC range"
            ) from exc

    @field_validator("maximum_age", mode="before")
    @classmethod
    def _maximum_age(cls, value: Any) -> timedelta:
        if not isinstance(value, timedelta) or value <= timedelta(0):
            raise PriceObservationError(
                "maximum_age must be a strictly positive timedelta"
            )
        return value

    @field_validator("risk_prices", mode="before")
    @classmethod
    def _risk_prices(cls, value: Any) -> tuple[RiskPriceFact, ...]:
        if not isinstance(value, tuple):
            raise PriceObservationError("risk_prices must be a tuple")
        facts: list[RiskPriceFact] = []
        for fact in value:
            if not isinstance(fact, RiskPriceFact):
                raise PriceFactError("risk_prices must contain RiskPriceFact values")
            price = fact.price
            if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                raise PriceFactError("risk price must be a finite positive Decimal")
            symbol = fact.symbol
            if not isinstance(symbol, str) or not symbol:
                raise PriceFactError("risk price symbols must be non-empty strings")
            _utf8(
                symbol,
                error=PriceFactError,
                message="risk price symbols must be valid UTF-8",
            )
            facts.append(fact)
        symbols = [fact.symbol for fact in facts]
        if len(set(symbols)) != len(symbols):
            raise PriceCoverageError("risk_prices contains a duplicate symbol")
        expected = sorted(facts, key=lambda fact: fact.symbol.encode("utf-8"))
        if facts != expected:
            raise PriceCoverageError("risk_prices is not in canonical symbol order")
        return tuple(facts)

    @model_validator(mode="after")
    def _identity(self) -> Self:
        derived = _observation_id(
            market_source=self.market_source,
            source_observation_id=self.source_observation_id,
            observed_at=self.observed_at,
            maximum_age=self.maximum_age,
            risk_prices=self.risk_prices,
        )
        if self.observation_id != derived:
            raise PriceObservationIdentityError(
                "observation identity does not re-derive"
            )
        return self

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise _typed_validation_error(
                exc, default=PriceObservationError, label="verified price observation"
            ) from exc


__all__ = ["PriceObservationRequest", "VerifiedPriceObservation"]
