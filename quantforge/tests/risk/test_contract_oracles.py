from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantforge.domain import Fill, OrderIntent
from quantforge.portfolio import apply_fill, create_portfolio
from quantforge.risk import RiskLimits, RiskReason, evaluate_order

T0 = datetime(2025, 1, 2, tzinfo=UTC)
BIG = RiskLimits(
    max_order_notional=Decimal(1000000),
    max_position_notional=Decimal(1000000),
    max_gross_exposure=Decimal(1000000),
)


def buy(symbol="A", quantity="5", price="100", minutes=0) -> OrderIntent:
    return OrderIntent(
        strategy_id="oracle",
        symbol=symbol,
        timestamp=T0 + timedelta(minutes=minutes),
        side="BUY",
        quantity=Decimal(quantity),
    )


def sell(symbol="A", quantity="5", minutes=1) -> OrderIntent:
    return OrderIntent(
        strategy_id="oracle",
        symbol=symbol,
        timestamp=T0 + timedelta(minutes=minutes),
        side="SELL",
        quantity=Decimal(quantity),
    )


def bought(symbol: str, quantity: str, price: str, cash: str) -> object:
    state = create_portfolio(Decimal(cash))
    return apply_fill(
        state,
        Fill(
            order_id=f"seed-{symbol}",
            symbol=symbol,
            timestamp=T0,
            side="BUY",
            quantity=Decimal(quantity),
            price=Decimal(price),
            fee=Decimal(0),
        ),
    )


def test_exact_contract_cash_oracle() -> None:
    price = {"A": Decimal(100)}
    # notional 5 * 100 = 500, fee 1
    assert evaluate_order(buy(), create_portfolio(Decimal(1000)), price, Decimal(1), BIG).approved
    low = evaluate_order(buy(), create_portfolio(Decimal(500)), price, Decimal(1), BIG)
    assert low.approved is False
    assert low.reasons == (RiskReason.INSUFFICIENT_CASH,)
    boundary = evaluate_order(buy(), create_portfolio(Decimal(501)), price, Decimal(1), BIG)
    assert boundary.approved is True
    assert boundary.reasons == ()


def test_exact_contract_position_notional_oracle() -> None:
    # existing 10 @ 100, BUY 2 @ 100 -> projected quantity 12, notional 1200
    portfolio = bought("A", "10", "100", "100000")
    passing = RiskLimits(
        max_order_notional=Decimal(1000),
        max_position_notional=Decimal(1200),
        max_gross_exposure=Decimal(1000000),
    )
    failing = RiskLimits(
        max_order_notional=Decimal(1000),
        max_position_notional=Decimal(1199),
        max_gross_exposure=Decimal(1000000),
    )
    assert evaluate_order(buy(quantity="2"), portfolio, {"A": Decimal(100)}, 0, passing).approved
    rejected = evaluate_order(buy(quantity="2"), portfolio, {"A": Decimal(100)}, 0, failing)
    assert rejected.reasons == (RiskReason.POSITION_NOTIONAL_LIMIT,)


def test_exact_contract_gross_exposure_oracle() -> None:
    # A 10 * 100 = 1000, B 5 * 200 = 1000 -> current gross 2000; BUY A 2 * 100 = 200
    portfolio = bought("B", "5", "200", "100000")
    portfolio = apply_fill(
        portfolio,
        Fill(
            order_id="seed-A",
            symbol="A",
            timestamp=T0,
            side="BUY",
            quantity=Decimal(10),
            price=Decimal(100),
            fee=Decimal(0),
        ),
    )
    prices = {"A": Decimal(100), "B": Decimal(200)}
    passing = RiskLimits(
        max_order_notional=Decimal(1000),
        max_position_notional=Decimal(1000000),
        max_gross_exposure=Decimal(2200),
    )
    failing = RiskLimits(
        max_order_notional=Decimal(1000),
        max_position_notional=Decimal(1000000),
        max_gross_exposure=Decimal(2199),
    )
    assert evaluate_order(buy(quantity="2"), portfolio, prices, 0, passing).approved
    rejected = evaluate_order(buy(quantity="2"), portfolio, prices, 0, failing)
    assert rejected.reasons == (RiskReason.GROSS_EXPOSURE_LIMIT,)


def test_exact_buy_multi_reason_order() -> None:
    tight = RiskLimits(
        max_order_notional=Decimal(100),
        max_position_notional=Decimal(100),
        max_gross_exposure=Decimal(100),
    )
    decision = evaluate_order(buy(), create_portfolio(Decimal(0)), {"A": Decimal(100)}, 1, tight)
    assert decision.approved is False
    assert decision.reasons == (
        RiskReason.ORDER_NOTIONAL_LIMIT,
        RiskReason.INSUFFICIENT_CASH,
        RiskReason.POSITION_NOTIONAL_LIMIT,
        RiskReason.GROSS_EXPOSURE_LIMIT,
    )


def test_exact_sell_multi_reason_order() -> None:
    portfolio = bought("A", "2", "100", "100000")
    tight = RiskLimits(
        max_order_notional=Decimal(100),
        max_position_notional=Decimal(1000000),
        max_gross_exposure=Decimal(1000000),
    )
    decision = evaluate_order(sell(quantity="5"), portfolio, {"A": Decimal(100)}, 0, tight)
    assert decision.reasons == (
        RiskReason.SELL_EXCEEDS_POSITION,
        RiskReason.ORDER_NOTIONAL_LIMIT,
    )


def test_structurally_valid_sell_ignores_existing_breaches() -> None:
    # cash < 0 (100 - 20*100 = -1900), position 2000 > limits, gross 2000 > limits
    portfolio = bought("A", "20", "100", "100")
    assert portfolio.cash == Decimal(-1900)
    tight = RiskLimits(
        max_order_notional=Decimal(10000),
        max_position_notional=Decimal(1000),
        max_gross_exposure=Decimal(1000),
    )
    decision = evaluate_order(sell(quantity="5"), portfolio, {"A": Decimal(100)}, 0, tight)
    assert decision.approved is True
    assert decision.reasons == ()
    for forbidden in (
        RiskReason.INSUFFICIENT_CASH,
        RiskReason.POSITION_NOTIONAL_LIMIT,
        RiskReason.GROSS_EXPOSURE_LIMIT,
    ):
        assert forbidden not in decision.reasons
