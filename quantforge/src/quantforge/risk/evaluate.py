from __future__ import annotations

from collections.abc import Mapping
from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Underflow,
    localcontext,
)
from typing import Any

from ..domain import OrderIntent
from ..portfolio import PortfolioState
from .values import RiskDecision, RiskError, RiskLimits, RiskReason, _decimal

_ZERO = Decimal(0)
_EXACT_TRAPS = {
    Clamped,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Underflow,
}


def _shape(value: Decimal) -> tuple[int, int]:
    """(coefficient width, exponent) of a finite Decimal."""
    _sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)  # finite Decimals always carry an int exponent
    return (len(digits) if digits else 1, exponent)


def _exact_context(precision: int) -> Context:
    """Build the isolated, deterministic context used by exact primitives."""
    if isinstance(precision, bool) or not isinstance(precision, int):
        raise RiskError("risk arithmetic precision must be an integer")
    if not 1 <= precision <= MAX_PREC:
        raise RiskError("risk arithmetic precision is outside the Decimal range")
    context = Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
    )
    for signal in context.traps:
        context.traps[signal] = signal in _EXACT_TRAPS
    return context


def _exact_mul(left: Decimal, right: Decimal) -> Decimal:
    """Exact product: needs len(left.digits) + len(right.digits) coefficients."""
    left_width, _left_exponent = _shape(left)
    right_width, _right_exponent = _shape(right)
    with localcontext(_exact_context(left_width + right_width)):
        return left * right


def _exact_sum(left: Decimal, right: Decimal, *, subtract: bool) -> Decimal:
    """Exact sum or difference: needs the aligned span plus one carry digit."""
    left_width, left_exponent = _shape(left)
    right_width, right_exponent = _shape(right)
    adjusted = (left_width + left_exponent - 1, right_width + right_exponent - 1)
    span = max(adjusted) - min(left_exponent, right_exponent) + 1
    with localcontext(_exact_context(span + 1)):
        return left - right if subtract else left + right


def _exact_add(left: Decimal, right: Decimal) -> Decimal:
    return _exact_sum(left, right, subtract=False)


def _exact_sub(left: Decimal, right: Decimal) -> Decimal:
    return _exact_sum(left, right, subtract=True)


def _risk_prices(
    order: OrderIntent, portfolio: PortfolioState, risk_prices: Mapping[str, Any]
) -> dict[str, Decimal]:
    if not isinstance(risk_prices, Mapping):
        raise TypeError("risk_prices must be a mapping of symbol to exact Decimal")
    cleaned: dict[str, Decimal] = {}
    for symbol, price in risk_prices.items():
        if not isinstance(symbol, str) or not symbol:
            raise RiskError("risk price symbols must be non-empty strings")
        number = _decimal(price, name=f"risk price for {symbol}")
        if number <= 0:
            raise RiskError(f"risk price for {symbol} must be greater than zero")
        cleaned[symbol] = number
    required = {position.symbol for position in portfolio.positions} | {order.symbol}
    if set(cleaned) != required:
        raise RiskError(
            "risk price coverage must equal open position symbols union the order symbol; "
            f"missing={sorted(required - set(cleaned))}, extra={sorted(set(cleaned) - required)}"
        )
    return cleaned


def evaluate_order(
    order: OrderIntent,
    portfolio: PortfolioState,
    risk_prices: Mapping[str, Any],
    estimated_fee: Any,
    limits: RiskLimits,
) -> RiskDecision:
    """Deterministic pre-trade evaluation of one canonical OrderIntent.

    Returns a normal ``RiskDecision`` for policy rejections and raises
    ``RiskError`` when the evaluation itself is invalid.  It never creates a
    Fill, mutates the portfolio, or reaches execution.
    """
    if not isinstance(order, OrderIntent):
        raise TypeError("order must be a quantforge.domain.OrderIntent")
    if not isinstance(portfolio, PortfolioState):
        raise TypeError("portfolio must be a quantforge.portfolio.PortfolioState")
    if not isinstance(limits, RiskLimits):
        raise TypeError("limits must be RiskLimits")
    fee = _decimal(estimated_fee, name="estimated_fee")
    if fee < 0:
        raise RiskError("estimated_fee must be non-negative")
    if portfolio.last_fill_timestamp is not None and order.timestamp < portfolio.last_fill_timestamp:
        raise RiskError("order timestamp must not precede the portfolio state")
    prices = _risk_prices(order, portfolio, risk_prices)
    held = {position.symbol: position for position in portfolio.positions}
    with localcontext(_exact_context(MAX_PREC)):
        # Every operation below uses an operation-specific exact primitive
        # whose bound is derived from its own operands.  This outer context is
        # also isolated so comparisons and boundary handling do not inherit
        # caller exponent or trap settings.
        try:
            notional = _exact_mul(order.quantity, prices[order.symbol])
            reasons: set[RiskReason] = set()
            if notional > limits.max_order_notional:
                reasons.add(RiskReason.ORDER_NOTIONAL_LIMIT)
            if order.side == "BUY":
                if _exact_sub(_exact_sub(portfolio.cash, notional), fee) < _ZERO:
                    reasons.add(RiskReason.INSUFFICIENT_CASH)
                existing = held.get(order.symbol)
                quantity_after = _exact_add(
                    existing.quantity if existing is not None else _ZERO, order.quantity
                )
                position_notional = _exact_mul(quantity_after, prices[order.symbol])
                if position_notional > limits.max_position_notional:
                    reasons.add(RiskReason.POSITION_NOTIONAL_LIMIT)
                gross_exposure = position_notional
                for other_symbol, other_position in held.items():
                    if other_symbol != order.symbol:
                        gross_exposure = _exact_add(
                            gross_exposure,
                            _exact_mul(other_position.quantity, prices[other_symbol]),
                        )
                if gross_exposure > limits.max_gross_exposure:
                    reasons.add(RiskReason.GROSS_EXPOSURE_LIMIT)
            else:
                position = held.get(order.symbol)
                if position is None or order.quantity > position.quantity:
                    reasons.add(RiskReason.SELL_EXCEEDS_POSITION)
        except DecimalException as exc:
            raise RiskError(f"risk arithmetic is not representable: {exc}") from exc
    canonical = tuple(reason for reason in RiskReason if reason in reasons)
    return RiskDecision(approved=not canonical, reasons=canonical)
