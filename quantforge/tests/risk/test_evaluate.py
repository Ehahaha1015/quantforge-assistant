from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantforge.domain import Fill, OrderIntent
from quantforge.portfolio import PortfolioState, apply_fill, create_portfolio
from quantforge.risk import (
    RiskDecision,
    RiskError,
    RiskLimits,
    RiskReason,
    evaluate_order,
)

T0 = datetime(2025, 1, 2, tzinfo=UTC)
LIMITS = RiskLimits(
    max_order_notional=Decimal(10000),
    max_position_notional=Decimal(50000),
    max_gross_exposure=Decimal(100000),
)


def intent(symbol="AAPL", side="BUY", quantity="10", price="10", minutes=0) -> OrderIntent:
    return OrderIntent(
        strategy_id="risk-test",
        symbol=symbol,
        timestamp=T0 + timedelta(minutes=minutes),
        side=side,
        quantity=Decimal(quantity),
    )


def portfolio_with(*fills: Fill) -> PortfolioState:
    state = create_portfolio(Decimal(100000))
    for item in fills:
        state = apply_fill(state, item)
    return state


def fill(symbol="AAPL", side="BUY", quantity="10", price="10", minutes=0) -> Fill:
    return Fill(
        order_id=f"f-{symbol}-{minutes}",
        symbol=symbol,
        timestamp=T0 + timedelta(minutes=minutes),
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(0),
    )


def test_flat_buy_requires_exactly_the_order_symbol_price() -> None:
    flat = create_portfolio(Decimal(1000))
    assert evaluate_order(intent(), flat, {"AAPL": Decimal(10)}, Decimal(0), LIMITS).approved
    with pytest.raises(RiskError, match="coverage"):
        evaluate_order(intent(), flat, {}, Decimal(0), LIMITS)
    with pytest.raises(RiskError, match="coverage"):
        evaluate_order(
            intent(), flat, {"AAPL": Decimal(10), "MSFT": Decimal(10)}, Decimal(0), LIMITS
        )


def test_price_and_fee_validation() -> None:
    flat = create_portfolio(Decimal(1000))
    for bad in (0, -1, Decimal("NaN"), Decimal("Infinity"), 1.5, True):
        with pytest.raises(RiskError):
            evaluate_order(intent(), flat, {"AAPL": bad}, Decimal(0), LIMITS)
    for bad in (-1, Decimal("NaN"), Decimal("-Infinity"), 1.5, True):
        with pytest.raises(RiskError):
            evaluate_order(intent(), flat, {"AAPL": Decimal(10)}, bad, LIMITS)


def test_stale_order_timestamp_is_invalid() -> None:
    portfolio = portfolio_with(fill(minutes=5))
    with pytest.raises(RiskError, match="timestamp"):
        evaluate_order(
            intent(side="BUY", quantity="1", minutes=4), portfolio, {"AAPL": Decimal(10)}, 0, LIMITS
        )
    equal = evaluate_order(
        intent(side="BUY", quantity="1", minutes=5), portfolio, {"AAPL": Decimal(10)}, 0, LIMITS
    )
    assert equal.approved


def test_buy_limit_oracles_and_reason_ordering() -> None:
    flat = create_portfolio(Decimal(1000))
    # order notional 10 * 10 = 100 ok; cash 1000 - 100 - 1 = 899 >= 0
    assert evaluate_order(intent(), flat, {"AAPL": Decimal(10)}, Decimal(1), LIMITS).approved
    over_cash = evaluate_order(intent(quantity="150"), flat, {"AAPL": Decimal(10)}, Decimal(0), LIMITS)
    assert over_cash.approved is False
    assert over_cash.reasons == (RiskReason.INSUFFICIENT_CASH,)
    everything = evaluate_order(
        intent(quantity="20000"),
        create_portfolio(Decimal(0)),
        {"AAPL": Decimal(10)},
        Decimal(0),
        LIMITS,
    )
    assert everything.reasons == (
        RiskReason.ORDER_NOTIONAL_LIMIT,
        RiskReason.INSUFFICIENT_CASH,
        RiskReason.POSITION_NOTIONAL_LIMIT,
        RiskReason.GROSS_EXPOSURE_LIMIT,
    )


