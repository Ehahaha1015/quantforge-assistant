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
from quantforge.portfolio import create_portfolio
from quantforge.predispatch import (
    AuthorizationFreshnessPolicy,
    PreDispatchDecision,
    PreDispatchError,
    PreDispatchReason,
    SubmissionHistoryState,
    evaluate_pre_dispatch,
)
from quantforge.risk import RiskLimits, evaluate_order

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
AUTHORIZED_AT = datetime.fromisoformat("2030-01-01T12:00:00+00:00")
EVALUATION_AT = datetime.fromisoformat("2030-01-01T12:05:00+00:00")
LATER_EVALUATION_AT = datetime.fromisoformat("2030-01-01T12:06:00+00:00")
OPERATOR = "operator@example.com"

SUBMISSION_ID = "broker1-4c78154fcce6c1791cb74257a777de95"
NEAR_MISS_SUBMISSION_ID = "broker1-4c78154fcce6c1791cb74257a777de96"
PAPER_IDENTIFIER = "paper1-d7a22c60d95e61f1103062d3d4189a21"
LIVE_FINGERPRINT = "live1-d7a22c60d95e61f1103062d3d4189a21"

MAX_AGE = timedelta(minutes=5)


def limits() -> RiskLimits:
    return RiskLimits(
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


def evaluate(
    state: str,
    *,
    submission_id: str = SUBMISSION_ID,
    evaluation_timestamp: datetime = EVALUATION_AT,
    portfolio_cash: int = 1000,
) -> PreDispatchDecision:
    return evaluate_pre_dispatch(
        buy_envelope(),
        SubmissionHistoryState(submission_id=submission_id, state=state),
        AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE),
        evaluation_timestamp,
        create_portfolio(portfolio_cash),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )


def test_oracle_g_unresolved_unknown_denies_eligibility() -> None:
    decision = evaluate("OUTCOME_UNKNOWN")
    assert decision.eligible is False
    assert decision.reasons == (PreDispatchReason.SUBMISSION_OUTCOME_UNKNOWN,)
    assert decision.envelope.submission_id == SUBMISSION_ID
    assert decision.history.submission_id == SUBMISSION_ID


def test_oracle_h_reconciliation_mismatch_fails_closed() -> None:
    decision = evaluate("NEVER_DISPATCHED", submission_id=NEAR_MISS_SUBMISSION_ID)
    assert decision.eligible is False
    assert decision.reasons == (PreDispatchReason.RECONCILIATION_MISMATCH,)
    assert decision.history.state == "NEVER_DISPATCHED"


def test_reconciliation_fact_for_another_identity_never_clears_this_one() -> None:
    permissive = SubmissionHistoryState(
        submission_id=NEAR_MISS_SUBMISSION_ID, state="NEVER_DISPATCHED"
    )
    unsafe = SubmissionHistoryState(
        submission_id=NEAR_MISS_SUBMISSION_ID, state="OUTCOME_UNKNOWN"
    )
    mitigated = evaluate_pre_dispatch(
        buy_envelope(),
        permissive,
        AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE),
        EVALUATION_AT,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )
    unresolved = evaluate_pre_dispatch(
        buy_envelope(),
        unsafe,
        AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE),
        EVALUATION_AT,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )
    assert mitigated.reasons == (PreDispatchReason.RECONCILIATION_MISMATCH,)
    assert unresolved.reasons == (PreDispatchReason.RECONCILIATION_MISMATCH,)


@pytest.mark.parametrize(
    "identifier",
    [
        PAPER_IDENTIFIER,
        LIVE_FINGERPRINT,
        SUBMISSION_ID.upper(),
        " " + SUBMISSION_ID,
        SUBMISSION_ID + " ",
        "broker1-" + "a" * 31,
        "broker1-" + "a" * 33,
        "broker1-" + "z" * 32,
        "broker2-" + "a" * 32,
        "",
    ],
)
def test_non_canonical_history_identities_are_rejected(identifier: str) -> None:
    with pytest.raises(PreDispatchError):
        SubmissionHistoryState(submission_id=identifier, state="NEVER_DISPATCHED")


