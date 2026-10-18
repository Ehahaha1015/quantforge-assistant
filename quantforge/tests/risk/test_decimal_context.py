from __future__ import annotations

import decimal
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantforge.domain import OrderIntent
from quantforge.portfolio import create_portfolio
from quantforge.risk import RiskLimits, RiskReason, evaluate_order

T0 = datetime(2025, 1, 2, tzinfo=UTC)


def _context_signature() -> tuple[object, ...]:
    context = decimal.getcontext()
    traps = tuple(sorted((signal.__name__, enabled) for signal, enabled in context.traps.items()))
    return (
        context.prec,
        context.rounding,
        context.Emax,
        context.Emin,
        context.clamp,
        traps,
    )


def probe(precision: int) -> tuple[bool, tuple[RiskReason, ...]]:
    saved = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = precision
        order = OrderIntent(
            strategy_id="probe",
            symbol="A",
            timestamp=T0,
            side="BUY",
            quantity=Decimal("1.23456789"),
        )
        limits = RiskLimits(
            max_order_notional=Decimal("9.99999995"),
            max_position_notional=Decimal(1000000),
            max_gross_exposure=Decimal(1000000),
        )
        decision = evaluate_order(
            order, create_portfolio(Decimal(1000000)), {"A": Decimal("8.1")}, Decimal(0), limits
        )
        return decision.approved, decision.reasons
    finally:
        decimal.getcontext().prec = saved


def test_exact_notional_comparison_ignores_ambient_precision() -> None:
    # exact product 9.999999909 <= 9.99999995 -> no ORDER_NOTIONAL_LIMIT
    for precision in (28, 5, 2, 1):
        approved, reasons = probe(precision)
        assert approved is True, f"precision {precision} changed the decision"
        assert reasons == ()
    assert Decimal("1.23456789") * Decimal("8.1") == Decimal("9.999999909")


def test_caller_context_is_restored() -> None:
    saved = decimal.getcontext().prec
    probe(5)
    assert decimal.getcontext().prec == saved


def test_exponent_alignment_needs_more_than_digit_count() -> None:
    """1E+40 + 1 needs 41 coefficient digits; the gateway must not round it away."""
    from quantforge.domain import Fill
    from quantforge.portfolio import apply_fill

    big = Decimal("1E+40")
    exact_sum = Decimal(10000000000000000000000000000000000000001)
    for precision in (28, 5, 2):
        saved = decimal.getcontext().prec
        try:
            decimal.getcontext().prec = precision
            portfolio = apply_fill(
                create_portfolio(Decimal("1E+50")),
                Fill(
                    order_id="seed",
                    symbol="A",
                    timestamp=T0,
                    side="BUY",
                    quantity=big,
                    price=Decimal(1),
                    fee=Decimal(0),
                ),
            )
            limits = RiskLimits(
                max_order_notional=Decimal("1E+50"),
                max_position_notional=Decimal("1E+40"),
                max_gross_exposure=Decimal("1E+60"),
            )
            decision = evaluate_order(
                OrderIntent(
                    strategy_id="align",
                    symbol="A",
                    timestamp=T0,
                    side="BUY",
                    quantity=Decimal(1),
                ),
                portfolio,
                {"A": Decimal(1)},
                Decimal(0),
                limits,
            )
            assert decision.reasons == (RiskReason.POSITION_NOTIONAL_LIMIT,), (
                f"precision {precision} rounded the aligned sum {exact_sum}"
            )
        finally:
            decimal.getcontext().prec = saved


def _order(quantity: Decimal):
    return OrderIntent(
        strategy_id="product-boundary",
        symbol="A",
        timestamp=T0,
        side="BUY",
        quantity=quantity,
    )


def _decision(quantity: Decimal, price: Decimal, limit: Decimal, precision: int):
    saved = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = precision
        return evaluate_order(
            _order(quantity),
            create_portfolio(Decimal(1000000)),
            {"A": price},
            Decimal(0),
            RiskLimits(
                max_order_notional=limit,
                max_position_notional=Decimal(1000000),
                max_gross_exposure=Decimal(1000000),
            ),
        )
    finally:
        decimal.getcontext().prec = saved


