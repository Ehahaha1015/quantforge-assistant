from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quantforge.broker import create_broker_submission_envelope
from quantforge.domain import OrderIntent
from quantforge.live import authorize_live_order, create_live_authorization
from quantforge.portfolio import create_portfolio
from quantforge.predispatch import (
    AuthorizationFreshnessPolicy,
    PreDispatchDecision,
    PreDispatchError,
    PreDispatchReason,
    SubmissionHistoryState,
    evaluate_pre_dispatch,
)
from quantforge.risk import RiskDecision, RiskLimits, evaluate_order

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
AUTHORIZED_AT = datetime.fromisoformat("2030-01-01T12:00:00+00:00")
EVALUATION_AT = datetime.fromisoformat("2030-01-01T12:05:00+00:00")
OPERATOR = "operator@example.com"
SUBMISSION_ID = "broker1-4c78154fcce6c1791cb74257a777de95"
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


def decision() -> PreDispatchDecision:
    order = buy_order()
    permission = authorize_live_order(
        order,
        evaluate_order(
            order,
            create_portfolio(1000),
            {"AAPL": Decimal(100)},
            Decimal(1),
            limits(),
        ),
        create_live_authorization(order, OPERATOR, AUTHORIZED_AT),
    )
    return evaluate_pre_dispatch(
        create_broker_submission_envelope(order, permission),
        SubmissionHistoryState(submission_id=SUBMISSION_ID, state="NEVER_DISPATCHED"),
        AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE),
        EVALUATION_AT,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )


def test_policy_round_trip_is_deterministic() -> None:
    policy = AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE)
    raw = policy.model_dump_json()
    assert raw == policy.model_dump_json()
    rebuilt = AuthorizationFreshnessPolicy.from_json(raw)
    assert rebuilt == policy
    assert rebuilt.max_authorization_age == MAX_AGE
    assert rebuilt.model_dump_json() == raw


def test_history_round_trip_is_deterministic() -> None:
    history = SubmissionHistoryState(submission_id=SUBMISSION_ID, state="ACKNOWLEDGED")
    raw = history.model_dump_json()
    assert raw == history.model_dump_json()
    rebuilt = SubmissionHistoryState.from_json(raw)
    assert rebuilt == history
    assert rebuilt.model_dump_json() == raw


def test_decision_round_trip_is_deterministic() -> None:
    artifact = decision()
    raw = artifact.model_dump_json()
    assert raw == artifact.model_dump_json()
    rebuilt = PreDispatchDecision.from_json(raw)
    assert rebuilt == artifact
    assert rebuilt.eligible is True
    assert rebuilt.reasons == ()
    assert rebuilt.model_dump_json() == raw


def _payload() -> dict[str, object]:
    return json.loads(decision().model_dump_json())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["envelope"]["order"].__setitem__("symbol", "MSFT"),
        lambda data: data["envelope"]["order"].__setitem__("quantity", "6"),
        lambda data: data["envelope"].__setitem__(
            "submission_id", "broker1-0e84b342e472496e8b9daa69a84ec87b"
        ),
        lambda data: data["envelope"]["permission"]["authorization"].__setitem__(
            "authorized_at", "2030-01-01T11:00:00Z"
        ),
        lambda data: data.__setitem__(
            "evaluation_timestamp", "2030-01-01T12:06:00Z"
        ),
        lambda data: data["policy"].__setitem__("max_authorization_age", "PT1M"),
        lambda data: data["policy"].__setitem__("max_authorization_age", "PT0S"),
        lambda data: data["policy"].__setitem__("max_authorization_age", "-PT5M"),
        lambda data: data["history"].__setitem__(
            "submission_id", "broker1-0e84b342e472496e8b9daa69a84ec87b"
        ),
        lambda data: data["history"].__setitem__("state", "OUTCOME_UNKNOWN"),
        lambda data: data["history"].__setitem__("state", "ACKNOWLEDGED"),
        lambda data: data.__setitem__("eligible", False),
        lambda data: data.__setitem__(
            "reasons", ["STALE_AUTHORIZATION", "FRESH_RISK_REJECTED"]
        ),
        lambda data: data.__setitem__("reasons", ["NOT_A_REASON"]),
        lambda data: data.pop("risk_decision"),
        lambda data: data.__setitem__("note", "extra"),
    ],
)
def test_tampered_decision_payloads_fail_closed(mutate) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises(PreDispatchError):
        PreDispatchDecision.from_json(json.dumps(payload))


def test_duplicate_and_misordered_reasons_fail_closed() -> None:
    rejected = evaluate_pre_dispatch(
        decision().envelope,
        SubmissionHistoryState(submission_id=SUBMISSION_ID, state="OUTCOME_UNKNOWN"),
        AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE),
        EVALUATION_AT,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )
    assert rejected.reasons == (PreDispatchReason.SUBMISSION_OUTCOME_UNKNOWN,)

    payload = json.loads(rejected.model_dump_json())
    payload["reasons"] = ["SUBMISSION_OUTCOME_UNKNOWN", "SUBMISSION_OUTCOME_UNKNOWN"]
    with pytest.raises(PreDispatchError):
        PreDispatchDecision.from_json(json.dumps(payload))

    two_reason = evaluate_pre_dispatch(
        decision().envelope,
        SubmissionHistoryState(submission_id=SUBMISSION_ID, state="OUTCOME_UNKNOWN"),
        AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE),
        datetime.fromisoformat("2030-01-01T12:06:00+00:00"),
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        limits(),
    )
    assert two_reason.reasons == (
        PreDispatchReason.STALE_AUTHORIZATION,
        PreDispatchReason.SUBMISSION_OUTCOME_UNKNOWN,
    )
    payload = json.loads(two_reason.model_dump_json())
    payload["reasons"] = ["SUBMISSION_OUTCOME_UNKNOWN", "STALE_AUTHORIZATION"]
    with pytest.raises(PreDispatchError):
        PreDispatchDecision.from_json(json.dumps(payload))


