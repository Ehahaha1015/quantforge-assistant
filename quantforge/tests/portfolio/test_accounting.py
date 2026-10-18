from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantforge.domain import Fill
from quantforge.portfolio import (
    PortfolioError,
    PortfolioState,
    apply_fill,
    create_portfolio,
    value_portfolio,
)

T0 = datetime(2025, 1, 2, tzinfo=UTC)


def fill(symbol="AAPL", side="BUY", quantity="10", price="10", fee="1", minutes=0) -> Fill:
    return Fill(
        order_id=f"order-{symbol}-{minutes}-{side}",
        symbol=symbol,
        timestamp=T0 + timedelta(minutes=minutes),
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
    )


def test_hand_calculated_accounting_oracle() -> None:
    state = create_portfolio(Decimal(1000))
    state = apply_fill(state, fill(quantity="10", price="10", fee="1"))
    assert state.cash == Decimal(899)
    assert state.positions[0].quantity == Decimal(10)
    assert state.positions[0].average_price == Decimal(10)

    state = apply_fill(state, fill(quantity="10", price="20", fee="2", minutes=1))
    assert state.cash == Decimal(697)
    assert state.positions[0].quantity == Decimal(20)
    assert state.positions[0].average_price == Decimal(15)

    state = apply_fill(state, fill(side="SELL", quantity="5", price="30", fee="1", minutes=2))
    assert state.cash == Decimal(846)
    assert state.positions[0].quantity == Decimal(15)
    assert state.positions[0].average_price == Decimal(15)
    assert state.cumulative_fees == Decimal(4)

    valuation = value_portfolio(state, {"AAPL": Decimal(25)}, T0 + timedelta(minutes=3))
    assert valuation.total_market_value == Decimal(375)
    assert valuation.equity == Decimal(1221)
    position = valuation.positions[0]
    assert position.market_value == Decimal(375)
    assert position.unrealized_pnl == Decimal(150)


def test_full_sell_removes_the_position_and_sell_cannot_oversell() -> None:
    state = apply_fill(create_portfolio(1000), fill(quantity="4", price="10", fee="0"))
    closed = apply_fill(state, fill(side="SELL", quantity="4", price="12", fee="0", minutes=1))
    assert closed.positions == ()
    assert closed.cash == Decimal(1008)
    assert value_portfolio(closed, {}, T0 + timedelta(minutes=1)).total_market_value == 0
    with pytest.raises(PortfolioError, match="exceeds the held quantity"):
        apply_fill(state, fill(side="SELL", quantity="5", price="12", fee="0", minutes=1))
    with pytest.raises(PortfolioError, match="without an open position"):
        apply_fill(create_portfolio(1000), fill(side="SELL", quantity="1", price="12", fee="0"))


def test_no_risk_policy_negative_cash_and_equity_are_facts() -> None:
    state = apply_fill(create_portfolio(100), fill(quantity="20", price="10", fee="0"))
    assert state.cash == Decimal(-100)
    valuation = value_portfolio(state, {"AAPL": Decimal(1)}, T0)
    assert valuation.total_market_value == Decimal(20)
    assert valuation.equity == Decimal(-80)


def test_fees_are_counted_once_and_never_enter_average_price() -> None:
    state = apply_fill(create_portfolio(1000), fill(quantity="5", price="10", fee="7"))
    assert state.cash == Decimal(943)
    assert state.positions[0].average_price == Decimal(10)
    assert state.cumulative_fees == Decimal(7)
    state = apply_fill(state, fill(quantity="5", price="20", fee="3", minutes=1))
    assert state.positions[0].average_price == Decimal(15)
    assert state.cumulative_fees == Decimal(10)
    assert state.positions[0].average_price != Decimal(16)


def test_chronology_and_same_timestamp() -> None:
    state = apply_fill(create_portfolio(1000), fill(minutes=5))
    with pytest.raises(PortfolioError, match="chronological"):
        apply_fill(state, fill(quantity="1", price="5", fee="0", minutes=4))
    same = apply_fill(state, fill(quantity="1", price="5", fee="0", minutes=5))
    assert same.positions[0].quantity == Decimal(11)
    with pytest.raises(PortfolioError, match="must not precede"):
        value_portfolio(state, {"AAPL": Decimal(10)}, T0 + timedelta(minutes=4))


