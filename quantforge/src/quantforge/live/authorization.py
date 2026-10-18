"""Deterministic human authorization boundary for future live execution.

This module is the last deterministic gate before a separate, still locked
broker adapter could ever consider one exact order. It validates and records
values; it never submits anything, never touches accounting, and never reads
a clock.

The declaration defined here is bound to one exact canonical order
fingerprint. That binding is what stops a declaration made for one order from
being applied to a materially different one.

Two installed limits must not be overstated:

- a deterministic library cannot prove that a human authored a declaration,
  so this module records the declared operator and instant instead;
- a frozen risk decision does not carry the order it was computed for, so the
  declaration, not the risk decision, is what pins the order.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..domain import OrderIntent
from ..risk import RiskDecision

__all__ = [
    "HumanLiveAuthorization",
    "LiveAuthorizationError",
    "LiveExecutionPermission",
    "authorize_live_order",
    "create_live_authorization",
    "live_order_fingerprint",
]

_FINGERPRINT_PATTERN = r"^live1-[0-9a-f]{32}$"
_AUTHORIZATION_FIELDS = {"mode", "order_fingerprint", "authorized_by", "authorized_at"}
_PERMISSION_FIELDS = {
    "order",
    "order_fingerprint",
    "risk_decision",
    "authorization",
    "mode",
}


class LiveAuthorizationError(ValueError):
    """The public error boundary for live human authorization."""


class _LiveValue(BaseModel):
    """Frozen, strict, extra-forbidding base for the live value objects."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HumanLiveAuthorization(_LiveValue):
    """A declared human authorization for one exact canonical order."""

    mode: Literal["LIVE"]
    order_fingerprint: str = Field(pattern=_FINGERPRINT_PATTERN)
    authorized_by: str
    authorized_at: datetime

    @field_validator("authorized_by")
    @classmethod
    def _requires_operator(cls, value: str) -> str:
        if not value.strip():
            raise LiveAuthorizationError(
                "authorized_by must name the declaring operator"
            )
        return value

    @field_validator("authorized_at")
    @classmethod
    def _requires_aware_instant(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise LiveAuthorizationError("authorized_at must be timezone-aware")
        try:
            return value.astimezone(UTC)
        except (OverflowError, ValueError) as exc:
            raise LiveAuthorizationError(
                "authorized_at is outside the representable UTC range"
            ) from exc

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise LiveAuthorizationError(
                f"human live authorization is invalid: {exc}"
            ) from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            data = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise LiveAuthorizationError(
                "human live authorization payload is not valid JSON"
            ) from exc
        if not isinstance(data, dict) or set(data) != _AUTHORIZATION_FIELDS:
            raise LiveAuthorizationError(
                "human live authorization payload has unexpected fields"
            )
        instant = data["authorized_at"]
        if not isinstance(instant, str):
            raise LiveAuthorizationError(
                "authorized_at must be an ISO-8601 instant string in a payload"
            )
        try:
            authorized_at = datetime.fromisoformat(instant)
        except ValueError as exc:
            raise LiveAuthorizationError(
                "authorized_at is not an ISO-8601 instant"
            ) from exc
        return cls(
            mode=data["mode"],
            order_fingerprint=data["order_fingerprint"],
            authorized_by=data["authorized_by"],
            authorized_at=authorized_at,
        )


class LiveExecutionPermission(_LiveValue):
    """The immutable audit record of one validated live authorization."""

    order: OrderIntent
    order_fingerprint: str = Field(pattern=_FINGERPRINT_PATTERN)
    risk_decision: RiskDecision
    authorization: HumanLiveAuthorization
    mode: Literal["LIVE"]

    @field_validator("order", mode="before")
    @classmethod
    def _requires_order(cls, value: Any) -> Any:
        if not isinstance(value, OrderIntent):
            raise LiveAuthorizationError(
                "order must be a quantforge.domain.OrderIntent"
            )
        return value

    @field_validator("risk_decision", mode="before")
    @classmethod
    def _requires_decision(cls, value: Any) -> Any:
        if not isinstance(value, RiskDecision):
            raise LiveAuthorizationError(
                "risk_decision must be a quantforge.risk.RiskDecision"
            )
        return value

    @field_validator("authorization", mode="before")
    @classmethod
    def _requires_authorization(cls, value: Any) -> Any:
        if not isinstance(value, HumanLiveAuthorization):
            raise LiveAuthorizationError(
                "authorization must be a quantforge.live.HumanLiveAuthorization"
            )
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        expected = live_order_fingerprint(self.order)
        if self.order_fingerprint != expected:
            raise LiveAuthorizationError(
                "permission fingerprint does not match the order it names"
            )
        if self.authorization.order_fingerprint != self.order_fingerprint:
            raise LiveAuthorizationError(
                "human authorization was declared for a different order"
            )
        if self.authorization.mode != self.mode:
            raise LiveAuthorizationError(
                "human authorization was not declared for live execution"
            )
        if self.risk_decision.approved is not True:
            raise LiveAuthorizationError(
                "a rejected risk decision cannot yield a live permission"
            )
        if self.authorization.authorized_at < self.order.timestamp:
            raise LiveAuthorizationError(
                "human authorization must not predate the order it authorizes"
            )
        return self

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise LiveAuthorizationError(
                f"live execution permission is invalid: {exc}"
            ) from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            data = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise LiveAuthorizationError(
                "live execution permission payload is not valid JSON"
            ) from exc
        if not isinstance(data, dict) or set(data) != _PERMISSION_FIELDS:
            raise LiveAuthorizationError(
                "live execution permission payload has unexpected fields"
            )
        try:
            order = OrderIntent.model_validate_json(json.dumps(data["order"]))
            risk_decision = RiskDecision.from_json(json.dumps(data["risk_decision"]))
            authorization = HumanLiveAuthorization.from_json(
                json.dumps(data["authorization"])
            )
        except (ValidationError, ValueError) as exc:
            raise LiveAuthorizationError(
                "live execution permission payload is invalid"
            ) from exc
        return cls(
            order=order,
            order_fingerprint=data["order_fingerprint"],
            risk_decision=risk_decision,
            authorization=authorization,
            mode=data["mode"],
        )


def live_order_fingerprint(order: OrderIntent) -> str:
    """Return the canonical versioned identity of one exact order."""
    if not isinstance(order, OrderIntent):
        raise TypeError("order must be a quantforge.domain.OrderIntent")
    payload: dict[str, str] = {
        "quantity": str(order.quantity),
        "side": order.side,
        "strategy_id": order.strategy_id,
        "symbol": order.symbol,
        "timestamp": order.timestamp.isoformat(timespec="microseconds"),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "live1-" + hashlib.sha256(canonical).hexdigest()[:32]


def create_live_authorization(
    order: OrderIntent,
    authorized_by: str,
    authorized_at: datetime,
) -> HumanLiveAuthorization:
    """Declare human authorization for one exact canonical order."""
    if not isinstance(order, OrderIntent):
        raise TypeError("order must be a quantforge.domain.OrderIntent")
    authorization = HumanLiveAuthorization(
        mode="LIVE",
        order_fingerprint=live_order_fingerprint(order),
        authorized_by=authorized_by,
        authorized_at=authorized_at,
    )
    if authorization.authorized_at < order.timestamp:
        raise LiveAuthorizationError(
            "human authorization must not predate the order it authorizes"
        )
    return authorization


def authorize_live_order(
    order: OrderIntent,
    risk_decision: RiskDecision,
    authorization: HumanLiveAuthorization,
) -> LiveExecutionPermission:
    """Validate one authorization against one order and record the result."""
    if not isinstance(order, OrderIntent):
        raise TypeError("order must be a quantforge.domain.OrderIntent")
    if not isinstance(risk_decision, RiskDecision):
        raise TypeError("risk_decision must be a quantforge.risk.RiskDecision")
    if not isinstance(authorization, HumanLiveAuthorization):
        raise TypeError(
            "authorization must be a quantforge.live.HumanLiveAuthorization"
        )
    if risk_decision.approved is not True:
        reasons = ", ".join(reason.value for reason in risk_decision.reasons)
        detail = f" ({reasons})" if reasons else ""
        raise LiveAuthorizationError(f"risk decision is not approved{detail}")
    return LiveExecutionPermission(
        order=order,
        order_fingerprint=live_order_fingerprint(order),
        risk_decision=risk_decision,
        authorization=authorization,
        mode="LIVE",
    )
