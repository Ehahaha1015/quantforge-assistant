from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from quantforge.domain import OrderIntent
from quantforge.live import (
    HumanLiveAuthorization,
    LiveAuthorizationError,
    LiveExecutionPermission,
    authorize_live_order,
    create_live_authorization,
    live_order_fingerprint,
)
from quantforge.portfolio import create_portfolio
from quantforge.risk import RiskDecision, RiskLimits, RiskReason, evaluate_order

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
AUTH_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:31:00+00:00")
OPERATOR = "operator@example.com"
BUY_FINGERPRINT = "live1-d7a22c60d95e61f1103062d3d4189a21"
SELL_FINGERPRINT = "live1-a2c9c452afe8eed2988dc9cb77908f4a"
PAPER_BUY_IDENTIFIER = "paper1-d7a22c60d95e61f1103062d3d4189a21"

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


def approved_decision() -> RiskDecision:
    decision = evaluate_order(
        buy_order(),
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        LIMITS,
    )
    assert decision.approved is True
    return decision


def authorization_for(order: OrderIntent) -> HumanLiveAuthorization:
    return create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)


def test_rejected_decision_reports_canonical_reason_codes() -> None:
    decision = RiskDecision(
        approved=False,
        reasons=(RiskReason.INSUFFICIENT_CASH, RiskReason.ORDER_NOTIONAL_LIMIT),
    )
    with pytest.raises(LiveAuthorizationError) as excinfo:
        authorize_live_order(buy_order(), decision, authorization_for(buy_order()))
    message = str(excinfo.value)
    assert "ORDER_NOTIONAL_LIMIT" in message
    assert "INSUFFICIENT_CASH" in message
    assert message.index("ORDER_NOTIONAL_LIMIT") < message.index("INSUFFICIENT_CASH")


def test_every_rejected_reason_appears_in_canonical_order() -> None:
    decision = RiskDecision(
        approved=False,
        reasons=(
            RiskReason.GROSS_EXPOSURE_LIMIT,
            RiskReason.SELL_EXCEEDS_POSITION,
            RiskReason.POSITION_NOTIONAL_LIMIT,
            RiskReason.ORDER_NOTIONAL_LIMIT,
        ),
    )
    with pytest.raises(LiveAuthorizationError) as excinfo:
        authorize_live_order(buy_order(), decision, authorization_for(buy_order()))
    message = str(excinfo.value)
    positions = [
        message.index(reason.value) for reason in RiskReason if reason.value in message
    ]
    assert positions == sorted(positions)
    for reason in decision.reasons:
        assert reason.value in message


@pytest.mark.parametrize("authorization", [None, True, False, {}, "LIVE", 1, object()])
def test_missing_or_non_authorization_is_a_type_error(authorization: object) -> None:
    with pytest.raises(TypeError):
        authorize_live_order(buy_order(), approved_decision(), authorization)