@pytest.mark.parametrize("precision", [28, 5, 2, 1])
def test_product_boundary_is_exact_for_narrow_limit_literal(precision: int) -> None:
    """(1 + 1E-20)^2 = 1 + 2E-20 + 1E-40 strictly exceeds 1 + 2E-20."""
    quantity = price = Decimal("1.00000000000000000001")
    narrow = Decimal("1.00000000000000000002")
    decision = _decision(quantity, price, narrow, precision)
    assert decision.reasons == (RiskReason.ORDER_NOTIONAL_LIMIT,), f"precision {precision}"
    exact_product = Decimal("1.0000000000000000000200000000000000000001")
    assert _decision(quantity, price, exact_product, precision).approved is True


@pytest.mark.parametrize("precision", [28, 5, 2, 1])
def test_product_boundary_is_exact_with_tiny_epsilon(precision: int) -> None:
    """(1 + 1E-36)^2 = 1 + 2E-36 + 1E-72 strictly exceeds 1 + 2E-36."""
    quantity = price = Decimal("1.000000000000000000000000000000000001")
    narrow = Decimal("1.000000000000000000000000000000000002")
    decision = _decision(quantity, price, narrow, precision)
    assert decision.reasons == (RiskReason.ORDER_NOTIONAL_LIMIT,), f"precision {precision}"
    exact_product = Decimal(
        "1.000000000000000000000000000000000002"
        "000000000000000000000000000000000001"
    )
    assert _decision(quantity, price, exact_product, precision).approved is True


def test_product_then_sum_composition_is_exact() -> None:
    """1E+200 + 2E-200 must still exceed a 1E+200 gross limit."""
    from quantforge.domain import Position
    from quantforge.portfolio import PortfolioState
    from quantforge.risk import evaluate_order
    from quantforge.risk.evaluate import _exact_add

    state = PortfolioState(
        initial_cash=Decimal(1),
        cash=Decimal(1),
        positions=(
            Position(symbol="A", quantity=Decimal("1E+100"), average_price=Decimal(1)),
            Position(symbol="B", quantity=Decimal("1E-100"), average_price=Decimal(1)),
        ),
    )
    order = OrderIntent(
        strategy_id="composition",
        symbol="C",
        timestamp=T0,
        side="BUY",
        quantity=Decimal("1E-100"),
    )
    limits = RiskLimits(
        max_order_notional=Decimal(1),
        max_position_notional=Decimal(1),
        max_gross_exposure=Decimal("1E+200"),
    )
    prices = {"A": Decimal("1E+100"), "B": Decimal("1E-100"), "C": Decimal("1E-100")}
    for precision in (None, 28, 5, 2, 1):
        saved = decimal.getcontext().prec
        try:
            if precision is not None:
                decimal.getcontext().prec = precision
            decision = evaluate_order(order, state, prices, Decimal(0), limits)
            assert decision.reasons == (RiskReason.GROSS_EXPOSURE_LIMIT,), (
                f"precision {precision} lost the low-order exposure"
            )
        finally:
            decimal.getcontext().prec = saved
    exact = _exact_add(Decimal("1E+200"), Decimal("1E-200"))
    assert exact != Decimal("1E+200")
    assert exact - Decimal("1E+200") == Decimal("1E-200")


def test_high_precision_operands_are_evaluated_exactly() -> None:
    saved = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 4
        quantity = Decimal("1.000000000000000000000000000000000001")
        price = Decimal("2.000000000000000000000000000000000001")
        # (1+1E-36)(2+1E-36) = 2 + 3E-36 + 1E-72, written as an exact literal so the
        # expected limit never depends on the ambient precision used to build it.
        exact = Decimal(
            "2.000000000000000000000000000000000003"
            "000000000000000000000000000000000001"
        )
        limits = RiskLimits(
            max_order_notional=exact,
            max_position_notional=Decimal(1000000),
            max_gross_exposure=Decimal(1000000),
        )
        decision = evaluate_order(
            OrderIntent(
                strategy_id="wide",
                symbol="A",
                timestamp=T0,
                side="BUY",
                quantity=quantity,
            ),
            create_portfolio(Decimal(1000000)),
            {"A": price},
            Decimal(0),
            limits,
        )
        assert decision.approved is True
        assert decision.reasons == ()
        # A limit of exactly 2 is breachable by 2 + 3E-36 + 1E-72 regardless of context.
        strict = RiskLimits(
            max_order_notional=Decimal(2),
            max_position_notional=Decimal(1000000),
            max_gross_exposure=Decimal(1000000),
        )
        rejected = evaluate_order(
            OrderIntent(
                strategy_id="wide",
                symbol="A",
                timestamp=T0,
                side="BUY",
                quantity=quantity,
            ),
            create_portfolio(Decimal(1000000)),
            {"A": price},
            Decimal(0),
            strict,
        )
        assert rejected.reasons == (RiskReason.ORDER_NOTIONAL_LIMIT,)
    finally:
        decimal.getcontext().prec = saved


