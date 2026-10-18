from __future__ import annotations

import decimal
import inspect
from datetime import datetime
from decimal import Decimal

import quantforge.execution.paper as paper_package
from quantforge.domain import Fill, OrderIntent
from quantforge.execution.paper import PaperExecutionError, execute_paper_order
from quantforge.risk import RiskDecision

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
EXECUTION_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:31:00+00:00")
APPROVED = RiskDecision(approved=True)

# Literal identities pinned by tasks/PAPER-001.md sections 19 and 20.
BUY_ORDER_ID = "paper1-d7a22c60d95e61f1103062d3d4189a21"
SELL_ORDER_ID = "paper1-a2c9c452afe8eed2988dc9cb77908f4a"

FORBIDDEN_PUBLIC_NAMES = (
    "PaperBroker",
    "PaperExchange",
    "PaperManager",
    "PaperService",
    "PaperAccount",
    "PaperRepository",
    "PaperRouter",
    "MatchingEngine",
    "ExecutionEngine",
    "OrderBook",
    "Session",
    "Plugin",
)


def buy_order(quantity: Decimal | int | str = Decimal(5), **overrides: object) -> OrderIntent:
    fields: dict[str, object] = {
        "strategy_id": "ma",
        "symbol": "AAPL",
        "timestamp": ORDER_TIMESTAMP,
        "side": "BUY",
        "quantity": quantity,
    }
    fields.update(overrides)
    return OrderIntent.model_validate(fields)


def sell_order(quantity: Decimal | int | str = Decimal(2), **overrides: object) -> OrderIntent:
    fields: dict[str, object] = {
        "strategy_id": "ma",
        "symbol": "AAPL",
        "timestamp": ORDER_TIMESTAMP,
        "side": "SELL",
        "quantity": quantity,
    }
    fields.update(overrides)
    return OrderIntent.model_validate(fields)


def context_signature(context: decimal.Context) -> tuple[object, ...]:
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.clamp,
        tuple((str(signal), trapped) for signal, trapped in sorted(
            context.traps.items(), key=lambda item: str(item[0])
        )),
    )


def test_buy_literal_oracle() -> None:
    fill = execute_paper_order(
        buy_order(), APPROVED, Decimal("100.25"), EXECUTION_TIMESTAMP, Decimal("1.50")
    )
    assert isinstance(fill, Fill)
    assert fill.order_id == BUY_ORDER_ID
    assert fill.symbol == "AAPL"
    assert fill.side == "BUY"
    assert fill.quantity == Decimal(5)
    assert fill.timestamp == EXECUTION_TIMESTAMP
    assert fill.price == Decimal("100.25")
    assert fill.fee == Decimal("1.50")


def test_buy_literal_oracle_repeats_identically() -> None:
    first = execute_paper_order(
        buy_order(), APPROVED, Decimal("100.25"), EXECUTION_TIMESTAMP, Decimal("1.50")
    )
    second = execute_paper_order(
        buy_order(), APPROVED, Decimal("100.25"), EXECUTION_TIMESTAMP, Decimal("1.50")
    )
    assert first == second
    assert first.order_id == second.order_id == BUY_ORDER_ID


def test_sell_literal_oracle() -> None:
    fill = execute_paper_order(
        sell_order(), APPROVED, Decimal(110), EXECUTION_TIMESTAMP, Decimal("0.75")
    )
    assert isinstance(fill, Fill)
    assert fill.order_id == SELL_ORDER_ID
    assert fill.symbol == "AAPL"
    assert fill.side == "SELL"
    assert fill.quantity == Decimal(2)
    assert fill.timestamp == EXECUTION_TIMESTAMP
    assert fill.price == Decimal(110)
    assert fill.fee == Decimal("0.75")


def test_public_surface_is_exactly_the_contracted_surface() -> None:
    assert issubclass(PaperExecutionError, ValueError)
    assert set(paper_package.__all__) == {"PaperExecutionError", "execute_paper_order"}
    assert len(paper_package.__all__) == 2
    parameters = list(inspect.signature(execute_paper_order).parameters)
    assert parameters == [
        "order",
        "risk_decision",
        "execution_price",
        "execution_timestamp",
        "execution_fee",
    ]
    assert paper_package.PaperExecutionError is PaperExecutionError
    for name in FORBIDDEN_PUBLIC_NAMES:
        assert not hasattr(paper_package, name)


def test_execution_timestamp_may_equal_order_timestamp() -> None:
    fill = execute_paper_order(
        buy_order(), APPROVED, Decimal("100.25"), ORDER_TIMESTAMP, Decimal(0)
    )
    assert fill.timestamp == ORDER_TIMESTAMP


def test_exact_int_inputs_are_accepted() -> None:
    fill = execute_paper_order(buy_order(), APPROVED, 100, EXECUTION_TIMESTAMP, 0)
    assert fill.price == Decimal(100)
    assert fill.fee == Decimal(0)


def test_inputs_are_not_mutated() -> None:
    order = buy_order()
    decision = RiskDecision(approved=True)
    order_before = order.model_dump()
    decision_before = decision.model_dump()

    execute_paper_order(order, decision, Decimal(100), EXECUTION_TIMESTAMP, Decimal(1))

    assert order.model_dump() == order_before
    assert decision.model_dump() == decision_before
    assert order.timestamp == ORDER_TIMESTAMP
    assert order.quantity == Decimal(5)
    assert EXECUTION_TIMESTAMP == datetime.fromisoformat("2026-01-02T14:31:00+00:00")


def test_inputs_are_not_mutated_after_a_failure() -> None:
    order = buy_order()
    decision = RiskDecision(approved=True)
    order_before = order.model_dump()

    try:
        execute_paper_order(
            order, decision, Decimal(0), EXECUTION_TIMESTAMP, Decimal(1)
        )
    except PaperExecutionError:
        pass
    else:  # pragma: no cover - the call above must fail
        raise AssertionError("execution_price == 0 must be rejected")

    assert order.model_dump() == order_before
    assert decision.approved is True


def test_hostile_ambient_decimal_context_does_not_change_the_fill() -> None:
    expected = execute_paper_order(
        buy_order(), APPROVED, Decimal("100.25"), EXECUTION_TIMESTAMP, Decimal("1.50")
    )
    before = context_signature(decimal.getcontext())

    with decimal.localcontext() as hostile:
        hostile.prec = 2
        hostile.rounding = decimal.ROUND_UP
        hostile.Emin = -5
        hostile.Emax = 5
        for signal in (
            decimal.Inexact,
            decimal.Rounded,
            decimal.Underflow,
            decimal.Overflow,
        ):
            hostile.traps[signal] = True
        actual = execute_paper_order(
            buy_order(), APPROVED, Decimal("100.25"), EXECUTION_TIMESTAMP,
            Decimal("1.50"),
        )

    assert actual == expected
    assert actual.order_id == BUY_ORDER_ID
    assert context_signature(decimal.getcontext()) == before
