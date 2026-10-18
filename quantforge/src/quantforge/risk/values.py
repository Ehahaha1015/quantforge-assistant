from __future__ import annotations

from decimal import Decimal, DecimalException
from enum import StrEnum
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class RiskError(ValueError):
    """The public error boundary for deterministic pre-trade risk values."""


def _decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int, str)):
        raise RiskError(f"{name} must be an exact Decimal or int, not {type(value).__name__}")
    try:
        number = Decimal(value)
    except (DecimalException, ValueError) as exc:
        raise RiskError(f"{name} must be a decimal number") from exc
    if not number.is_finite():
        raise RiskError(f"{name} must be finite")
    return number


class RiskReason(StrEnum):
    """Stable rejection reason codes; the string value is the canonical identity."""

    SELL_EXCEEDS_POSITION = "SELL_EXCEEDS_POSITION"
    ORDER_NOTIONAL_LIMIT = "ORDER_NOTIONAL_LIMIT"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    POSITION_NOTIONAL_LIMIT = "POSITION_NOTIONAL_LIMIT"
    GROSS_EXPOSURE_LIMIT = "GROSS_EXPOSURE_LIMIT"


class RiskLimits(BaseModel):
    """Immutable, fully required deterministic risk limits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_order_notional: Decimal
    max_position_notional: Decimal
    max_gross_exposure: Decimal

    @field_validator(
        "max_order_notional",
        "max_position_notional",
        "max_gross_exposure",
        mode="before",
    )
    @classmethod
    def _positive(cls, value: Any, info: Any) -> Decimal:
        number = _decimal(value, name=str(info.field_name))
        if number <= 0:
            raise RiskError(f"{info.field_name} must be greater than zero")
        return number

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise RiskError(f"risk limits are invalid: {exc}") from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            return cls.model_validate_json(value)
        except ValidationError as exc:
            raise RiskError(f"risk limits payload is invalid: {exc}") from exc


class RiskDecision(BaseModel):
    """Immutable approval or rejection with stable, canonically ordered reasons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: bool = Field(strict=True)
    reasons: tuple[RiskReason, ...] = ()
    detail: str = ""

    @field_validator("reasons", mode="before")
    @classmethod
    def _canonical(cls, value: Any) -> tuple[RiskReason, ...]:
        order = list(RiskReason)
        items = [item if isinstance(item, RiskReason) else RiskReason(item) for item in value]
        return tuple(sorted(set(items), key=order.index))

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.approved and self.reasons:
            raise RiskError("approved decisions must not carry rejection reasons")
        if not self.approved and not self.reasons:
            raise RiskError("rejected decisions require at least one rejection reason")
        return self

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise RiskError(f"risk decision is invalid: {exc}") from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            return cls.model_validate_json(value)
        except ValidationError as exc:
            raise RiskError(f"risk decision payload is invalid: {exc}") from exc
