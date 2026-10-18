from __future__ import annotations

import decimal
import hashlib
import inspect
import subprocess
import sys
from datetime import datetime
from decimal import Decimal

import pytest

import quantforge.live as live_package
from quantforge.domain import Fill, OrderIntent
from quantforge.live import (
    HumanLiveAuthorization,
    LiveAuthorizationError,
    LiveExecutionPermission,
    authorize_live_order,
    create_live_authorization,
    live_order_fingerprint,
)
from quantforge.portfolio import apply_fill, create_portfolio
from quantforge.risk import RiskDecision, RiskLimits, evaluate_order

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
AUTH_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:31:00+00:00")
OPERATOR = "operator@example.com"

BUY_FINGERPRINT = "live1-d7a22c60d95e61f1103062d3d4189a21"
SELL_FINGERPRINT = "live1-a2c9c452afe8eed2988dc9cb77908f4a"
PAPER_BUY_IDENTIFIER = "paper1-d7a22c60d95e61f1103062d3d4189a21"
QUANTITY_100_FINGERPRINT = "live1-64f545f7e4fa16e8b9a770c90908dd3b"

# Hand-written canonical bytes for the BUY fixture, independent of production.
BUY_CANONICAL_BYTES = (
    b'{"quantity":"5","side":"BUY","strategy_id":"ma","symbol":"AAPL",'
    b'"timestamp":"2026-01-02T14:30:00.000000+00:00"}'
)

LIMITS = RiskLimits(
    max_order_notional=Decimal(1000),
    max_position_notional=Decimal(1000),
    max_gross_exposure=Decimal(1000),
)


def buy_order(**overrides: object) -> OrderIntent:
    fields: dict[str, object] = {
        "strategy_id": "ma",
        "symbol": "AAPL",
        "timestamp": ORDER_TIMESTAMP,
        "side": "BUY",
        "quantity": Decimal(5),
    }
    fields.update(overrides)
    return OrderIntent.model_validate(fields)


def sell_order(**overrides: object) -> OrderIntent:
    fields: dict[str, object] = {
        "strategy_id": "ma",
        "symbol": "AAPL",
        "timestamp": ORDER_TIMESTAMP,
        "side": "SELL",
        "quantity": Decimal(2),
    }
    fields.update(overrides)
    return OrderIntent.model_validate(fields)


def approved_decision(order: OrderIntent) -> RiskDecision:
    decision = evaluate_order(
        order,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        LIMITS,
    )
    assert decision.approved is True
    assert decision.reasons == ()
    return decision


def decision_for_sell(order: OrderIntent) -> RiskDecision:
    """Approve a SELL against a portfolio that really holds the quantity."""
    seeded = apply_fill(
        create_portfolio(1000),
        Fill(
            order_id="seed-AAPL",
            symbol="AAPL",
            timestamp=ORDER_TIMESTAMP,
            side="BUY",
            quantity=Decimal(2),
            price=Decimal(100),
            fee=Decimal(0),
        ),
    )
    decision = evaluate_order(
        order,
        seeded,
        {"AAPL": Decimal(100)},
        Decimal(1),
        LIMITS,
    )
    assert decision.approved is True
    assert decision.reasons == ()
    return decision


def context_signature(context: decimal.Context) -> tuple[object, ...]:
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.clamp,
        tuple(
            (str(signal), trapped)
            for signal, trapped in sorted(
                context.traps.items(), key=lambda item: str(item[0])
            )
        ),
    )


def test_buy_happy_path_oracle() -> None:
    order = buy_order()
    decision = approved_decision(order)
    authorization = create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    assert isinstance(authorization, HumanLiveAuthorization)
    assert authorization.mode == "LIVE"
    assert authorization.order_fingerprint == BUY_FINGERPRINT
    assert authorization.authorized_by == OPERATOR
    assert authorization.authorized_at == AUTH_TIMESTAMP

    permission = authorize_live_order(order, decision, authorization)
    assert isinstance(permission, LiveExecutionPermission)
    assert permission.order == order
    assert permission.order_fingerprint == BUY_FINGERPRINT
    assert permission.risk_decision == decision
    assert permission.authorization == authorization
    assert permission.mode == "LIVE"


def test_sell_happy_path_oracle() -> None:
    order = sell_order()
    decision = decision_for_sell(order)
    authorization = create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    assert authorization.order_fingerprint == SELL_FINGERPRINT
    permission = authorize_live_order(order, decision, authorization)
    assert permission.order_fingerprint == SELL_FINGERPRINT
    assert permission.order.side == "SELL"


def test_pinned_fingerprints_match_independent_raw_bytes() -> None:
    digest = hashlib.sha256(BUY_CANONICAL_BYTES).hexdigest()[:32]
    assert "live1-" + digest == BUY_FINGERPRINT
    assert live_order_fingerprint(buy_order()) == BUY_FINGERPRINT
    assert live_order_fingerprint(sell_order()) == SELL_FINGERPRINT