def test_symbol_ordering_is_canonical_and_input_state_is_immutable() -> None:
    state = create_portfolio(1000)
    for symbol in ("ZZZ", "AAA", "MMM"):
        state = apply_fill(state, fill(symbol=symbol, quantity="1", price="10", fee="0"))
    assert [position.symbol for position in state.positions] == ["AAA", "MMM", "ZZZ"]
    reversed_state = PortfolioState(
        initial_cash=Decimal(0), cash=Decimal(0), positions=tuple(reversed(state.positions))
    )
    assert [position.symbol for position in reversed_state.positions] == ["AAA", "MMM", "ZZZ"]
    with pytest.raises(ValidationError):
        state.cash = Decimal(0)  # type: ignore[misc]
    before = state
    apply_fill(state, fill(symbol="BBB", quantity="1", price="10", fee="0", minutes=1))
    assert state == before
    assert [position.symbol for position in state.positions] == ["AAA", "MMM", "ZZZ"]


@pytest.mark.parametrize(
    "marks,message",
    [
        ({}, "missing="),
        ({"AAPL": Decimal(10), "MSFT": Decimal(10)}, "extra="),
        ({"AAPL": Decimal(0)}, "must be positive"),
        ({"AAPL": Decimal(-1)}, "must be positive"),
        ({"AAPL": 10.0}, "must be an exact Decimal"),
        ({"AAPL": True}, "must be an exact Decimal"),
        ({"AAPL": Decimal("NaN")}, "must be finite"),
        ({"AAPL": Decimal("Infinity")}, "must be finite"),
    ],
)
def test_mark_coverage_is_exact_and_exact_decimal(marks, message) -> None:
    state = apply_fill(create_portfolio(1000), fill(quantity="1", price="10", fee="0"))
    with pytest.raises(PortfolioError, match=message):
        value_portfolio(state, marks, T0)


def test_flat_portfolio_requires_empty_marks() -> None:
    state = create_portfolio(1000)
    assert value_portfolio(state, {}, T0).equity == Decimal(1000)
    with pytest.raises(PortfolioError, match="extra="):
        value_portfolio(state, {"AAPL": Decimal(10)}, T0)


@pytest.mark.parametrize("bad", [1.5, True, Decimal("NaN"), Decimal("-Infinity")])
def test_non_exact_or_non_finite_cash_is_rejected(bad) -> None:
    with pytest.raises((PortfolioError, ValidationError)):
        create_portfolio(bad)


def test_serialization_round_trips_with_decimals_and_timestamps() -> None:
    state = apply_fill(create_portfolio(Decimal("1000.5")), fill(quantity="2", price="3.25", fee="0.25"))
    restored = PortfolioState.from_json(state.model_dump_json())
    assert restored == state
    assert restored.cash == Decimal("993.75")
    assert restored.last_fill_timestamp == T0
    valuation = value_portfolio(state, {"AAPL": Decimal("4.5")}, T0)
    assert valuation.as_of.tzinfo is not None
    assert type(valuation).model_validate_json(valuation.model_dump_json()) == valuation
    assert state.model_dump_json() == state.model_dump_json()


def test_canonical_domain_fill_integration() -> None:
    canonical = Fill(
        order_id="domain-order-1",
        symbol="MSFT",
        timestamp=T0,
        side="BUY",
        quantity=Decimal(3),
        price=Decimal(100),
        fee=Decimal(1),
    )
    state = apply_fill(create_portfolio(Decimal(500)), canonical)
    assert state.cash == Decimal(199)
    assert state.positions[0] == type(state.positions[0])(
        symbol="MSFT", quantity=Decimal(3), average_price=Decimal(100)
    )
    with pytest.raises(TypeError, match="domain.Fill"):
        apply_fill(state, {"symbol": "MSFT"})  # type: ignore[arg-type]
