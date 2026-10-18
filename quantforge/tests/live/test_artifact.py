from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantforge.domain import OrderIntent
from quantforge.live import (
    HumanLiveAuthorization,
    LiveAuthorizationError,
    LiveExecutionPermission,
    authorize_live_order,
    create_live_authorization,
)
from quantforge.portfolio import create_portfolio
from quantforge.risk import RiskDecision, RiskLimits, evaluate_order

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
AUTH_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:31:00+00:00")
OPERATOR = "operator@example.com"
BUY_FINGERPRINT = "live1-d7a22c60d95e61f1103062d3d4189a21"

LIMITS = RiskLimits(
    max_order_notional=Decimal(1000),
    max_position_notional=Decimal(1000),
    max_gross_exposure=Decimal(1000),
)


def buy_order() -> OrderIntent:
    return OrderIntent(
        strategy_id="ma",
        symbol="AAPL",
        timestamp=ORDER_TIMESTAMP,
        side="BUY",
        quantity=Decimal(5),
    )


def permission() -> LiveExecutionPermission:
    order = buy_order()
    decision = evaluate_order(
        order,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        LIMITS,
    )
    assert decision.approved is True
    authorization = create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    return authorize_live_order(order, decision, authorization)


def test_permission_answers_the_audit_questions() -> None:
    artifact = permission()
    assert artifact.order == buy_order()
    assert artifact.order_fingerprint == BUY_FINGERPRINT
    assert artifact.risk_decision.approved is True
    assert artifact.risk_decision.reasons == ()
    assert artifact.authorization.authorized_by == OPERATOR
    assert artifact.authorization.authorized_at == AUTH_TIMESTAMP
    assert artifact.mode == "LIVE"
    assert artifact.authorization.mode == "LIVE"


def test_permission_and_authorization_are_frozen() -> None:
    artifact = permission()
    with pytest.raises(ValidationError):
        artifact.mode = "PAPER"
    with pytest.raises(ValidationError):
        artifact.order_fingerprint = "live1-" + "a" * 32
    with pytest.raises(ValidationError):
        artifact.authorization.authorized_by = "someone-else"


def test_permission_rejects_unknown_fields() -> None:
    artifact = permission()
    payload = json.loads(artifact.model_dump_json())
    payload["note"] = "extra"
    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission.from_json(json.dumps(payload))


def test_serialization_is_deterministic_and_round_trips() -> None:
    artifact = permission()
    first = artifact.model_dump_json()
    second = artifact.model_dump_json()
    assert first == second
    assert LiveExecutionPermission.from_json(first) == artifact
    rebuilt = LiveExecutionPermission.from_json(first)
    assert rebuilt.model_dump_json() == first


def test_authorization_round_trips() -> None:
    authorization = create_live_authorization(buy_order(), OPERATOR, AUTH_TIMESTAMP)
    payload = authorization.model_dump_json()
    assert HumanLiveAuthorization.from_json(payload) == authorization
    assert HumanLiveAuthorization.from_json(payload).model_dump_json() == payload


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["order"].__setitem__("symbol", "MSFT"),
        lambda data: data["order"].__setitem__("quantity", "6"),
        lambda data: data.__setitem__("order_fingerprint", "live1-" + "a" * 32),
        lambda data: data["authorization"].__setitem__(
            "order_fingerprint", "live1-" + "b" * 32
        ),
        lambda data: data["authorization"].__setitem__("mode", "PAPER"),
        lambda data: data["authorization"].__setitem__(
            "authorized_at", "2026-01-02T14:29:00+00:00"
        ),
        lambda data: data.__setitem__("mode", "PAPER"),
        lambda data: data.__setitem__(
            "risk_decision",
            {"approved": False, "reasons": ["INSUFFICIENT_CASH"], "detail": ""},
        ),
        lambda data: data.__delitem__("authorization"),
    ],
)
def test_tampered_payloads_fail_closed(mutate) -> None:
    payload = json.loads(permission().model_dump_json())
    mutate(payload)
    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission.from_json(json.dumps(payload))


def test_malformed_payloads_fail_closed() -> None:
    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission.from_json("not json at all")
    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission.from_json("[]")
    with pytest.raises(LiveAuthorizationError):
        HumanLiveAuthorization.from_json("{}")


def test_rejected_decision_is_never_embedded_in_a_permission() -> None:
    order = buy_order()
    authorization = create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    rejected = RiskDecision(approved=False, reasons=("INSUFFICIENT_CASH",))
    with pytest.raises(LiveAuthorizationError):
        authorize_live_order(order, rejected, authorization)
    with pytest.raises(LiveAuthorizationError):
        LiveExecutionPermission(
            order=order,
            order_fingerprint=BUY_FINGERPRINT,
            risk_decision=rejected,
            authorization=authorization,
            mode="LIVE",
        )
