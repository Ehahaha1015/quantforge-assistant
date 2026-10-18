from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quantforge.broker import (
    BrokerSubmissionEnvelope,
    create_broker_submission_envelope,
)
from quantforge.domain import OrderIntent
from quantforge.live import (
    LiveExecutionPermission,
    authorize_live_order,
    create_live_authorization,
)
from quantforge.portfolio import PortfolioState, create_portfolio
from quantforge.predispatch import (
    AuthorizationFreshnessPolicy,
    PreDispatchDecision,
    PreDispatchError,
    PreDispatchReason,
    SubmissionHistoryState,
    evaluate_pre_dispatch,
)
from quantforge.risk import RiskLimits, RiskReason, evaluate_order

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
AUTHORIZED_AT = datetime.fromisoformat("2030-01-01T12:00:00+00:00")
EVALUATION_AT = datetime.fromisoformat("2030-01-01T12:05:00+00:00")
EVALUATION_PLUS_ONE_MICROSECOND = datetime.fromisoformat(
    "2030-01-01T12:05:00.000001+00:00"
)
EVALUATION_MINUS_ONE_MICROSECOND = datetime.fromisoformat(
    "2030-01-01T11:59:59.999999+00:00"
)
OPERATOR = "operator@example.com"

SUBMISSION_ID = "broker1-4c78154fcce6c1791cb74257a777de95"
LIVE_FINGERPRINT = "live1-d7a22c60d95e61f1103062d3d4189a21"

MAX_AGE = timedelta(minutes=5)


def limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal(1000),
        max_position_notional=Decimal(1000),
        max_gross_exposure=Decimal(1000),
    )


def buy_order(quantity: Decimal = Decimal(5)) -> OrderIntent:
    return OrderIntent(
        strategy_id="ma",
        symbol="AAPL",
        timestamp=ORDER_TIMESTAMP,
        side="BUY",
        quantity=quantity,
    )


def buy_permission() -> LiveExecutionPermission:
    order = buy_order()
    decision = evaluate_order(
        order,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )
    assert decision.approved is True
    return authorize_live_order(
        order,
        decision,
        create_live_authorization(order, OPERATOR, AUTHORIZED_AT),
    )


def buy_envelope() -> BrokerSubmissionEnvelope:
    return create_broker_submission_envelope(buy_order(), buy_permission())


def policy() -> AuthorizationFreshnessPolicy:
    return AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE)


def history(state: str = "NEVER_DISPATCHED") -> SubmissionHistoryState:
    return SubmissionHistoryState(submission_id=SUBMISSION_ID, state=state)


def evaluate(
    *,
    evaluation_timestamp: datetime = EVALUATION_AT,
    portfolio: PortfolioState | None = None,
    price: str = "100",
    submission_history: SubmissionHistoryState | None = None,
) -> PreDispatchDecision:
    return evaluate_pre_dispatch(
        buy_envelope(),
        submission_history if submission_history is not None else history(),
        policy(),
        evaluation_timestamp,
        portfolio if portfolio is not None else create_portfolio(1000),
        {"AAPL": Decimal(price)},
        Decimal(1),
        limits(),
    )


def test_oracle_a_valid_first_dispatch_eligibility() -> None:
    decision = evaluate()
    assert isinstance(decision, PreDispatchDecision)
    assert decision.eligible is True
    assert decision.reasons == ()
    assert decision.risk_decision.approved is True
    assert decision.risk_decision.reasons == ()
    assert decision.evaluation_timestamp == EVALUATION_AT
    assert decision.evaluation_timestamp.tzinfo is not None
    assert decision.envelope.submission_id == SUBMISSION_ID
    assert decision.envelope.permission.order_fingerprint == LIVE_FINGERPRINT
    assert decision.envelope.permission.mode == "LIVE"


def test_oracle_b_stale_human_authorization() -> None:
    decision = evaluate(
        evaluation_timestamp=datetime.fromisoformat("2030-01-01T12:06:00+00:00")
    )
    assert decision.eligible is False
    assert decision.reasons == (PreDispatchReason.STALE_AUTHORIZATION,)


def test_oracle_c_exact_freshness_equality_boundary() -> None:
    decision = evaluate(
        evaluation_timestamp=datetime.fromisoformat("2030-01-01T12:05:00.000000+00:00")
    )
    assert decision.eligible is True
    assert decision.reasons == ()


def test_oracle_c_one_microsecond_beyond_the_boundary() -> None:
    decision = evaluate(evaluation_timestamp=EVALUATION_PLUS_ONE_MICROSECOND)
    assert decision.reasons == (PreDispatchReason.STALE_AUTHORIZATION,)


def test_oracle_d_future_human_authorization() -> None:
    decision = evaluate(evaluation_timestamp=EVALUATION_MINUS_ONE_MICROSECOND)
    assert decision.eligible is False
    assert decision.reasons == (PreDispatchReason.FUTURE_AUTHORIZATION,)


def test_oracle_e_fresh_risk_rejection_despite_historical_approval() -> None:
    envelope = buy_envelope()
    assert envelope.permission.risk_decision.approved is True
    decision = evaluate_pre_dispatch(
        envelope,
        history(),
        policy(),
        EVALUATION_AT,
        create_portfolio(100),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )
    assert decision.eligible is False
    assert decision.reasons == (PreDispatchReason.FRESH_RISK_REJECTED,)
    assert decision.risk_decision.approved is False
    assert RiskReason.INSUFFICIENT_CASH in decision.risk_decision.reasons
    assert envelope.permission.risk_decision.approved is True
    assert envelope.permission.risk_decision.reasons == ()


def test_oracle_f_fresh_risk_uses_the_exact_envelope_order() -> None:
    envelope = buy_envelope()
    decision = evaluate_pre_dispatch(
        envelope,
        history(),
        policy(),
        EVALUATION_AT,
        create_portfolio(500),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )
    assert decision.reasons == (PreDispatchReason.FRESH_RISK_REJECTED,)

    # A smaller order would have been approved under these same facts, so the
    # rejection proves the gate evaluated the envelope's exact order.
    smaller = evaluate_order(
        buy_order(Decimal(4)),
        create_portfolio(500),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )
    assert smaller.approved is True
    expected = evaluate_order(
        envelope.order,
        create_portfolio(500),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )
    assert decision.risk_decision == expected


def test_fresh_risk_accepts_current_facts() -> None:
    decision = evaluate(price="100")
    assert decision.eligible is True
    assert decision.risk_decision.approved is True


def test_risk_fact_errors_are_errors_not_denials() -> None:
    with pytest.raises(PreDispatchError):
        evaluate_pre_dispatch(
            buy_envelope(),
            history(),
            policy(),
            EVALUATION_AT,
            create_portfolio(1000),
            {"MSFT": Decimal(100)},
            Decimal(1),
            limits(),
        )
    with pytest.raises(PreDispatchError):
        evaluate_pre_dispatch(
            buy_envelope(),
            history(),
            policy(),
            EVALUATION_AT,
            create_portfolio(1000),
            {"AAPL": Decimal(100)},
            Decimal(-1),
            limits(),
        )
