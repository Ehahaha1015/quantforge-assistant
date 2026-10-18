from __future__ import annotations

import pytest
from _support import SUBMISSION_ID, TARGET_JSON, make_observation

from quantforge.execution_ledger import (
    BrokerObservationStatus,
    ExecutionLedgerCorruptionError,
    ExecutionLedgerDeniedError,
    ReconciliationConflict,
    ReconciliationResult,
)
from quantforge.execution_ledger.identity import derive_attempt_id
from quantforge.execution_ledger.reconciliation import (
    assess,
    raise_on_conflict,
    recorded_assessment,
)


def _conflict_assessment():
    return assess(
        execstate_state="ACKNOWLEDGED",
        observation=make_observation(
            status=BrokerObservationStatus.REJECTED,
            broker_reference="broker-ref-2",
        ),
    )


def test_raise_on_conflict_raises_the_distinct_error() -> None:
    assessment = _conflict_assessment()
    with pytest.raises(ReconciliationConflict) as excinfo:
        raise_on_conflict(assessment)
    assert excinfo.value.assessment is assessment
    assert not isinstance(excinfo.value, ExecutionLedgerCorruptionError)


def test_raise_on_conflict_is_noop_for_non_conflicts() -> None:
    compatible = assess(
        execstate_state="DISPATCHING",
        observation=make_observation(),
    )
    unknown = assess(
        execstate_state="OUTCOME_UNKNOWN",
        observation=make_observation(),
    )
    assert raise_on_conflict(compatible) is None
    assert raise_on_conflict(unknown) is None


def test_recorded_assessment_marks_recorded_evidence() -> None:
    compatible = assess(
        execstate_state="DISPATCHING",
        observation=make_observation(),
    )
    recorded = recorded_assessment(
        compatible, "execstate-observation-v1-1"
    )
    assert recorded.recorded is True
    assert recorded.observation_id == "execstate-observation-v1-1"
    assert recorded.result is ReconciliationResult.COMPATIBLE
    unknown = assess(
        execstate_state="OUTCOME_UNKNOWN",
        observation=make_observation(),
    )
    assert (
        recorded_assessment(
            unknown, "execstate-observation-v1-2"
        ).recorded
        is True
    )
    with pytest.raises(ExecutionLedgerDeniedError):
        recorded_assessment(
            _conflict_assessment(), "execstate-observation-v1-3"
        )


def test_conflict_and_corruption_are_mutually_exclusive() -> None:
    try:
        raise_on_conflict(_conflict_assessment())
    except ExecutionLedgerCorruptionError:  # pragma: no cover
        pytest.fail("conflict was reported as corruption")
    except ReconciliationConflict:
        pass
    try:
        assess(
            execstate_state="DISPATCHING",
            observation=make_observation(),
            recorded=(make_observation(generation=2),),
        )
    except ReconciliationConflict:  # pragma: no cover
        pytest.fail("corruption was reported as a conflict")
    except ExecutionLedgerCorruptionError:
        pass


def test_supplied_attempt_id_mismatch_is_a_corruption_error() -> None:
    """Reviewer A CH-B: contract 4.2 rule 3/4, I23 and Oracle P.

    A caller-supplied identifier that differs from the deterministically
    derived identity is internal contradiction: it fails closed as the
    corruption family, surfaces no alternate identity, and is never routed
    into external reconciliation-disagreement handling.
    """

    observation = make_observation()
    other_attempt = derive_attempt_id(
        SUBMISSION_ID, TARGET_JSON, 2, 1
    )
    before = observation
    with pytest.raises(ExecutionLedgerCorruptionError) as raised:
        assess(
            execstate_state="DISPATCHING",
            observation=observation,
            attempt_id=other_attempt,
        )
    assert not isinstance(raised.value, ReconciliationConflict)
    # No alternate or replacement identifier is surfaced.
    assert other_attempt not in str(raised.value)
    # Zero mutation: the explicit inputs are untouched.
    assert observation == before


def test_malformed_attempt_id_is_denied() -> None:
    with pytest.raises(ExecutionLedgerDeniedError):
        assess(
            execstate_state="DISPATCHING",
            observation=make_observation(),
            attempt_id="not-an-attempt",
        )


def test_assessment_has_zero_side_effects_on_inputs() -> None:
    recorded = make_observation(
        observation_id="execstate-observation-v1-1"
    )
    candidate = make_observation(
        broker_reference="broker-ref-2"
    )
    recorded_before = (recorded,)
    assessment = assess(
        execstate_state="DISPATCHING",
        observation=candidate,
        recorded=recorded_before,
    )
    assert recorded_before == (recorded,)
    assert candidate == make_observation(
        broker_reference="broker-ref-2"
    )
    assert assessment.recorded is False
