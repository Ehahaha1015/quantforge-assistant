from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from ..domain import Fill, Position

_ZERO = Decimal(0)


class PortfolioError(ValueError):
    """A portfolio-layer accounting, chronology, or valuation failure."""


def _decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int, str)):
        raise PortfolioError(f"{name} must be an exact Decimal or int, not {type(value).__name__}")
    try:
        number = Decimal(value)
    except (DecimalException, ValueError) as exc:
        raise PortfolioError(f"{name} must be a decimal number") from exc
    if not number.is_finite():
        raise PortfolioError(f"{name} must be finite")
    return number


def _utc(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PortfolioError(f"{name} must be timezone-aware")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise PortfolioError(f"{name} is outside the representable UTC range") from exc


class PortfolioState(BaseModel):
    """Immutable long-only accounting state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_cash: Decimal
    cash: Decimal
    positions: tuple[Position, ...] = ()
    cumulative_fees: Decimal = _ZERO
    last_fill_timestamp: datetime | None = None

    @field_validator("initial_cash", "cash", "cumulative_fees", mode="before")
    @classmethod
    def _money(cls, value: Any, info: Any) -> Decimal:
        return _decimal(value, name=str(info.field_name))

    @field_validator("last_fill_timestamp")
    @classmethod
    def _timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value, name="last_fill_timestamp")

    @model_validator(mode="after")
    def _canonical_positions(self) -> Self:
        symbols = [position.symbol for position in self.positions]
        if len(set(symbols)) != len(symbols):
            raise PortfolioError("portfolio state must hold at most one position per symbol")
        for position in self.positions:
            if position.quantity <= 0:
                raise PortfolioError(
                    f"stored position quantity must be positive for {position.symbol}"
                )
        ordered = tuple(sorted(self.positions, key=lambda position: position.symbol))
        if ordered != self.positions:
            object.__setattr__(self, "positions", ordered)
        return self

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            return cls.model_validate_json(value)
        except ValidationError as exc:
            raise PortfolioError(f"portfolio state payload is invalid: {exc}") from exc

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise PortfolioError(f"portfolio state is invalid: {exc}") from exc


class PositionValuation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    quantity: Decimal
    average_price: Decimal
    mark_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal


class PortfolioValuation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    cash: Decimal
    cumulative_fees: Decimal
    total_market_value: Decimal
    equity: Decimal
    positions: tuple[PositionValuation, ...] = ()

    @field_validator("as_of")
    @classmethod
    def _timestamp(cls, value: datetime) -> datetime:
        return _utc(value, name="as_of")

    @model_validator(mode="after")
    def _canonical_positions(self) -> Self:
        ordered = tuple(sorted(self.positions, key=lambda position: position.symbol))
        if ordered != self.positions:
            object.__setattr__(self, "positions", ordered)
        return self

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise PortfolioError(f"portfolio valuation is invalid: {exc}") from exc


def create_portfolio(initial_cash: Decimal | int) -> PortfolioState:
    """Start an empty portfolio; initial cash must be an exact non-negative Decimal."""
    amount = _decimal(initial_cash, name="initial_cash")
    if amount < 0:
        raise PortfolioError("initial_cash must be greater than or equal to zero")
    return PortfolioState(initial_cash=amount, cash=amount)


def apply_fill(state: PortfolioState, fill: Fill) -> PortfolioState:
    """Apply one factual canonical Fill without any risk policy.

    Cash may go negative, which is a fact rather than a rejected state; the
    only structural rejection is a SELL that would create a short position.
    """
    if not isinstance(state, PortfolioState):
        raise TypeError("state must be a PortfolioState")
    if not isinstance(fill, Fill):
        raise TypeError("fill must be a quantforge.domain.Fill")
    if state.last_fill_timestamp is not None and fill.timestamp < state.last_fill_timestamp:
        raise PortfolioError("fills must be applied in chronological order")

    held = {position.symbol: position for position in state.positions}
    existing = held.get(fill.symbol)
    try:
        if fill.side == "BUY":
            cash = state.cash - fill.quantity * fill.price - fill.fee
            if existing is None:
                position: Position | None = Position(
                    symbol=fill.symbol, quantity=fill.quantity, average_price=fill.price
                )
            else:
                quantity = existing.quantity + fill.quantity
                average_price = (
                    existing.quantity * existing.average_price
                    + fill.quantity * fill.price
                ) / quantity
                position = Position(
                    symbol=existing.symbol, quantity=quantity, average_price=average_price
                )
        else:
            if existing is None:
                raise PortfolioError(f"cannot sell {fill.symbol} without an open position")
            if fill.quantity > existing.quantity:
                raise PortfolioError(f"sell quantity exceeds the held quantity for {fill.symbol}")
            cash = state.cash + fill.quantity * fill.price - fill.fee
            remaining = existing.quantity - fill.quantity
            position = (
                None
                if remaining == 0
                else Position(
                    symbol=existing.symbol,
                    quantity=remaining,
                    average_price=existing.average_price,
                )
            )
    except DecimalException as exc:
        raise PortfolioError(f"fill arithmetic is not representable: {exc}") from exc

    if position is None:
        del held[fill.symbol]
    else:
        held[fill.symbol] = position
    try:
        fees = state.cumulative_fees + fill.fee
    except DecimalException as exc:
        raise PortfolioError(f"fill arithmetic is not representable: {exc}") from exc
    return PortfolioState(
        initial_cash=state.initial_cash,
        cash=cash,
        positions=tuple(held.values()),
        cumulative_fees=fees,
        last_fill_timestamp=fill.timestamp,
    )


def value_portfolio(
    state: PortfolioState,
    mark_prices: Mapping[str, Decimal | int],
    as_of: datetime,
) -> PortfolioValuation:
    """Value the portfolio from explicit marks and an explicit valuation time."""
    if not isinstance(state, PortfolioState):
        raise TypeError("state must be a PortfolioState")
    moment = _utc(as_of, name="as_of")
    if state.last_fill_timestamp is not None and moment < state.last_fill_timestamp:
        raise PortfolioError("as_of must not precede the last fill timestamp")
    if not isinstance(mark_prices, Mapping):
        raise TypeError("mark_prices must be a mapping of symbol to exact Decimal")

    marks: dict[str, Decimal] = {}
    for symbol, price in mark_prices.items():
        if not isinstance(symbol, str) or not symbol:
            raise PortfolioError("mark symbols must be non-empty strings")
        number = _decimal(price, name=f"mark price for {symbol}")
        if number <= 0:
            raise PortfolioError(f"mark price for {symbol} must be positive")
        marks[symbol] = number
    expected = {position.symbol for position in state.positions}
    if set(marks) != expected:
        missing = sorted(expected - set(marks))
        extra = sorted(set(marks) - expected)
        raise PortfolioError(f"mark coverage must match open positions; missing={missing}, extra={extra}")

    valuations: list[PositionValuation] = []
    total = _ZERO
    try:
        for position in state.positions:
            mark = marks[position.symbol]
            market_value = position.quantity * mark
            unrealized = position.quantity * (mark - position.average_price)
            if not market_value.is_finite() or not unrealized.is_finite():
                raise PortfolioError("valuation arithmetic is not finite")
            valuations.append(
                PositionValuation(
                    symbol=position.symbol,
                    quantity=position.quantity,
                    average_price=position.average_price,
                    mark_price=mark,
                    market_value=market_value,
                    unrealized_pnl=unrealized,
                )
            )
            total += market_value
        equity = state.cash + total
        if not total.is_finite() or not equity.is_finite():
            raise PortfolioError("valuation arithmetic is not finite")
    except DecimalException as exc:
        raise PortfolioError(f"valuation arithmetic is not representable: {exc}") from exc
    return PortfolioValuation(
        as_of=moment,
        cash=state.cash,
        cumulative_fees=state.cumulative_fees,
        total_market_value=total,
        equity=equity,
        positions=tuple(valuations),
    )
