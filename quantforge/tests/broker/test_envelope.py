from __future__ import annotations

import decimal
import hashlib
import json
from datetime import datetime
from decimal import Decimal

import pytest

from quantforge.broker import (
    BrokerBoundaryError,
    BrokerSubmissionEnvelope,
    broker_submission_id,
    create_broker_submission_envelope,
)
from quantforge.domain import Fill, OrderIntent
from quantforge.live import (
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

BUY_SUBMISSION_ID = "broker1-4c78154fcce6c1791cb74257a777de95"
SELL_SUBMISSION_ID = "broker1-0e84b342e472496e8b9daa69a84ec87b"
BUY_LIVE_FINGERPRINT = "live1-d7a22c60d95e61f1103062d3d4189a21"
PAPER_BUY_IDENTIFIER = "paper1-d7a22c60d95e61f1103062d3d4189a21"

# Hand-written canonical bytes for the BUY fixture, independent of production.
BUY_SUBMISSION_BYTES = (
    b'{"order_fingerprint":"live1-d7a22c60d95e61f1103062d3d4189a21"}'
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


def buy_permission() -> LiveExecutionPermission:
    order = buy_order()
    decision = evaluate_order(
        order,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        LIMITS,
    )
    assert decision.approved is True
    return authorize_live_order(
        order, decision, create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    )


def sell_permission() -> LiveExecutionPermission:
    order = sell_order()
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
        order, seeded, {"AAPL": Decimal(100)}, Decimal(1), LIMITS
    )
    assert decision.approved is True
    return authorize_live_order(
        order, decision, create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    )


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


def test_valid_envelope_oracle_buy() -> None:
    envelope = create_broker_submission_envelope(buy_order(), buy_permission())
    assert isinstance(envelope, BrokerSubmissionEnvelope)
    assert envelope.order == buy_order()
    assert envelope.submission_id == BUY_SUBMISSION_ID
    assert envelope.permission.order_fingerprint == BUY_LIVE_FINGERPRINT
    assert envelope.permission.order_fingerprint == live_order_fingerprint(
        envelope.order
    )
    assert envelope.permission.mode == "LIVE"


def test_valid_envelope_oracle_sell() -> None:
    envelope = create_broker_submission_envelope(sell_order(), sell_permission())
    assert envelope.submission_id == SELL_SUBMISSION_ID
    assert envelope.order.side == "SELL"


def test_pinned_identity_matches_independent_raw_bytes() -> None:
    digest = hashlib.sha256(BUY_SUBMISSION_BYTES).hexdigest()[:32]
    assert "broker1-" + digest == BUY_SUBMISSION_ID
    assert broker_submission_id(buy_order()) == BUY_SUBMISSION_ID
    assert broker_submission_id(sell_order()) == SELL_SUBMISSION_ID


@pytest.mark.parametrize(
    "permission",
    [
        None,
        True,
        False,
        {},
        {"order_fingerprint": BUY_LIVE_FINGERPRINT},
        "LIVE",
        1,
        object(),
    ],
)
def test_envelope_requires_a_real_permission(permission: object) -> None:
    with pytest.raises(TypeError):
        create_broker_submission_envelope(buy_order(), permission)


def test_permission_chain_cannot_be_substituted() -> None:
    order = buy_order()
    authorization = create_live_authorization(order, OPERATOR, AUTH_TIMESTAMP)
    decision = RiskDecision(approved=True)
    for substitute in (order, decision, authorization):
        with pytest.raises(TypeError):
            create_broker_submission_envelope(order, substitute)


@pytest.mark.parametrize("order", ["AAPL", None, {"symbol": "AAPL"}, 5])
def test_envelope_requires_a_real_order(order: object) -> None:
    with pytest.raises(TypeError):
        create_broker_submission_envelope(order, buy_permission())


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
def test_order_mismatch_fails_closed(field: str, value: object) -> None:
    permission = buy_permission()
    with pytest.raises(BrokerBoundaryError):
        create_broker_submission_envelope(buy_order(**{field: value}), permission)


def test_direct_construction_fails_closed() -> None:
    order = buy_order()
    permission = buy_permission()
    other_permission = sell_permission()

    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope(
            order=order, permission=other_permission, submission_id=BUY_SUBMISSION_ID
        )
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope(
            order=order, permission=permission, submission_id=SELL_SUBMISSION_ID
        )
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope(
            order=order,
            permission=permission,
            submission_id=PAPER_BUY_IDENTIFIER,
        )
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope(
            order=order,
            permission=permission,
            submission_id=BUY_LIVE_FINGERPRINT,
        )
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope(
            order=order,
            permission=permission,
            submission_id=BUY_SUBMISSION_ID,
            note="extra",
        )
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope(
            order={"symbol": "AAPL"},
            permission=permission,
            submission_id=BUY_SUBMISSION_ID,
        )
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope(
            order=order,
            permission=order_fingerprint_only(),
            submission_id=BUY_SUBMISSION_ID,
        )


def order_fingerprint_only() -> dict[str, str]:
    return {"order_fingerprint": BUY_LIVE_FINGERPRINT}


def test_serialization_round_trip_is_deterministic() -> None:
    envelope = create_broker_submission_envelope(buy_order(), buy_permission())
    first = envelope.model_dump_json()
    assert first == envelope.model_dump_json()
    rebuilt = BrokerSubmissionEnvelope.from_json(first)
    assert rebuilt == envelope
    assert rebuilt.model_dump_json() == first


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["order"].__setitem__("symbol", "MSFT"),
        lambda data: data["order"].__setitem__("quantity", "6"),
        lambda data: data.__setitem__("submission_id", SELL_SUBMISSION_ID),
        lambda data: data.__setitem__(
            "submission_id", "live1-d7a22c60d95e61f1103062d3d4189a21"
        ),
        lambda data: data["permission"].__setitem__(
            "order_fingerprint", "live1-a2c9c452afe8eed2988dc9cb77908f4a"
        ),
        lambda data: data["permission"].__setitem__("mode", "PAPER"),
        lambda data: data["permission"].__delitem__("authorization"),
        lambda data: data.__setitem__("note", "extra"),
    ],
)
def test_tampered_envelope_payloads_fail_closed(mutate) -> None:
    payload = json.loads(
        create_broker_submission_envelope(
            buy_order(), buy_permission()
        ).model_dump_json()
    )
    mutate(payload)
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope.from_json(json.dumps(payload))


def test_malformed_envelope_payloads_fail_closed() -> None:
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope.from_json("not json")
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope.from_json("[]")
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionEnvelope.from_json("{}")


def test_inputs_are_not_mutated() -> None:
    order = buy_order()
    permission = buy_permission()
    order_before = order.model_dump()
    permission_before = permission.model_dump()

    create_broker_submission_envelope(order, permission)

    assert order.model_dump() == order_before
    assert permission.model_dump() == permission_before


def test_hostile_ambient_decimal_context_does_not_change_results() -> None:
    expected_identity = broker_submission_id(buy_order())
    expected_envelope = create_broker_submission_envelope(buy_order(), buy_permission())
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
        under_hostile_identity = broker_submission_id(buy_order())
        under_hostile_envelope = create_broker_submission_envelope(
            buy_order(), buy_permission()
        )

    assert under_hostile_identity == expected_identity == BUY_SUBMISSION_ID
    assert under_hostile_envelope == expected_envelope
    assert context_signature(decimal.getcontext()) == before