def test_oracle_i_acknowledged_prior_submission_is_not_duplicate_eligible() -> None:
    decision = evaluate("ACKNOWLEDGED")
    assert decision.eligible is False
    assert decision.reasons == (PreDispatchReason.SUBMISSION_ALREADY_ACKNOWLEDGED,)


def test_oracle_j_no_effect_confirmed_stays_denied() -> None:
    decision = evaluate("NO_EFFECT_CONFIRMED")
    assert decision.eligible is False
    assert decision.reasons == (PreDispatchReason.RE_DISPATCH_DEFERRED,)


@pytest.mark.parametrize(
    "bad_history",
    [None, True, False, {}, "NEVER_DISPATCHED", 1],
)
def test_oracle_k_missing_or_invalid_history_is_a_type_error(bad_history: object) -> None:
    with pytest.raises(TypeError):
        evaluate_pre_dispatch(
            buy_envelope(),
            bad_history,
            AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE),
            EVALUATION_AT,
            create_portfolio(1000),
            {"AAPL": Decimal(100)},
            Decimal(1),
            limits(),
        )


def test_oracle_k_history_argument_has_no_default() -> None:
    import inspect

    parameters = inspect.signature(evaluate_pre_dispatch).parameters
    assert list(parameters) == [
        "envelope",
        "history",
        "policy",
        "evaluation_timestamp",
        "portfolio",
        "risk_prices",
        "estimated_fee",
        "limits",
    ]
    for parameter in parameters.values():
        assert parameter.default is inspect.Parameter.empty


def test_first_dispatch_provenance_is_an_explicit_bound_fact() -> None:
    eligible = evaluate("NEVER_DISPATCHED")
    assert eligible.eligible is True
    assert eligible.reasons == ()
    assert eligible.history.state == "NEVER_DISPATCHED"
    assert eligible.history.submission_id == eligible.envelope.submission_id

    for state in ("OUTCOME_UNKNOWN", "ACKNOWLEDGED", "NO_EFFECT_CONFIRMED"):
        decision = evaluate(state)
        assert decision.eligible is False
        assert decision.reasons != ()


def test_oracle_l_temporal_replay_is_not_reusable_forever() -> None:
    first = evaluate("NEVER_DISPATCHED")
    assert first.eligible is True
    assert first.evaluation_timestamp == EVALUATION_AT

    later = evaluate("NEVER_DISPATCHED", evaluation_timestamp=LATER_EVALUATION_AT)
    assert later.eligible is False
    assert later.reasons == (PreDispatchReason.STALE_AUTHORIZATION,)
    assert later.evaluation_timestamp == LATER_EVALUATION_AT
    assert first.evaluation_timestamp != later.evaluation_timestamp


def test_multi_reason_oracle_canonical_order() -> None:
    decision = evaluate(
        "OUTCOME_UNKNOWN",
        evaluation_timestamp=LATER_EVALUATION_AT,
        portfolio_cash=100,
    )
    assert decision.eligible is False
    assert decision.reasons == (
        PreDispatchReason.STALE_AUTHORIZATION,
        PreDispatchReason.FRESH_RISK_REJECTED,
        PreDispatchReason.SUBMISSION_OUTCOME_UNKNOWN,
    )


def test_stale_risk_scenario_from_the_contract() -> None:
    envelope = buy_envelope()
    assert envelope.permission.risk_decision.approved is True
    decision = evaluate_pre_dispatch(
        envelope,
        SubmissionHistoryState(submission_id=SUBMISSION_ID, state="NEVER_DISPATCHED"),
        AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE),
        EVALUATION_AT,
        create_portfolio(100),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )
    assert decision.reasons == (PreDispatchReason.FRESH_RISK_REJECTED,)
    assert envelope.permission.risk_decision.approved is True


def test_unknown_scenario_denies_without_override() -> None:
    decision = evaluate("OUTCOME_UNKNOWN")
    assert decision.reasons == (PreDispatchReason.SUBMISSION_OUTCOME_UNKNOWN,)
    assert decision.envelope.submission_id == SUBMISSION_ID
