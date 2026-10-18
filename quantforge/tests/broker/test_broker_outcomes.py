from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest

from quantforge.broker import (
    BrokerBoundaryError,
    BrokerPreDispatchError,
    BrokerSubmissionEnvelope,
    BrokerSubmissionOutcome,
    broker_submission_id,
    create_broker_submission_envelope,
)
from quantforge.domain import OrderIntent
from quantforge.live import (
    LiveExecutionPermission,
    authorize_live_order,
    create_live_authorization,
)
from quantforge.portfolio import PortfolioState, create_portfolio
from quantforge.risk import RiskLimits, evaluate_order

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
AUTH_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:31:00+00:00")
OPERATOR = "operator@example.com"

BUY_SUBMISSION_ID = "broker1-4c78154fcce6c1791cb74257a777de95"
BUY_LIVE_FINGERPRINT = "live1-d7a22c60d95e61f1103062d3d4189a21"
PAPER_BUY_IDENTIFIER = "paper1-d7a22c60d95e61f1103062d3d4189a21"

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


def envelope() -> BrokerSubmissionEnvelope:
    return create_broker_submission_envelope(buy_order(), buy_permission())


def test_accepted_outcome_representation() -> None:
    outcome = BrokerSubmissionOutcome(
        submission_id=BUY_SUBMISSION_ID,
        status="ACCEPTED",
        broker_reference="BROKER-REF-1",
        broker_message="accepted",
    )
    assert outcome.status == "ACCEPTED"
    assert outcome.broker_reference == "BROKER-REF-1"
    assert set(outcome.model_dump()) == {
        "submission_id",
        "status",
        "broker_reference",
        "broker_message",
    }
    payload = outcome.model_dump_json()
    for absent in ("price", "quantity", "fee", "position", "cash", "fill"):
        assert absent not in payload


def test_optional_broker_evidence_rules() -> None:
    accepted = BrokerSubmissionOutcome(submission_id=BUY_SUBMISSION_ID, status="ACCEPTED")
    assert accepted.broker_reference is None
    assert accepted.broker_message is None

    for blank in ("", "   ", "\t"):
        with pytest.raises(BrokerBoundaryError):
            BrokerSubmissionOutcome(
                submission_id=BUY_SUBMISSION_ID,
                status="ACCEPTED",
                broker_reference=blank,
            )
        with pytest.raises(BrokerBoundaryError):
            BrokerSubmissionOutcome(
                submission_id=BUY_SUBMISSION_ID,
                status="REJECTED",
                broker_message=blank,
            )


def test_rejected_outcome_is_distinct_from_unknown() -> None:
    rejected = BrokerSubmissionOutcome(
        submission_id=BUY_SUBMISSION_ID, status="REJECTED", broker_message="risk limit"
    )
    unknown = BrokerSubmissionOutcome(
        submission_id=BUY_SUBMISSION_ID, status="OUTCOME_UNKNOWN"
    )
    assert rejected.status == "REJECTED"
    assert unknown.status == "OUTCOME_UNKNOWN"
    assert rejected != unknown
    assert rejected.model_dump_json() != unknown.model_dump_json()


def test_ambiguous_outcome_is_first_class() -> None:
    unknown = BrokerSubmissionOutcome(
        submission_id=BUY_SUBMISSION_ID, status="OUTCOME_UNKNOWN"
    )
    assert unknown.status != "REJECTED"
    assert unknown is not False
    assert unknown is not None
    assert broker_submission_id(buy_order()) == unknown.submission_id


def test_pre_dispatch_error_is_distinct_from_the_boundary_error() -> None:
    assert BrokerPreDispatchError is not BrokerBoundaryError
    assert issubclass(BrokerPreDispatchError, ValueError)
    assert issubclass(BrokerBoundaryError, ValueError)
    assert not issubclass(BrokerPreDispatchError, BrokerBoundaryError)


def test_outcomes_do_not_create_canonical_execution_evidence() -> None:
    state = create_portfolio(1000)
    before = state.model_dump()
    for status in ("ACCEPTED", "REJECTED", "OUTCOME_UNKNOWN"):
        BrokerSubmissionOutcome(submission_id=BUY_SUBMISSION_ID, status=status)
    assert isinstance(state, PortfolioState)
    assert state.model_dump() == before


def test_outcome_serialization_round_trip_is_deterministic() -> None:
    outcome = BrokerSubmissionOutcome(
        submission_id=BUY_SUBMISSION_ID,
        status="REJECTED",
        broker_reference="BROKER-REF-2",
        broker_message="rejected",
    )
    first = outcome.model_dump_json()
    assert first == outcome.model_dump_json()
    rebuilt = BrokerSubmissionOutcome.from_json(first)
    assert rebuilt == outcome
    assert rebuilt.model_dump_json() == first


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.__setitem__("status", "FILLED"),
        lambda data: data.__setitem__("status", "accepted"),
        lambda data: data.__setitem__("status", "UNKNOWN"),
        lambda data: data.__setitem__("submission_id", BUY_LIVE_FINGERPRINT),
        lambda data: data.__setitem__("submission_id", PAPER_BUY_IDENTIFIER),
        lambda data: data.__setitem__("submission_id", "broker1-" + "a" * 31),
        lambda data: data.__setitem__("broker_reference", ""),
        lambda data: data.__setitem__("broker_message", "   "),
        lambda data: data.__setitem__("note", "extra"),
        lambda data: data.pop("status"),
        lambda data: data.__setitem__("status", None),
        lambda data: data.__setitem__("status", True),
    ],
)
def test_tampered_outcome_payloads_fail_closed(mutate) -> None:
    payload = json.loads(
        BrokerSubmissionOutcome(
            submission_id=BUY_SUBMISSION_ID,
            status="OUTCOME_UNKNOWN",
            broker_reference=None,
            broker_message=None,
        ).model_dump_json()
    )
    mutate(payload)
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionOutcome.from_json(json.dumps(payload))


def test_malformed_outcome_payloads_fail_closed() -> None:
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionOutcome.from_json("not json")
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionOutcome.from_json("[]")
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionOutcome.from_json("{}")


def test_literal_dispatch_loss_scenario() -> None:
    submission = envelope()
    submission_id = submission.submission_id
    assert submission_id == BUY_SUBMISSION_ID

    # Dispatch may have begun; the response is lost or unusable.
    outcome = BrokerSubmissionOutcome(
        submission_id=submission_id, status="OUTCOME_UNKNOWN"
    )

    assert outcome.status == "OUTCOME_UNKNOWN"
    assert outcome.status != "REJECTED"
    assert broker_submission_id(buy_order()) == submission_id
    assert not hasattr(outcome, "retry")
    assert not hasattr(submission, "retry")
