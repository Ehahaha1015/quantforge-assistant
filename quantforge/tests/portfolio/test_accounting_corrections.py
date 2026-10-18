from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantforge.domain import Fill, Position
from quantforge.portfolio import (
    PortfolioError,
    PortfolioState,
    apply_fill,
    create_portfolio,
    value_portfolio,
)

T0 = datetime(2025, 1, 2, tzinfo=UTC)


def fill(quantity="1", price="10", fee="0", side="BUY", minutes=0) -> Fill:
    return Fill(
        order_id=f"correction-{minutes}-{side}",
        symbol="AAPL",
        timestamp=T0 + timedelta(minutes=minutes),
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
    )


@pytest.mark.parametrize("bad", [Decimal(-1), Decimal(-100)])
def test_negative_initial_cash_is_rejected(bad: Decimal) -> None:
    with pytest.raises(PortfolioError, match="greater than or equal to zero"):
        create_portfolio(bad)


def test_zero_initial_cash_is_valid() -> None:
    state = create_portfolio(Decimal(0))
    assert state.cash == Decimal(0)
    assert state.initial_cash == Decimal(0)


def test_initial_cash_is_retained_preserved_and_serialized() -> None:
    state = create_portfolio(Decimal(1000))
    assert state.initial_cash == Decimal(1000)
    after_buy = apply_fill(state, fill(quantity="10", price="10", fee="1"))
    assert after_buy.initial_cash == Decimal(1000)
    assert after_buy.cash == Decimal(899)
    after_sell = apply_fill(after_buy, fill(quantity="4", price="12", fee="1", side="SELL", minutes=1))
    assert after_sell.initial_cash == Decimal(1000)
    assert after_sell.cash == Decimal(946)
    restored = PortfolioState.from_json(after_sell.model_dump_json())
    assert restored.initial_cash == Decimal(1000)
    assert restored == after_sell
    assert '"initial_cash":"1000"' in after_sell.model_dump_json()


def test_direct_state_construction_enforces_canonical_invariants() -> None:
    held = Position(symbol="AAPL", quantity=Decimal(2), average_price=Decimal(10))
    duplicate = Position(symbol="AAPL", quantity=Decimal(1), average_price=Decimal(9))
    with pytest.raises((PortfolioError, ValidationError), match="one position per symbol"):
        PortfolioState(initial_cash=Decimal(0), cash=Decimal(0), positions=(held, duplicate))
    for quantity in (Decimal(0), Decimal(-2)):
        invalid = Position(symbol="MSFT", quantity=quantity, average_price=Decimal(10))
        with pytest.raises((PortfolioError, ValidationError), match="quantity must be positive"):
            PortfolioState(initial_cash=Decimal(0), cash=Decimal(0), positions=(invalid,))
    shuffled = PortfolioState(
        initial_cash=Decimal(0),
        cash=Decimal(0),
        positions=(
            Position(symbol="ZZZ", quantity=Decimal(1), average_price=Decimal(1)),
            Position(symbol="AAA", quantity=Decimal(1), average_price=Decimal(1)),
        ),
    )
    assert [position.symbol for position in shuffled.positions] == ["AAA", "ZZZ"]


def test_derived_decimal_failures_become_portfolio_errors() -> None:
    huge = Decimal("1E+999999")
    state = apply_fill(create_portfolio(0), fill(quantity="1", price="10"))
    with pytest.raises(PortfolioError, match="not representable"):
        apply_fill(state, fill(quantity=str(huge), price=str(huge), minutes=1))
    held = PortfolioState(
        initial_cash=Decimal(0),
        cash=Decimal(0),
        positions=(Position(symbol="AAPL", quantity=huge, average_price=Decimal(1)),),
    )
    with pytest.raises(PortfolioError, match="not representable|not finite"):
        value_portfolio(held, {"AAPL": huge}, T0)