def test_fingerprint_is_reproducible_in_a_fresh_interpreter() -> None:
    code = (
        "from datetime import datetime\n"
        "from decimal import Decimal\n"
        "from quantforge.domain import OrderIntent\n"
        "from quantforge.live import live_order_fingerprint\n"
        "order = OrderIntent(strategy_id='ma', symbol='AAPL',\n"
        "    timestamp=datetime.fromisoformat('2026-01-02T14:30:00+00:00'),\n"
        "    side='BUY', quantity=Decimal('5'))\n"
        "print(live_order_fingerprint(order))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == BUY_FINGERPRINT


def test_repeat_calls_return_equal_artifacts() -> None:
    order = buy_order()
    decision = approved_decision(order)
    first_authorization = create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    second_authorization = create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    assert first_authorization == second_authorization

    first = authorize_live_order(order, decision, first_authorization)
    second = authorize_live_order(order, decision, second_authorization)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_public_surface_is_exactly_the_contracted_surface() -> None:
    assert issubclass(LiveAuthorizationError, ValueError)
    assert set(live_package.__all__) == {
        "LiveAuthorizationError",
        "HumanLiveAuthorization",
        "LiveExecutionPermission",
        "live_order_fingerprint",
        "create_live_authorization",
        "authorize_live_order",
    }
    assert len(live_package.__all__) == 6
    assert list(inspect.signature(live_order_fingerprint).parameters) == ["order"]
    assert list(inspect.signature(create_live_authorization).parameters) == [
        "order",
        "authorized_by",
        "authorized_at",
    ]
    assert list(inspect.signature(authorize_live_order).parameters) == [
        "order",
        "risk_decision",
        "authorization",
    ]


def test_no_execution_or_management_surface_exists() -> None:
    forbidden = (
        "LiveBroker",
        "LiveTradingManager",
        "LiveExecutionManager",
        "LiveExecutionService",
        "BrokerService",
        "BrokerClient",
        "OMS",
        "EMS",
        "ApprovalRepository",
        "ApprovalDatabase",
        "ExecutionRouter",
        "MatchingEngine",
    )
    for name in forbidden:
        assert not hasattr(live_package, name)
    for name in ("submit", "execute", "place_order", "cancel_order", "replace_order"):
        assert not hasattr(LiveExecutionPermission, name)
        assert not hasattr(HumanLiveAuthorization, name)


def test_inputs_are_not_mutated() -> None:
    order = buy_order()
    decision = approved_decision(order)
    authorization = create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    order_before = order.model_dump()
    decision_before = decision.model_dump()
    authorization_before = authorization.model_dump()
    assert AUTH_TIMESTAMP == datetime.fromisoformat("2026-01-02T14:31:00+00:00")

    authorize_live_order(order, decision, authorization)

    assert order.model_dump() == order_before
    assert decision.model_dump() == decision_before
    assert authorization.model_dump() == authorization_before


def test_inputs_are_not_mutated_after_a_failure() -> None:
    order = buy_order()
    authorization = create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    order_before = order.model_dump()
    rejected = RiskDecision(approved=False, reasons=("INSUFFICIENT_CASH",))

    with pytest.raises(LiveAuthorizationError):
        authorize_live_order(order, rejected, authorization)

    assert order.model_dump() == order_before
    assert rejected.reasons == ("INSUFFICIENT_CASH",)


def test_hostile_ambient_decimal_context_does_not_change_results() -> None:
    order = buy_order()
    decision = approved_decision(order)
    expected_fingerprint = live_order_fingerprint(order)
    expected_permission = authorize_live_order(
        order,
        decision,
        create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP),
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
        under_hostile_fingerprint = live_order_fingerprint(order)
        under_hostile_permission = authorize_live_order(
            order,
            decision,
            create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP),
        )

    assert under_hostile_fingerprint == expected_fingerprint == BUY_FINGERPRINT
    assert under_hostile_permission == expected_permission
    assert context_signature(decimal.getcontext()) == before


@pytest.mark.parametrize("quantity", [5, "5", "5.0", "50E-1", Decimal("5.000")])
def test_equivalent_quantity_spellings_share_one_fingerprint(quantity: object) -> None:
    assert live_order_fingerprint(buy_order(quantity=quantity)) == BUY_FINGERPRINT


@pytest.mark.parametrize("quantity", [100, "100", "1E+2", Decimal("1E+2")])
def test_quantity_uses_the_domain_normalized_representation(quantity: object) -> None:
    order = buy_order(quantity=quantity)
    assert str(order.quantity) == "1E+2"
    assert live_order_fingerprint(order) == QUANTITY_100_FINGERPRINT


def test_equivalent_utc_instants_share_one_fingerprint() -> None:
    shifted = datetime.fromisoformat("2026-01-02T22:30:00+08:00")
    assert shifted.utcoffset() is not None
    assert live_order_fingerprint(buy_order(timestamp=shifted)) == BUY_FINGERPRINT


@pytest.mark.parametrize(
    "field, value",
    [
        ("strategy_id", "other"),
        ("symbol", "MSFT"),
        ("side", "SELL"),
        ("quantity", Decimal(6)),
        ("timestamp", datetime.fromisoformat("2026-01-02T14:30:01+00:00")),
    ],
)
def test_each_identity_field_changes_the_fingerprint(field: str, value: object) -> None:
    assert live_order_fingerprint(buy_order(**{field: value})) != BUY_FINGERPRINT


def test_live_namespace_is_separate_from_paper_identifier() -> None:
    fingerprint = live_order_fingerprint(buy_order())
    assert fingerprint != PAPER_BUY_IDENTIFIER
    assert fingerprint.removeprefix("live1-") == PAPER_BUY_IDENTIFIER.removeprefix(
        "paper1-"
    )
