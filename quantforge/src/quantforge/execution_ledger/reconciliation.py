"""Pure reconciliation logic for the broker-neutral execution ledger.

This module reads nothing, writes nothing, and holds no state. It compares
one normalized external observation against authoritative EXECSTATE state and
already-recorded observations, and returns an informational assessment.

An external disagreement is never a corruption: matrix conflicts return a
CONFLICT assessment and are raised as `ReconciliationConflict` by
`raise_on_conflict`. Evidence from a different attempt tuple is an internal
contradiction and raises `ExecutionLedgerCorruptionError` instead.
"""

from __future__ import annotations

from dataclasses import replace

from .errors import (
    ExecutionLedgerCorruptionError,
    ExecutionLedgerDeniedError,
    ReconciliationConflict,
)
from .identity import (
    derive_attempt_id,
    validate_attempt_id,
    validate_observation_id,
)
from .models import (
    EXECSTATE_STATE_LITERALS,
    AttemptPhase,
    BrokerObservationStatus,
    BrokerOrderObservation,
    ReconciliationAssessment,
    ReconciliationReason,
    ReconciliationResult,
    observation_semantic_key,
)

__all__ = [
    "COMPATIBILITY_MATRIX",
    "assess",
    "is_contradictory",
    "is_exact_replay",
    "raise_on_conflict",
    "recorded_assessment",
]

_A = BrokerObservationStatus.ACCEPTED
_R = BrokerObservationStatus.REJECTED
_U = BrokerObservationStatus.UNKNOWN

_C = ReconciliationResult.CONFLICT
_K = ReconciliationResult.COMPATIBLE
_N = ReconciliationResult.UNKNOWN

