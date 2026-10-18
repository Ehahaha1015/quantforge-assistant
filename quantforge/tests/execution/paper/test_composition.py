from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from quantforge.domain import Fill, OrderIntent
from quantforge.execution.paper import execute_paper_order
from quantforge.portfolio import apply_fill, create_portfolio
from quantforge.risk import RiskDecision, RiskLimits, evaluate_order

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
EXECUTION_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:31:00+00:00")

PAIR_ORDER_ID = "paper1-50c848b62cc2fc2bf167d80cf7f1a91c"

LIMITS = RiskLimits(
    max_order_notional=Decimal(1000),
    max_position_notional=Decimal(1000),
    max_gross_exposure=Decimal(1000),
)


def pair_order() -> OrderIntent:
    return OrderIntent(
        strategy_id="ma",
        symbol="AAPL",
        timestamp=ORDER_TIMESTAMP,
        side="BUY",
        quantity=Decimal(2),
    )


def test_risk_to_paper_composition_uses_public_apis_only() -> None:
    order = pair_order()
    decision = evaluate_order(
        order,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        LIMITS,
    )
    assert decision.approved is True
    assert decision.reasons == ()

    fill = execute_paper_order(
        order, decision, Decimal("100.50"), EXECUTION_TIMESTAMP, Decimal("0.50")
    )
    assert isinstance(fill, Fill)
    assert fill.order_id == PAIR_ORDER_ID
    assert fill.symbol == "AAPL"
    assert fill.side == "BUY"
    assert fill.quantity == Decimal(2)
    assert fill.timestamp == EXECUTION_TIMESTAMP
    assert fill.price == Decimal("100.50")
    assert fill.fee == Decimal("0.50")


def test_paper_to_portfolio_composition_uses_the_frozen_accounting() -> None:
    state = create_portfolio(1000)
    fill = execute_paper_order(
        pair_order(), RiskDecision(approved=True), Decimal(100), EXECUTION_TIMESTAMP,
        Decimal(1),
    )

    updated = apply_fill(state, fill)

    assert updated.initial_cash == Decimal(1000)
    assert updated.cash == Decimal(799)
    assert updated.cumulative_fees == Decimal(1)
    assert updated.last_fill_timestamp == EXECUTION_TIMESTAMP
    assert len(updated.positions) == 1
    position = updated.positions[0]
    assert position.symbol == "AAPL"
    assert position.quantity == Decimal(2)
    assert position.average_price == Decimal(100)


def test_execution_price_may_diverge_from_the_risk_price() -> None:
    order = pair_order()
    decision = evaluate_order(
        order,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        LIMITS,
    )
    assert decision.approved is True

    # 2 * 600 = 1200 would breach max_order_notional, yet paper execution
    # records the execution fact and never re-runs risk.
    fill = execute_paper_order(
        order, decision, Decimal(600), EXECUTION_TIMESTAMP, Decimal(0)
    )
    assert fill.price == Decimal(600)
    assert fill.order_id == PAIR_ORDER_ID


def test_execution_fee_may_diverge_from_the_estimated_fee() -> None:
    order = pair_order()
    decision = evaluate_order(
        order,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        LIMITS,
    )
    assert decision.approved is True

    fill = execute_paper_order(
        order, decision, Decimal(100), EXECUTION_TIMESTAMP, Decimal("25.75")
    )
    assert fill.fee == Decimal("25.75")
    assert fill.order_id == PAIR_ORDER_ID


def test_authorization_binding_limitation_is_explicit() -> None:
    # Contract section 6: the frozen RiskDecision carries no order identity, so
    # paper execution cannot prove which order produced it.  An approved
    # decision built directly - never passed through evaluate_order - is
    # therefore accepted by design.  This test pins that documented boundary
    # so it stays a deliberate decision rather than an unnoticed gap.
    decision = RiskDecision(approved=True)
    assert decision.reasons == ()
    assert decision.detail == ""

    fill = execute_paper_order(
        pair_order(), decision, Decimal(100), EXECUTION_TIMESTAMP, Decimal(1)
    )
    assert fill.order_id == PAIR_ORDER_ID
