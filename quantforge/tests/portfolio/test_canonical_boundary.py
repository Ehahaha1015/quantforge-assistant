from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantforge.domain import Position
from quantforge.portfolio import (
    PortfolioError,
    PortfolioState,
    PortfolioValuation,
    PositionValuation,
    create_portfolio,
    value_portfolio,
)

T0 = datetime(2025, 1, 2, tzinfo=UTC)


def test_state_model_boundary_surfaces_portfolio_error() -> None:
    with pytest.raises(PortfolioError, match="portfolio state is invalid"):
        PortfolioState(initial_cash=1.5, cash=Decimal(0))
    with pytest.raises(PortfolioError, match="one position per symbol"):
        PortfolioState(
            initial_cash=Decimal(0),
            cash=Decimal(0),
            positions=(
                Position(symbol="AAPL", quantity=Decimal(1), average_price=Decimal(1)),
                Position(symbol="AAPL", quantity=Decimal(1), average_price=Decimal(1)),
            ),
        )
    with pytest.raises(PortfolioError, match="payload is invalid"):
        PortfolioState.from_json("{}")


def test_valuation_model_boundary_and_canonical_position_order() -> None:
    state = create_portfolio(Decimal(0))
    state = PortfolioState(
        initial_cash=Decimal(0),
        cash=Decimal(0),
        positions=(
            Position(symbol="ZZZ", quantity=Decimal(1), average_price=Decimal(2)),
            Position(symbol="AAA", quantity=Decimal(1), average_price=Decimal(2)),
        ),
    )
    valuation = value_portfolio(state, {"ZZZ": Decimal(3), "AAA": Decimal(4)}, T0)
    assert [position.symbol for position in valuation.positions] == ["AAA", "ZZZ"]
    reversed_valuation = PortfolioValuation(
        as_of=T0,
        cash=Decimal(0),
        cumulative_fees=Decimal(0),
        total_market_value=Decimal(7),
        equity=Decimal(7),
        positions=tuple(reversed(valuation.positions)),
    )
    assert [position.symbol for position in reversed_valuation.positions] == ["AAA", "ZZZ"]
    with pytest.raises(PortfolioError, match="portfolio valuation is invalid"):
        PortfolioValuation(
            as_of=T0.replace(tzinfo=None),
            cash=Decimal(0),
            cumulative_fees=Decimal(0),
            total_market_value=Decimal(0),
            equity=Decimal(0),
            positions=(),
        )
    assert state.positions[0].symbol == "AAA"
    assert isinstance(valuation.positions[0], PositionValuation)