#: Frozen compatibility matrix: (state, status, result, reason, phase).
#:
#: The matrix is total over the six authoritative EXECSTATE state literals and
#: the three broker-neutral observation statuses.
COMPATIBILITY_MATRIX: tuple[
    tuple[
        str,
        BrokerObservationStatus,
        ReconciliationResult,
        ReconciliationReason,
        AttemptPhase,
    ],
    ...,
] = (
    (
        "PREPARED",
        _A,
        _C,
        ReconciliationReason.CONFLICT_NO_DISPATCH_RECORD,
        AttemptPhase.UNKNOWN,
    ),
    (
        "PREPARED",
        _R,
        _C,
        ReconciliationReason.CONFLICT_NO_DISPATCH_RECORD,
        AttemptPhase.UNKNOWN,
    ),
    (
        "PREPARED",
        _U,
        _N,
        ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    (
        "DISPATCHING",
        _A,
        _K,
        ReconciliationReason.COMPATIBLE_ACCEPTED_ON_DISPATCHING,
        AttemptPhase.OBSERVED,
    ),
    (
        "DISPATCHING",
        _R,
        _K,
        ReconciliationReason.COMPATIBLE_REJECTED_ON_DISPATCHING,
        AttemptPhase.OBSERVED,
    ),
    (
        "DISPATCHING",
        _U,
        _N,
        ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    (
        "ACKNOWLEDGED",
        _A,
        _K,
        ReconciliationReason.COMPATIBLE_FINAL_ACCEPTED,
        AttemptPhase.FINALIZED,
    ),
    (
        "ACKNOWLEDGED",
        _R,
        _C,
        ReconciliationReason.CONFLICT_TERMINAL_ACCEPTED_VS_REJECTED,
        AttemptPhase.UNKNOWN,
    ),
    (
        "ACKNOWLEDGED",
        _U,
        _N,
        ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    (
        "REJECTED",
        _A,
        _C,
        ReconciliationReason.CONFLICT_TERMINAL_REJECTED_VS_ACCEPTED,
        AttemptPhase.UNKNOWN,
    ),
    (
        "REJECTED",
        _R,
        _K,
        ReconciliationReason.COMPATIBLE_FINAL_REJECTED,
        AttemptPhase.FINALIZED,
    ),
    (
        "REJECTED",
        _U,
        _N,
        ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    (
        "OUTCOME_UNKNOWN",
        _A,
        _K,
        ReconciliationReason.COMPATIBLE_OBSERVATION_ON_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    (
        "OUTCOME_UNKNOWN",
        _R,
        _K,
        ReconciliationReason.COMPATIBLE_OBSERVATION_ON_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    (
        "OUTCOME_UNKNOWN",
        _U,
        _K,
        ReconciliationReason.COMPATIBLE_OBSERVATION_ON_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    (
        "NO_EFFECT_CONFIRMED",
        _A,
        _C,
        ReconciliationReason.CONFLICT_NO_EFFECT_VS_ACCEPTED,
        AttemptPhase.UNKNOWN,
    ),
    (
        "NO_EFFECT_CONFIRMED",
        _R,
        _N,
        ReconciliationReason.UNKNOWN_LOCAL_AMBIGUOUS,
        AttemptPhase.UNKNOWN,
    ),
    (
        "NO_EFFECT_CONFIRMED",
        _U,
        _N,
        ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
)

_MATRIX = {
    (state, status): (result, reason, phase)
    for state, status, result, reason, phase in COMPATIBILITY_MATRIX
}

_OPPOSITE_STATUS = {
    BrokerObservationStatus.ACCEPTED: BrokerObservationStatus.REJECTED,
    BrokerObservationStatus.REJECTED: BrokerObservationStatus.ACCEPTED,
}


def _require_observation(observation: object) -> BrokerOrderObservation:
    if not isinstance(observation, BrokerOrderObservation):
        raise TypeError("observation must be a BrokerOrderObservation")
    return observation


def _require_recorded(
    recorded: object,
) -> tuple[BrokerOrderObservation, ...]:
    if not isinstance(recorded, tuple):
        raise TypeError("recorded must be a tuple of observations")
    for item in recorded:
        if not isinstance(item, BrokerOrderObservation):
            raise TypeError(
                "recorded must contain BrokerOrderObservation values"
            )
    return recorded


def _attempt_id(observation: BrokerOrderObservation) -> str:
    return derive_attempt_id(
        observation.submission_id,
        observation.target_json,
        observation.generation,
        observation.recovery_epoch,
    )


def is_exact_replay(
    observation: BrokerOrderObservation,
    recorded: tuple[BrokerOrderObservation, ...],
) -> bool:
    """Return whether an observation is an exact semantic replay."""

    candidate = _require_observation(observation)
    history = _require_recorded(recorded)
    semantic_key = observation_semantic_key(candidate)
    return any(
        observation_semantic_key(item) == semantic_key for item in history
    )


def is_contradictory(
    observation: BrokerOrderObservation,
    recorded: tuple[BrokerOrderObservation, ...],
) -> bool:
    """Return whether opposing recorded evidence blocks this observation."""

    candidate = _require_observation(observation)
    history = _require_recorded(recorded)
    opposite = _OPPOSITE_STATUS.get(candidate.status)
    if opposite is None:
        return False
    semantic_key = observation_semantic_key(candidate)
    return any(
        item.status is opposite
        and observation_semantic_key(item) != semantic_key
        for item in history
    )


def assess(
    *,
    execstate_state: str,
    observation: BrokerOrderObservation,
    recorded: tuple[BrokerOrderObservation, ...] = (),
    attempt_id: str | None = None,
) -> ReconciliationAssessment:
    """Return the pure reconciliation assessment for one observation."""

    candidate = _require_observation(observation)
    history = _require_recorded(recorded)
    if not isinstance(execstate_state, str):
        raise TypeError("execstate_state must be a string")
    if execstate_state not in EXECSTATE_STATE_LITERALS:
        raise ExecutionLedgerDeniedError(
            "execstate_state is not a contracted literal"
        )
    derived = _attempt_id(candidate)
    for item in history:
        if _attempt_id(item) != derived:
            raise ExecutionLedgerCorruptionError(
                "recorded observation belongs to a different attempt"
            )
    expected = derived if attempt_id is None else validate_attempt_id(attempt_id)
    if expected != derived:
        # Contract 4.2 rule 3/4 and I23: a caller-supplied identifier that
        # differs from the deterministically derived identity is internal
        # contradiction. It fails closed with zero mutation, surfaces no
        # alternate identifier, and is never reported as an external
        # reconciliation disagreement.
        raise ExecutionLedgerCorruptionError(
            "caller-supplied attempt identity differs from the derived identity"
        )
    result, reason, phase = _MATRIX[(execstate_state, candidate.status)]
    if (
        result is not ReconciliationResult.CONFLICT
        and is_contradictory(candidate, history)
    ):
        result = ReconciliationResult.CONFLICT
        reason = ReconciliationReason.CONFLICT_CONTRADICTORY_OBSERVATION
        phase = AttemptPhase.UNKNOWN
    return ReconciliationAssessment(
        attempt_id=derived,
        observation_id=None,
        result=result,
        reason=reason,
        required_phase=phase,
        recorded=False,
        existing_state=execstate_state,
    )


def raise_on_conflict(assessment: ReconciliationAssessment) -> None:
    """Raise `ReconciliationConflict` for a conflicting assessment."""

    if not isinstance(assessment, ReconciliationAssessment):
        raise TypeError("assessment must be a ReconciliationAssessment")
    if assessment.result is ReconciliationResult.CONFLICT:
        raise ReconciliationConflict(
            "observation conflicts with durable execution truth",
            assessment=assessment,
        )


def recorded_assessment(
    assessment: ReconciliationAssessment, observation_id: str
) -> ReconciliationAssessment:
    """Mark one non-conflicting assessment as recorded evidence."""

    if not isinstance(assessment, ReconciliationAssessment):
        raise TypeError("assessment must be a ReconciliationAssessment")
    if assessment.result is ReconciliationResult.CONFLICT:
        raise ExecutionLedgerDeniedError(
            "a reconciliation conflict cannot be recorded"
        )
    return replace(
        assessment,
        observation_id=validate_observation_id(observation_id),
        recorded=True,
    )