@pytest.mark.parametrize(
    ("quantity_text", "price_text", "limit_text", "exact_text"),
    [
        ("1E-100", "1E-100", "1E-201", "1E-200"),
        ("1E-600000", "1E-600000", "1E-1500000", "1E-1200000"),
    ],
    ids=("compact-a001", "reviewer-extreme"),
)
def test_order_notional_isolated_from_ambient_emin(
    quantity_text: str,
    price_text: str,
    limit_text: str,
    exact_text: str,
) -> None:
    quantity = Decimal(quantity_text)
    price = Decimal(price_text)
    limit = Decimal(limit_text)
    exact_order_notional = Decimal(exact_text)
    order = OrderIntent(
        strategy_id="a001-emin",
        symbol="A",
        timestamp=T0,
        side="BUY",
        quantity=quantity,
    )
    portfolio = create_portfolio(Decimal("1E+100"))
    limits = RiskLimits(
        max_order_notional=limit,
        max_position_notional=Decimal("1E+100"),
        max_gross_exposure=Decimal("1E+100"),
    )
    assert exact_order_notional > limit

    normal = decimal.Context()
    hostile = decimal.Context(
        prec=5,
        rounding=decimal.ROUND_DOWN,
        Emin=-5,
        Emax=17,
        clamp=1,
    )
    hostile.traps[decimal.Inexact] = True
    hostile.traps[decimal.Rounded] = True
    hostile.traps[decimal.Subnormal] = True
    hostile.traps[decimal.Underflow] = True

    decisions = []
    original = decimal.getcontext().copy()
    try:
        for caller_context in (normal, hostile):
            decimal.setcontext(caller_context.copy())
            before = _context_signature()
            decision = evaluate_order(
                order,
                portfolio,
                {"A": price},
                Decimal(0),
                limits,
            )
            after = _context_signature()
            assert after == before
            decisions.append(decision)
    finally:
        decimal.setcontext(original)

    assert [(decision.approved, decision.reasons) for decision in decisions] == [
        (False, (RiskReason.ORDER_NOTIONAL_LIMIT,)),
        (False, (RiskReason.ORDER_NOTIONAL_LIMIT,)),
    ]


def test_order_notional_isolated_from_ambient_emax() -> None:
    quantity = Decimal("1E+100")
    price = Decimal("1E+100")
    limit = Decimal("1E+199")
    exact_order_notional = Decimal("1E+200")
    order = OrderIntent(
        strategy_id="a001-emax",
        symbol="A",
        timestamp=T0,
        side="BUY",
        quantity=quantity,
    )
    portfolio = create_portfolio(Decimal("1E+300"))
    limits = RiskLimits(
        max_order_notional=limit,
        max_position_notional=Decimal("1E+300"),
        max_gross_exposure=Decimal("1E+300"),
    )
    assert exact_order_notional > limit

    normal = decimal.Context()
    hostile = decimal.Context(
        prec=3,
        rounding=decimal.ROUND_UP,
        Emin=-17,
        Emax=5,
        clamp=1,
    )

    decisions = []
    original = decimal.getcontext().copy()
    try:
        for caller_context in (normal, hostile):
            decimal.setcontext(caller_context.copy())
            before = _context_signature()
            decision = evaluate_order(
                order,
                portfolio,
                {"A": price},
                Decimal(0),
                limits,
            )
            after = _context_signature()
            assert after == before
            decisions.append(decision)
    finally:
        decimal.setcontext(original)

    assert [(decision.approved, decision.reasons) for decision in decisions] == [
        (False, (RiskReason.ORDER_NOTIONAL_LIMIT,)),
        (False, (RiskReason.ORDER_NOTIONAL_LIMIT,)),
    ]
