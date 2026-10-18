"""Deterministic paper execution for QuantForge.

``execute_paper_order`` turns one canonical ``OrderIntent`` plus one explicit
execution instruction into one canonical ``Fill``.  It is a pure function:
no market data, no clock, no persistence, and no accounting.

Authorization limitation
------------------------

This boundary verifies approval semantics only.  The frozen RISK-001
``RiskDecision`` carries no order identity, fingerprint, or provenance, so
paper execution cannot verify that the supplied authorization was produced
for this exact ``OrderIntent``.  Pairing a decision with the order it
authorized is the caller's responsibility, and this module deliberately
invents no binding mechanism of its own.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Any

from pydantic import ValidationError

from ...domain import Fill, OrderIntent
from ...risk import RiskDecision

__all__ = ["PaperExecutionError", "execute_paper_order"]

_ZERO = Decimal(0)


class PaperExecutionError(ValueError):
    """The public error boundary for deterministic paper execution."""


def _decimal(value: Any, *, name: str) -> Decimal:
    """Coerce one explicit execution input; floats and bools are never exact."""
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int, str)):
        raise PaperExecutionError(
            f"{name} must be an exact Decimal, int, or str, not {type(value).__name__}"
        )
    try:
        number = Decimal(value)
    except (DecimalException, ValueError) as exc:
        raise PaperExecutionError(f"{name} must be a decimal number") from exc
    if not number.is_finite():
        raise PaperExecutionError(f"{name} must be finite")
    return number


def _utc(value: Any, *, name: str) -> datetime:
    """Require one timezone-aware datetime and normalize it to UTC."""
    if not isinstance(value, datetime):
        raise PaperExecutionError(f"{name} must be a datetime")
    try:
        aware = value.tzinfo is not None and value.utcoffset() is not None
    except (OverflowError, ValueError) as exc:
        raise PaperExecutionError(f"{name} has an unusable UTC offset") from exc
    if not aware:
        raise PaperExecutionError(f"{name} must be timezone-aware")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise PaperExecutionError(
            f"{name} is outside the representable UTC range"
        ) from exc


def _order_id(order: OrderIntent) -> str:
    """Derive the deterministic paper identity of one canonical order."""
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
    return "paper1-" + hashlib.sha256(canonical).hexdigest()[:32]


def execute_paper_order(
    order: OrderIntent,
    risk_decision: RiskDecision,
    execution_price: Decimal | int | str,
    execution_timestamp: datetime,
    execution_fee: Decimal | int | str,
) -> Fill:
    """Build the canonical Fill for one approved, explicitly priced order."""
    if not isinstance(order, OrderIntent):
        raise TypeError("order must be a quantforge.domain.OrderIntent")
    if not isinstance(risk_decision, RiskDecision):
        raise TypeError("risk_decision must be a quantforge.risk.RiskDecision")
    if risk_decision.approved is not True:
        reasons = ", ".join(reason.value for reason in risk_decision.reasons)
        detail = f" ({reasons})" if reasons else ""
        raise PaperExecutionError(f"risk authorization is not approved{detail}")

    price = _decimal(execution_price, name="execution_price")
    if price <= _ZERO:
        raise PaperExecutionError("execution_price must be greater than zero")
    fee = _decimal(execution_fee, name="execution_fee")
    if fee < _ZERO:
        raise PaperExecutionError("execution_fee must be non-negative")
    moment = _utc(execution_timestamp, name="execution_timestamp")
    if moment < order.timestamp:
        raise PaperExecutionError(
            "execution_timestamp must not precede order.timestamp"
        )

    try:
        return Fill(
            order_id=_order_id(order),
            symbol=order.symbol,
            timestamp=moment,
            side=order.side,
            quantity=order.quantity,
            price=price,
            fee=fee,
        )
    except ValidationError as exc:
        raise PaperExecutionError(f"paper fill is invalid: {exc}") from exc