def test_approved_decision_alone_cannot_yield_a_permission() -> None:
    import inspect

    signature = inspect.signature(authorize_live_order)
    assert signature.parameters["authorization"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        authorize_live_order(buy_order(), approved_decision())


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
def test_authorization_for_order_a_fails_closed_for_order_b(
    field: str, value: object
) -> None:
    authorization = authorization_for(buy_order())
    with pytest.raises(LiveAuthorizationError):
        authorize_live_order(buy_order(**{field: value}), approved_decision(), authorization)


def test_paper_identifier_cannot_be_a_live_authorization() -> None:
    with pytest.raises(LiveAuthorizationError):
        HumanLiveAuthorization(
            mode="LIVE",
            order_fingerprint=PAPER_BUY_IDENTIFIER,
            authorized_by=OPERATOR,
            authorized_at=AUTH_TIMESTAMP,
        )


@pytest.mark.parametrize("mode", ["PAPER", "paper", "SIM", "live", ""])
def test_non_live_mode_is_rejected(mode: str) -> None:
    with pytest.raises(LiveAuthorizationError):
        HumanLiveAuthorization(
            mode=mode,
            order_fingerprint=BUY_FINGERPRINT,
            authorized_by=OPERATOR,
            authorized_at=AUTH_TIMESTAMP,
        )


@pytest.mark.parametrize(
    "fingerprint",
    [
        BUY_FINGERPRINT.upper(),
        BUY_FINGERPRINT.removeprefix("live1-"),
        "live1-" + "a" * 31,
        "live1-" + "a" * 33,
        "live1-" + "z" * 32,
        "live2-" + "a" * 32,
        "",
    ],
)
def test_non_canonical_fingerprints_are_rejected(fingerprint: str) -> None:
    with pytest.raises(LiveAuthorizationError):
        HumanLiveAuthorization(
            mode="LIVE",
            order_fingerprint=fingerprint,
            authorized_by=OPERATOR,
            authorized_at=AUTH_TIMESTAMP,
        )


@pytest.mark.parametrize("operator", ["", "   ", "\t"])
def test_blank_operator_is_rejected(operator: str) -> None:
    with pytest.raises(LiveAuthorizationError):
        HumanLiveAuthorization(
            mode="LIVE",
            order_fingerprint=BUY_FINGERPRINT,
            authorized_by=operator,
            authorized_at=AUTH_TIMESTAMP,
        )


def test_naive_authorization_instant_is_rejected() -> None:
    naive = datetime(2026, 1, 2, 14, 31)  # noqa: DTZ001 - the naive value is the case
    assert naive.tzinfo is None
    with pytest.raises(LiveAuthorizationError):
        create_live_authorization(buy_order(), OPERATOR, naive)


def test_non_datetime_authorization_instant_is_rejected() -> None:
    with pytest.raises(LiveAuthorizationError):
        create_live_authorization(buy_order(), OPERATOR, "2026-01-02T14:31:00+00:00")


def test_predating_authorization_is_rejected() -> None:
    earlier = datetime.fromisoformat("2026-01-02T14:29:00+00:00")
    with pytest.raises(LiveAuthorizationError):
        create_live_authorization(buy_order(), OPERATOR, earlier)


def test_equal_timestamp_authorization_is_accepted() -> None:
    authorization = create_live_authorization(buy_order(), OPERATOR, ORDER_TIMESTAMP)
    assert authorization.authorized_at == ORDER_TIMESTAMP
    permission = authorize_live_order(
        buy_order(), approved_decision(), authorization
    )
    assert permission.order_fingerprint == BUY_FINGERPRINT


@pytest.mark.parametrize("order", ["AAPL", None, {"symbol": "AAPL"}, 5])
def test_fingerprint_requires_a_real_order(order: object) -> None:
    with pytest.raises(TypeError):
        live_order_fingerprint(order)
    with pytest.raises(TypeError):
        create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)


@pytest.mark.parametrize("decision", [True, False, None, {}, "approved"])
def test_decision_argument_requires_a_real_decision(decision: object) -> None:
    with pytest.raises(TypeError):
        authorize_live_order(buy_order(), decision, authorization_for(buy_order()))


def test_direct_inconsistent_artifact_construction_fails_closed() -> None:
    order = buy_order()
    decision = approved_decision()
    authorization = authorization_for(order)

    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission(
            order=order,
            order_fingerprint=SELL_FINGERPRINT,
            risk_decision=decision,
            authorization=authorization,
            mode="LIVE",
        )
    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission(
            order=order,
            order_fingerprint=BUY_FINGERPRINT,
            risk_decision=RiskDecision(
                approved=False, reasons=(RiskReason.INSUFFICIENT_CASH,)
            ),
            authorization=authorization,
            mode="LIVE",
        )
    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission(
            order=order,
            order_fingerprint=BUY_FINGERPRINT,
            risk_decision=decision,
            authorization=authorization,
            mode="PAPER",
        )
    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission(
            order=order,
            order_fingerprint=BUY_FINGERPRINT,
            risk_decision=decision,
            authorization=authorization,
            mode="LIVE",
            extra="field",
        )
    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission(
            order={"symbol": "AAPL"},
            order_fingerprint=BUY_FINGERPRINT,
            risk_decision=decision,
            authorization=authorization,
            mode="LIVE",
        )
    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission(
            order=order,
            order_fingerprint=BUY_FINGERPRINT,
            risk_decision=decision,
            authorization=authorization_for(
                buy_order(quantity=Decimal(6))
            ),
            mode="LIVE",
        )


def test_failures_leave_inputs_untouched() -> None:
    order = buy_order()
    decision = RiskDecision(approved=False, reasons=(RiskReason.SELL_EXCEEDS_POSITION,))
    before = order.model_dump()
    with pytest.raises(LiveAuthorizationError):
        authorize_live_order(order, decision, authorization_for(buy_order(symbol="MSFT")))
    assert order.model_dump() == before
