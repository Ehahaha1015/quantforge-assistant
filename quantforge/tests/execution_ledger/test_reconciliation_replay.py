from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _support import SUBMISSION_ID, make_observation

from quantforge.execution_ledger import (
    AttemptPhase,
    BrokerObservationStatus,
    ExecutionLedgerCorruptionError,
    ReconciliationReason,
    ReconciliationResult,
)
from quantforge.execution_ledger.reconciliation import (
    assess,
    is_contradictory,
    is_exact_replay,
)


def test_exact_replay_detection() -> None:
    first = make_observation(
        observation_id="execstate-observation-v1-1"
    )
    same_observation = make_observation(
        observation_id="execstate-observation-v1-2"
    )
    assert is_exact_replay(same_observation, (first,)) is True
    assert is_exact_replay(first, ()) is False
    assert (
        is_exact_replay(
            make_observation(broker_reference="broker-ref-2"), (first,)
        )
        is False
    )
    assert (
        is_exact_replay(
            make_observation(
                observed_at=datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC)
            ),
            (first,),
        )
        is False
    )
    assert (
        is_exact_replay(
            make_observation(
                status=BrokerObservationStatus.REJECTED,
                broker_reference="broker-ref-1",
            ),
            (first,),
        )
        is False
    )


def test_exact_replay_remains_a_compatible_assessment() -> None:
    recorded = make_observation(
        observation_id="execstate-observation-v1-1"
    )
    replay = make_observation(
        observation_id="execstate-observation-v1-2"
    )
    assessment = assess(
        execstate_state="DISPATCHING",
        observation=replay,
        recorded=(recorded,),
    )
    assert assessment.result is ReconciliationResult.COMPATIBLE
    assert assessment.reason is (
        ReconciliationReason.COMPATIBLE_ACCEPTED_ON_DISPATCHING
    )
    assert assessment.recorded is False


def test_contradiction_detection() -> None:
    accepted = make_observation(
        status=BrokerObservationStatus.ACCEPTED
    )
    rejected = make_observation(
        status=BrokerObservationStatus.REJECTED,
        broker_reference="broker-ref-2",
    )
    unknown = make_observation(
        status=BrokerObservationStatus.UNKNOWN,
        broker_reference="broker-ref-3",
    )
    assert is_contradictory(rejected, (accepted,)) is True
    assert is_contradictory(accepted, (rejected,)) is True
    assert is_contradictory(accepted, (unknown,)) is False
    assert is_contradictory(unknown, (accepted,)) is False
    assert is_contradictory(accepted, (accepted,)) is False
    assert is_contradictory(accepted, ()) is False
    assert (
        is_contradictory(
            make_observation(observation_id="execstate-observation-v1-1"),
            (make_observation(observation_id="execstate-observation-v1-2"),),
        )
        is False
    )


def test_contradiction_overrides_a_compatible_matrix_entry() -> None:
    recorded = make_observation(
        status=BrokerObservationStatus.REJECTED,
        broker_reference="broker-ref-2",
    )
    candidate = make_observation(
        status=BrokerObservationStatus.ACCEPTED,
        broker_reference="broker-ref-1",
    )
    assessment = assess(
        execstate_state="DISPATCHING",
        observation=candidate,
        recorded=(recorded,),
    )
    assert assessment.result is ReconciliationResult.CONFLICT
    assert assessment.reason is (
        ReconciliationReason.CONFLICT_CONTRADICTORY_OBSERVATION
    )
    assert assessment.required_phase is AttemptPhase.UNKNOWN
    assert assessment.recorded is False


def test_matrix_conflict_takes_precedence_over_contradiction() -> None:
    recorded = make_observation(
        status=BrokerObservationStatus.ACCEPTED
    )
    candidate = make_observation(
        status=BrokerObservationStatus.REJECTED,
        broker_reference="broker-ref-2",
    )
    assessment = assess(
        execstate_state="ACKNOWLEDGED",
        observation=candidate,
        recorded=(recorded,),
    )
    assert assessment.result is ReconciliationResult.CONFLICT
    assert assessment.reason is (
        ReconciliationReason.CONFLICT_TERMINAL_ACCEPTED_VS_REJECTED
    )


def test_mixed_attempt_recorded_observations_are_corruption() -> None:
    recorded = make_observation(generation=2)
    candidate = make_observation()
    with pytest.raises(ExecutionLedgerCorruptionError):
        assess(
            execstate_state="DISPATCHING",
            observation=candidate,
            recorded=(recorded,),
        )
    assert recorded.submission_id == SUBMISSION_ID