@pytest.mark.parametrize(
    "payload",
    ["not json", "[]", "{}", "null", "true", "1", '"decision"'],
)
def test_malformed_decision_payloads_fail_closed(payload: str) -> None:
    with pytest.raises(PreDispatchError):
        PreDispatchDecision.from_json(payload)


@pytest.mark.parametrize(
    "payload",
    ["not json", "[]", "{}", "null", '{"max_authorization_age": 5}'],
)
def test_malformed_policy_payloads_fail_closed(payload: str) -> None:
    with pytest.raises(PreDispatchError):
        AuthorizationFreshnessPolicy.from_json(payload)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        "{}",
        '{"submission_id": "' + SUBMISSION_ID + '"}',
        '{"submission_id": "' + SUBMISSION_ID + '", "state": "UNKNOWN_STATE"}',
    ],
)
def test_malformed_history_payloads_fail_closed(payload: str) -> None:
    with pytest.raises(PreDispatchError):
        SubmissionHistoryState.from_json(payload)


def test_policy_requires_a_positive_duration() -> None:
    for bad in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(PreDispatchError):
            AuthorizationFreshnessPolicy(max_authorization_age=bad)
    for bad_type in (None, True, 5, "PT5M"):
        with pytest.raises(PreDispatchError):
            AuthorizationFreshnessPolicy(max_authorization_age=bad_type)


def test_direct_construction_rejects_contradictions() -> None:
    artifact = decision()
    envelope = artifact.envelope
    history = artifact.history
    policy = artifact.policy

    with pytest.raises(PreDispatchError):
        PreDispatchDecision(
            envelope=envelope,
            history=history,
            policy=policy,
            evaluation_timestamp=EVALUATION_AT,
            risk_decision=artifact.risk_decision,
            eligible=False,
            reasons=(),
        )
    with pytest.raises(PreDispatchError):
        PreDispatchDecision(
            envelope=envelope,
            history=history,
            policy=policy,
            evaluation_timestamp=EVALUATION_AT,
            risk_decision=artifact.risk_decision,
            eligible=False,
            reasons=(PreDispatchReason.STALE_AUTHORIZATION,),
        )
    with pytest.raises(PreDispatchError):
        PreDispatchDecision(
            envelope=envelope,
            history=history,
            policy=policy,
            evaluation_timestamp=datetime.fromisoformat("2030-01-01T12:06:00+00:00"),
            risk_decision=artifact.risk_decision,
            eligible=True,
            reasons=(),
        )
    with pytest.raises(PreDispatchError):
        PreDispatchDecision(
            envelope=envelope,
            history=SubmissionHistoryState(
                submission_id=SUBMISSION_ID, state="OUTCOME_UNKNOWN"
            ),
            policy=policy,
            evaluation_timestamp=EVALUATION_AT,
            risk_decision=artifact.risk_decision,
            eligible=True,
            reasons=(),
        )
    with pytest.raises(PreDispatchError):
        PreDispatchDecision(
            envelope=envelope,
            history=history,
            policy=policy,
            evaluation_timestamp=EVALUATION_AT,
            risk_decision=RiskDecision.model_construct(
                approved=False, reasons=(), detail=""
            ),
            eligible=True,
            reasons=(),
        )


def test_direct_construction_rejects_wrong_nested_types() -> None:
    artifact = decision()
    with pytest.raises(PreDispatchError):
        PreDispatchDecision(
            envelope=artifact.envelope.model_dump(),
            history=artifact.history,
            policy=artifact.policy,
            evaluation_timestamp=EVALUATION_AT,
            risk_decision=artifact.risk_decision,
            eligible=True,
            reasons=(),
        )
    with pytest.raises(PreDispatchError):
        PreDispatchDecision(
            envelope=artifact.envelope,
            history=artifact.history.model_dump(),
            policy=artifact.policy,
            evaluation_timestamp=EVALUATION_AT,
            risk_decision=artifact.risk_decision,
            eligible=True,
            reasons=(),
        )
    with pytest.raises(PreDispatchError):
        PreDispatchDecision(
            envelope=artifact.envelope,
            history=artifact.history,
            policy=artifact.policy.model_dump(),
            evaluation_timestamp=EVALUATION_AT,
            risk_decision=artifact.risk_decision,
            eligible=True,
            reasons=(),
        )


def test_evaluation_timestamp_must_be_aware_in_construction_and_evaluation() -> None:
    artifact = decision()
    naive = datetime(2030, 1, 1, 12, 5)  # noqa: DTZ001 - the naive value is the case
    assert naive.tzinfo is None
    with pytest.raises(PreDispatchError):
        PreDispatchDecision(
            envelope=artifact.envelope,
            history=artifact.history,
            policy=artifact.policy,
            evaluation_timestamp=naive,
            risk_decision=artifact.risk_decision,
            eligible=True,
            reasons=(),
        )
    with pytest.raises(PreDispatchError):
        evaluate_pre_dispatch(
            artifact.envelope,
            artifact.history,
            artifact.policy,
            naive,
            create_portfolio(1000),
            {"AAPL": Decimal(100)},
            Decimal(1),
            limits(),
        )