def test_position_and_gross_exposure_oracles() -> None:
    flat = create_portfolio(Decimal(100000))
    # order 800 * 10 = 8000 <= 10000; position 8000 <= 50000; gross 8000 <= 100000
    assert evaluate_order(intent(quantity="800"), flat, {"AAPL": Decimal(10)}, 0, LIMITS).approved
    # existing AAPL 45000 + order 9000 = 54000 position notional > 50000, order notional ok
    rich = portfolio_with(fill(quantity="4500", price="10"))
    position_breach = evaluate_order(
        intent(quantity="900"), rich, {"AAPL": Decimal(10)}, 0, LIMITS
    )
    assert position_breach.reasons == (RiskReason.POSITION_NOTIONAL_LIMIT,)
    # existing MSFT 95000 + order 9000 = 104000 gross > 100000 while position stays legal
    funded = create_portfolio(Decimal(1000000))
    gross_portfolio = apply_fill(funded, fill(symbol="MSFT", quantity="9500", price="10"))
    gross_breach = evaluate_order(
        intent(quantity="900"),
        gross_portfolio,
        {"AAPL": Decimal(10), "MSFT": Decimal(10)},
        0,
        LIMITS,
    )
    assert gross_breach.reasons == (RiskReason.GROSS_EXPOSURE_LIMIT,)


def test_sell_rules_and_risk_reducing_exception() -> None:
    portfolio = portfolio_with(fill(quantity="10", price="10"))
    oversell = evaluate_order(
        intent(side="SELL", quantity="11"), portfolio, {"AAPL": Decimal(10)}, 0, LIMITS
    )
    assert oversell.reasons == (RiskReason.SELL_EXCEEDS_POSITION,)
    missing = evaluate_order(
        intent(symbol="MSFT", side="SELL", quantity="1"),
        portfolio,
        {"AAPL": Decimal(10), "MSFT": Decimal(10)},
        0,
        LIMITS,
    )
    assert missing.reasons == (RiskReason.SELL_EXCEEDS_POSITION,)
    valid = evaluate_order(
        intent(side="SELL", quantity="5"), portfolio, {"AAPL": Decimal(10)}, 1, LIMITS
    )
    assert valid.approved
    both = evaluate_order(
        intent(side="SELL", quantity="3000"),
        portfolio_with(fill(quantity="2000", price="10")),
        {"AAPL": Decimal(10)},
        0,
        LIMITS,
    )
    assert both.reasons == (RiskReason.SELL_EXCEEDS_POSITION, RiskReason.ORDER_NOTIONAL_LIMIT)


def test_risk_reducing_sell_ignores_buy_only_limits() -> None:
    portfolio = portfolio_with(fill(quantity="9000", price="10"))
    decision = evaluate_order(
        intent(side="SELL", quantity="500"), portfolio, {"AAPL": Decimal(10)}, 0, LIMITS
    )
    assert decision.approved is True
    assert decision.reasons == ()


def test_evaluation_does_not_mutate_and_returns_decision_type() -> None:
    portfolio = portfolio_with(fill(quantity="10", price="10"))
    before = portfolio.model_dump_json()
    decision = evaluate_order(intent(quantity="1"), portfolio, {"AAPL": Decimal(11)}, 0, LIMITS)
    assert isinstance(decision, RiskDecision)
    assert portfolio.model_dump_json() == before
    assert decision.approved is True


def test_evaluation_argument_types() -> None:
    flat = create_portfolio(Decimal(1000))
    with pytest.raises(TypeError):
        evaluate_order({"symbol": "AAPL"}, flat, {"AAPL": Decimal(10)}, 0, LIMITS)
    with pytest.raises(TypeError):
        evaluate_order(intent(), "not-a-portfolio", {"AAPL": Decimal(10)}, 0, LIMITS)
    with pytest.raises(TypeError):
        evaluate_order(intent(), flat, {"AAPL": Decimal(10)}, 0, {"max_order_notional": 1})
