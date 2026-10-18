"""Deterministic broker submission boundary.

This module binds one exact canonical order to one valid live execution
permission and derives the stable local identity of that intended submission.
It performs no transport, holds no credentials, and never performs an external
side effect.

Two installed limits must not be overstated:

- the local submission identity is a deterministic name for one intended
  submission, not a promise that any broker de-duplicates repeated
  submissions;
- this boundary cannot observe a real dispatch frontier, so a future adapter,
  not this module, must decide whether a failure happened before or after an
  external attempt.
"""

from __future__ import annotations

import hashlib
import json
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
from ..live import LiveExecutionPermission, live_order_fingerprint

__all__ = [
    "BrokerBoundaryError",
    "BrokerPreDispatchError",
    "BrokerSubmissionEnvelope",
    "BrokerSubmissionOutcome",
    "broker_submission_id",
    "create_broker_submission_envelope",
]

_IDENTITY_PATTERN = r"^broker1-[0-9a-f]{32}$"
_ENVELOPE_FIELDS = {"order", "permission", "submission_id"}
_OUTCOME_FIELDS = {"submission_id", "status", "broker_reference", "broker_message"}


class BrokerBoundaryError(ValueError):
    """The public error boundary for local, deterministic broker validation."""


class BrokerPreDispatchError(ValueError):
    """A future adapter's proof that no external submission attempt occurred.

    Only an adapter that owns the real dispatch frontier may raise this, and
    only when it can establish that nothing was sent. This deterministic module
    never raises it, because it never dispatches anything.
    """


class _BrokerValue(BaseModel):
    """Frozen, strict, extra-forbidding base for the broker value objects."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def broker_submission_id(order: OrderIntent) -> str:
    """Return the stable local identity of one intended broker submission."""
    if not isinstance(order, OrderIntent):
        raise TypeError("order must be a quantforge.domain.OrderIntent")
    payload: dict[str, str] = {"order_fingerprint": live_order_fingerprint(order)}
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "broker1-" + hashlib.sha256(canonical).hexdigest()[:32]


class BrokerSubmissionEnvelope(_BrokerValue):
    """One exact authorized order bound to one intended submission."""

    order: OrderIntent
    permission: LiveExecutionPermission
    submission_id: str = Field(pattern=_IDENTITY_PATTERN)

    @field_validator("order", mode="before")
    @classmethod
    def _requires_order(cls, value: Any) -> Any:
        if not isinstance(value, OrderIntent):
            raise BrokerBoundaryError(
                "order must be a quantforge.domain.OrderIntent"
            )
        return value

    @field_validator("permission", mode="before")
    @classmethod
    def _requires_permission(cls, value: Any) -> Any:
        if not isinstance(value, LiveExecutionPermission):
            raise BrokerBoundaryError(
                "permission must be a quantforge.live.LiveExecutionPermission"
            )
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        expected = live_order_fingerprint(self.order)
        if self.permission.order != self.order:
            raise BrokerBoundaryError(
                "permission was issued for a different order than the envelope"
            )
        if self.permission.order_fingerprint != expected:
            raise BrokerBoundaryError(
                "permission fingerprint does not match the presented order"
            )
        if (
            self.permission.authorization.order_fingerprint
            != self.permission.order_fingerprint
        ):
            raise BrokerBoundaryError(
                "permission authorization does not match its order binding"
            )
        if self.permission.mode != "LIVE" or self.permission.authorization.mode != "LIVE":
            raise BrokerBoundaryError(
                "envelope requires a live permission for live authorization"
            )
        if self.submission_id != broker_submission_id(self.order):
            raise BrokerBoundaryError(
                "submission identity does not match the presented order"
            )
        return self

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise BrokerBoundaryError(
                f"broker submission envelope is invalid: {exc}"
            ) from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            data = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise BrokerBoundaryError(
                "broker submission envelope payload is not valid JSON"
            ) from exc
        if not isinstance(data, dict) or set(data) != _ENVELOPE_FIELDS:
            raise BrokerBoundaryError(
                "broker submission envelope payload has unexpected fields"
            )
        try:
            order = OrderIntent.model_validate_json(json.dumps(data["order"]))
            permission = LiveExecutionPermission.from_json(
                json.dumps(data["permission"])
            )
        except (ValidationError, ValueError) as exc:
            raise BrokerBoundaryError(
                "broker submission envelope payload is invalid"
            ) from exc
        return cls(
            order=order,
            permission=permission,
            submission_id=data["submission_id"],
        )


class BrokerSubmissionOutcome(_BrokerValue):
    """The broker-neutral result of one attempted submission.

    ACCEPTED means a broker acknowledged the submission. It does not mean the
    order executed, and it never creates canonical execution evidence.
    OUTCOME_UNKNOWN means an external attempt may have happened while
    acceptance or rejection cannot be established; it must never be treated as
    a rejection or as permission to submit again.
    """

    submission_id: str = Field(pattern=_IDENTITY_PATTERN)
    status: Literal["ACCEPTED", "REJECTED", "OUTCOME_UNKNOWN"]
    broker_reference: str | None = None
    broker_message: str | None = None

    @field_validator("broker_reference", "broker_message")
    @classmethod
    def _non_empty_when_present(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise BrokerBoundaryError(
                "optional broker evidence must be non-empty when present"
            )
        return value

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise BrokerBoundaryError(
                f"broker submission outcome is invalid: {exc}"
            ) from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            data = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise BrokerBoundaryError(
                "broker submission outcome payload is not valid JSON"
            ) from exc
        if not isinstance(data, dict) or set(data) != _OUTCOME_FIELDS:
            raise BrokerBoundaryError(
                "broker submission outcome payload has unexpected fields"
            )
        return cls(
            submission_id=data["submission_id"],
            status=data["status"],
            broker_reference=data["broker_reference"],
            broker_message=data["broker_message"],
        )


def create_broker_submission_envelope(
    order: OrderIntent,
    permission: LiveExecutionPermission,
) -> BrokerSubmissionEnvelope:
    """Bind one exact order to one valid live permission before any dispatch."""
    if not isinstance(order, OrderIntent):
        raise TypeError("order must be a quantforge.domain.OrderIntent")
    if not isinstance(permission, LiveExecutionPermission):
        raise TypeError(
            "permission must be a quantforge.live.LiveExecutionPermission"
        )
    return BrokerSubmissionEnvelope(
        order=order,
        permission=permission,
        submission_id=broker_submission_id(order),
    )
