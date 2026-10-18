"""Pure projection logic for the broker-neutral execution ledger.

This module reads nothing, writes nothing, and holds no state. It derives a
read-only `AttemptPhase` label and an `ExecutionAttemptView` from normalized
EXECSTATE audit facts, recorded observation evidence, and informational
reconciliation assessments.

The projections are never dispatch authority: EXECSTATE remains the only
authoritative state machine, and no phase is persisted anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from quantforge.frontier import ExecutionTargetBinding

from ._canonical import parse_canonical_instant
from .errors import ExecutionLedgerCorruptionError, ExecutionLedgerDeniedError
from .identity import (
    derive_attempt_id,
    parse_observation_id,
    validate_submission_id,
)
from .models import (
    EXECSTATE_STATE_LITERALS,
    AttemptPhase,
    BrokerObservationStatus,
    BrokerOrderObservation,
    ExecutionAttemptView,
    ReconciliationAssessment,
    ReconciliationResult,
)
from .reconciliation import COMPATIBILITY_MATRIX

__all__ = [
    "PHASE_REASONS",
    "AuditFact",
    "project_attempt",
    "project_history",
    "project_phase",
]

PHASE_REASONS = frozenset(
    {
        "UNKNOWN_LOCAL_OUTCOME_UNKNOWN",
        "UNKNOWN_RECORDED_OBSERVATION",
        "UNKNOWN_ASSESSMENT",
        "UNKNOWN_ASSESSMENT_CONFLICT",
        "UNKNOWN_RECORDED_CONFLICT",
        "FINALIZED_ACKNOWLEDGED",
        "FINALIZED_REJECTED",
        "FINALIZED_NO_EFFECT_CONFIRMED",
        "OBSERVED_DISPATCHING_WITH_OBSERVATION",
        "DISPATCHING_RECORDED",
        "CLAIMED_ACTIVE_CLAIM",
        "PREPARED_NO_CLAIM",
    }
)

_TERMINAL_STATES = frozenset(
    {"ACKNOWLEDGED", "REJECTED", "NO_EFFECT_CONFIRMED"}
)

_OBSERVATIONAL_REASONS = frozenset(
    {
        "CLAIM_REPLAY",
        "CLAIM_CONFLICT",
        "DUPLICATE_PREPARE_REPLAY",
        "DISPATCHING_REPLAY",
        "TRANSPORT_OUTCOME_REPLAY",
        "PRE_DISPATCH_NO_EFFECT_REPLAY",
        "RECONCILIATION_REPLAY",
    }
)

_MATRIX = {
    (state, status): (result, reason, phase)
    for state, status, result, reason, phase in COMPATIBILITY_MATRIX
}


@dataclass(frozen=True, slots=True)
class AuditFact:
    """One normalized SUBMISSION-scoped EXECSTATE audit fact."""

    event_sequence: int
    event_kind: str
    reason: str
    submission_id: str
    target_json: str
    generation: int
    recovery_epoch: int
    new_state: str
    observed_at: datetime
    claim_id: str | None = None

    def __post_init__(self) -> None:
        sequence = _positive_integer(self.event_sequence, name="event_sequence")
        generation = _positive_integer(self.generation, name="generation")
        epoch = _positive_integer(
            self.recovery_epoch, name="recovery_epoch"
        )
        kind = _upper_token(self.event_kind, name="event_kind")
        reason = _upper_token(self.reason, name="reason")
        submission = validate_submission_id(self.submission_id)
        target_json = _target_json(self.target_json)
        state = _state_literal(self.new_state)
        moment = _aware_datetime(self.observed_at, name="observed_at")
        claim = self.claim_id
        if claim is not None:
            claim = _opaque_token(claim, name="claim_id")
        object.__setattr__(self, "event_sequence", sequence)
        object.__setattr__(self, "event_kind", kind)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "submission_id", submission)
        object.__setattr__(self, "target_json", target_json)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "recovery_epoch", epoch)
        object.__setattr__(self, "new_state", state)
        object.__setattr__(self, "observed_at", moment)
        object.__setattr__(self, "claim_id", claim)

    @classmethod
    def from_mapping(cls, document: dict[str, Any]) -> AuditFact:
        """Build one audit fact from a parsed EXECSTATE audit event."""

        if not isinstance(document, dict):
            raise TypeError("audit fact document must be a dictionary")
        if document.get("row_scope") != "SUBMISSION":
            raise ExecutionLedgerCorruptionError(
                "audit fact is not SUBMISSION-scoped"
            )
        try:
            return cls(
                event_sequence=document["event_sequence"],
                event_kind=document["event_kind"],
                reason=document["reason"],
                submission_id=document["submission_id"],
                target_json=document["target_json"],
                generation=document["generation"],
                recovery_epoch=document["recovery_epoch"],
                new_state=document["new_state"],
                observed_at=parse_canonical_instant(document["observed_at"]),
                claim_id=document.get("claim_id"),
            )
        except ExecutionLedgerCorruptionError:
            raise
        except (
            TypeError,
            ValueError,
            KeyError,
            ExecutionLedgerDeniedError,
        ) as exc:
            raise ExecutionLedgerCorruptionError(
                "stored audit fact is invalid"
            ) from exc


def _positive_integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ExecutionLedgerDeniedError(f"{name} must be positive")
    return value


def _upper_token(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if (
        not value
        or len(value) > 64
        or value.upper() != value
        or not all(char.isalnum() or char == "_" for char in value)
    ):
        raise ExecutionLedgerDeniedError(f"{name} is malformed")
    return value


def _opaque_token(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if (
        not value
        or len(value) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ExecutionLedgerDeniedError(f"{name} is malformed")
    return value


def _target_json(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("target_json must be a string")
    if not value or "\x00" in value:
        raise ExecutionLedgerDeniedError("target_json is malformed")
    return value


def _state_literal(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("new_state must be a string")
    if value not in EXECSTATE_STATE_LITERALS:
        raise ExecutionLedgerDeniedError(
            "new_state is not a contracted literal"
        )
    return value


def _aware_datetime(value: Any, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionLedgerDeniedError(f"{name} must be timezone-aware")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ExecutionLedgerDeniedError(
            f"{name} is outside the representable UTC range"
        ) from exc


def _require_observation(
    observation: Any,
) -> BrokerOrderObservation:
    if not isinstance(observation, BrokerOrderObservation):
        raise TypeError("observations must contain BrokerOrderObservation")
    if observation.observation_id is None:
        raise ExecutionLedgerCorruptionError(
            "projection requires persisted observation identities"
        )
    return observation


def _observation_sequence(observation: BrokerOrderObservation) -> int:
    observation_id = observation.observation_id
    if observation_id is None:
        raise ExecutionLedgerCorruptionError(
            "projection requires persisted observation identities"
        )
    return parse_observation_id(observation_id)


def _ordered_observations(
    observations: Any,
) -> tuple[BrokerOrderObservation, ...]:
    if not isinstance(observations, tuple):
        raise TypeError("observations must be a tuple")
    previous = 0
    for item in observations:
        checked = _require_observation(item)
        sequence = _observation_sequence(checked)
        if sequence <= previous:
            raise ExecutionLedgerCorruptionError(
                "observations are not in observation_sequence order"
            )
        previous = sequence
    return observations


def _ordered_assessments(
    assessments: Any,
) -> tuple[ReconciliationAssessment, ...]:
    if not isinstance(assessments, tuple):
        raise TypeError("assessments must be a tuple")
    for item in assessments:
        if not isinstance(item, ReconciliationAssessment):
            raise TypeError(
                "assessments must contain ReconciliationAssessment values"
            )
    return assessments


def project_phase(
    *,
    execstate_state: str,
    observations: tuple[BrokerOrderObservation, ...] = (),
    assessments: tuple[ReconciliationAssessment, ...] = (),
    claim_active: bool = False,
) -> tuple[AttemptPhase, str]:
    """Derive the read-only attempt phase for one durable state slice."""

    state = _state_literal(execstate_state)
    history = _ordered_observations(observations)
    judgements = _ordered_assessments(assessments)
    if not isinstance(claim_active, bool):
        raise TypeError("claim_active must be a bool")
    if state == "OUTCOME_UNKNOWN":
        return AttemptPhase.UNKNOWN, "UNKNOWN_LOCAL_OUTCOME_UNKNOWN"
    for observation in history:
        if observation.status is BrokerObservationStatus.UNKNOWN:
            return AttemptPhase.UNKNOWN, "UNKNOWN_RECORDED_OBSERVATION"
    for assessment in judgements:
        if assessment.result is ReconciliationResult.CONFLICT:
            return AttemptPhase.UNKNOWN, "UNKNOWN_ASSESSMENT_CONFLICT"
    for assessment in judgements:
        if assessment.result is ReconciliationResult.UNKNOWN:
            return AttemptPhase.UNKNOWN, "UNKNOWN_ASSESSMENT"
    for observation in history:
        result = _MATRIX[(state, observation.status)][0]
        if result is ReconciliationResult.CONFLICT:
            return AttemptPhase.UNKNOWN, "UNKNOWN_RECORDED_CONFLICT"
        if result is ReconciliationResult.UNKNOWN:
            return AttemptPhase.UNKNOWN, "UNKNOWN_RECORDED_OBSERVATION"
    if state in _TERMINAL_STATES:
        return AttemptPhase.FINALIZED, f"FINALIZED_{state}"
    if state == "DISPATCHING":
        if any(
            observation.status is not BrokerObservationStatus.UNKNOWN
            for observation in history
        ):
            return (
                AttemptPhase.OBSERVED,
                "OBSERVED_DISPATCHING_WITH_OBSERVATION",
            )
        return AttemptPhase.DISPATCHING, "DISPATCHING_RECORDED"
    if claim_active:
        return AttemptPhase.CLAIMED, "CLAIMED_ACTIVE_CLAIM"
    return AttemptPhase.PREPARED, "PREPARED_NO_CLAIM"


def _validate_binding(
    *,
    submission_id: str,
    target: ExecutionTargetBinding,
    generation: int,
    recovery_epoch: int,
) -> None:
    validate_submission_id(submission_id)
    if not isinstance(target, ExecutionTargetBinding):
        raise TypeError("target must be an ExecutionTargetBinding")
    if target.submission_id != submission_id:
        raise ExecutionLedgerDeniedError(
            "target submission identity differs from the attempt"
        )
    _positive_integer(generation, name="generation")
    _positive_integer(recovery_epoch, name="recovery_epoch")


def _fact_key(fact: AuditFact) -> tuple[str, str, int, int]:
    return (
        fact.submission_id,
        fact.target_json,
        fact.generation,
        fact.recovery_epoch,
    )


def _observation_key(
    observation: BrokerOrderObservation,
) -> tuple[str, str, int, int]:
    return (
        observation.submission_id,
        observation.target_json,
        observation.generation,
        observation.recovery_epoch,
    )


def _slice_state(facts: tuple[AuditFact, ...]) -> str:
    authoritative = [
        fact for fact in facts if fact.reason not in _OBSERVATIONAL_REASONS
    ]
    if not authoritative:
        raise ExecutionLedgerCorruptionError(
            "attempt slice has no state-authoritative audit fact"
        )
    return authoritative[-1].new_state


def _slice_claim(facts: tuple[AuditFact, ...]) -> tuple[bool, str | None]:
    active = False
    claim_id: str | None = None
    for fact in facts:
        if fact.event_kind == "CLAIM_ACQUIRED":
            if fact.claim_id is None:
                raise ExecutionLedgerCorruptionError(
                    "claim acquisition has no claim identity"
                )
            active = True
            claim_id = fact.claim_id
        elif fact.event_kind == "CLAIM_REVOKED":
            active = False
            claim_id = None
    return active, claim_id


def _build_view(
    *,
    submission_id: str,
    target: ExecutionTargetBinding,
    generation: int,
    recovery_epoch: int,
    execstate_state: str,
    facts: tuple[AuditFact, ...],
    observations: tuple[BrokerOrderObservation, ...],
    assessments: tuple[ReconciliationAssessment, ...],
    current_receipt_id: str | None,
) -> ExecutionAttemptView:
    state = _state_literal(execstate_state)
    if facts:
        slice_state = _slice_state(facts)
        if slice_state != state:
            raise ExecutionLedgerCorruptionError(
                "provided EXECSTATE state contradicts audit facts"
            )
        claim_active, claim_id = _slice_claim(facts)
    else:
        claim_active, claim_id = False, None
    attempt_id = derive_attempt_id(
        submission_id,
        target.model_dump_json(),
        generation,
        recovery_epoch,
    )
    for assessment in assessments:
        if assessment.attempt_id != attempt_id:
            raise ExecutionLedgerCorruptionError(
                "assessment belongs to a different attempt"
            )
    phase, reason = project_phase(
        execstate_state=state,
        observations=observations,
        assessments=assessments,
        claim_active=claim_active,
    )
    observation_ids = tuple(
        str(observation.observation_id)
        for observation in observations
    )
    return ExecutionAttemptView(
        attempt_id=attempt_id,
        submission_id=submission_id,
        target=target,
        generation=generation,
        recovery_epoch=recovery_epoch,
        phase=phase,
        phase_reason=reason,
        execstate_state=state,
        active_claim_id=claim_id if claim_active else None,
        current_receipt_id=current_receipt_id,
        observation_ids=observation_ids,
        observation_count=len(observation_ids),
    )


def project_attempt(
    *,
    submission_id: str,
    target: ExecutionTargetBinding,
    generation: int,
    recovery_epoch: int,
    execstate_state: str,
    audit_facts: tuple[AuditFact, ...] = (),
    observations: tuple[BrokerOrderObservation, ...] = (),
    assessments: tuple[ReconciliationAssessment, ...] = (),
    current_receipt_id: str | None = None,
) -> ExecutionAttemptView:
    """Reconstruct one read-only attempt view from durable facts."""

    _validate_binding(
        submission_id=submission_id,
        target=target,
        generation=generation,
        recovery_epoch=recovery_epoch,
    )
    if not isinstance(audit_facts, tuple):
        raise TypeError("audit_facts must be a tuple")
    history = _ordered_observations(observations)
    judgements = _ordered_assessments(assessments)
    if current_receipt_id is not None:
        current_receipt_id = _opaque_token(
            current_receipt_id, name="current_receipt_id"
        )
    key = (
        submission_id,
        target.model_dump_json(),
        generation,
        recovery_epoch,
    )
    for fact in audit_facts:
        if not isinstance(fact, AuditFact):
            raise TypeError("audit_facts must contain AuditFact values")
        if _fact_key(fact) != key:
            raise ExecutionLedgerCorruptionError(
                "audit fact belongs to a different attempt"
            )
    for observation in history:
        if _observation_key(observation) != key:
            raise ExecutionLedgerCorruptionError(
                "observation belongs to a different attempt"
            )
    return _build_view(
        submission_id=submission_id,
        target=target,
        generation=generation,
        recovery_epoch=recovery_epoch,
        execstate_state=execstate_state,
        facts=audit_facts,
        observations=history,
        assessments=judgements,
        current_receipt_id=current_receipt_id,
    )


def _group_facts(
    audit_facts: tuple[AuditFact, ...],
) -> list[tuple[tuple[str, str, int, int], tuple[AuditFact, ...]]]:
    if not isinstance(audit_facts, tuple):
        raise TypeError("audit_facts must be a tuple")
    previous = 0
    groups: dict[
        tuple[str, str, int, int], list[AuditFact]
    ] = {}
    for fact in audit_facts:
        if not isinstance(fact, AuditFact):
            raise TypeError("audit_facts must contain AuditFact values")
        if fact.event_sequence <= previous:
            raise ExecutionLedgerCorruptionError(
                "audit facts are not in event_sequence order"
            )
        previous = fact.event_sequence
        groups.setdefault(_fact_key(fact), []).append(fact)
    return [
        (key, tuple(items)) for key, items in groups.items()
    ]


def project_history(
    *,
    submission_id: str,
    target: ExecutionTargetBinding,
    audit_facts: tuple[AuditFact, ...],
    observations: tuple[BrokerOrderObservation, ...] = (),
    assessments: tuple[ReconciliationAssessment, ...] = (),
    current_execstate_state: str | None = None,
    current_receipt_id: str | None = None,
) -> tuple[ExecutionAttemptView, ...]:
    """Reconstruct every attempt slice in durable audit order."""

    _validate_binding(
        submission_id=submission_id,
        target=target,
        generation=1,
        recovery_epoch=1,
    )
    history = _ordered_observations(observations)
    judgements = _ordered_assessments(assessments)
    if current_receipt_id is not None:
        current_receipt_id = _opaque_token(
            current_receipt_id, name="current_receipt_id"
        )
    groups = _group_facts(audit_facts)
    expected_submission = submission_id
    expected_target = target.model_dump_json()
    for fact in audit_facts:
        if (
            fact.submission_id != expected_submission
            or fact.target_json != expected_target
        ):
            raise ExecutionLedgerCorruptionError(
                "audit fact belongs to a different submission or target"
            )
    if not groups:
        if current_execstate_state is not None or history or judgements:
            raise ExecutionLedgerCorruptionError(
                "evidence exists without a durable attempt"
            )
        return ()
    if (
        current_execstate_state is not None
        and _slice_state(groups[-1][1]) != current_execstate_state
    ):
        raise ExecutionLedgerCorruptionError(
            "current EXECSTATE state contradicts audit facts"
        )
    by_key: dict[
        tuple[str, str, int, int], list[BrokerOrderObservation]
    ] = {}
    for observation in history:
        by_key.setdefault(_observation_key(observation), []).append(
            observation
        )
    attempt_ids = {
        derive_attempt_id(
            key[0], key[1], key[2], key[3]
        ): key
        for key, _facts in groups
    }
    by_assessment: dict[str, list[ReconciliationAssessment]] = {}
    for assessment in judgements:
        if assessment.attempt_id not in attempt_ids:
            raise ExecutionLedgerCorruptionError(
                "assessment belongs to an unknown attempt"
            )
        by_assessment.setdefault(assessment.attempt_id, []).append(
            assessment
        )
    views: list[ExecutionAttemptView] = []
    known_keys = {key for key, _facts in groups}
    for observation in history:
        if _observation_key(observation) not in known_keys:
            raise ExecutionLedgerCorruptionError(
                "observation belongs to an unknown attempt"
            )
    for index, (key, facts) in enumerate(groups):
        slice_observations = tuple(by_key.get(key, ()))
        attempt_id = derive_attempt_id(key[0], key[1], key[2], key[3])
        views.append(
            _build_view(
                submission_id=key[0],
                target=target,
                generation=key[2],
                recovery_epoch=key[3],
                execstate_state=_slice_state(facts),
                facts=facts,
                observations=slice_observations,
                assessments=tuple(
                    by_assessment.get(attempt_id, ())
                ),
                current_receipt_id=(
                    current_receipt_id if index == len(groups) - 1 else None
                ),
            )
        )
    return tuple(views)
