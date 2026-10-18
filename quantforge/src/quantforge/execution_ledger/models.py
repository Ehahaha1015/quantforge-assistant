"""Immutable value objects for the broker-neutral execution ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from quantforge.frontier import ExecutionTargetBinding

from ._canonical import (
    canonical_instant,
    canonical_json,
    opaque,
    parse_canonical_instant,
)
from .errors import ExecutionLedgerCorruptionError, ExecutionLedgerDeniedError
from .identity import (
    derive_assessment_id,
    validate_attempt_id,
    validate_observation_id,
    validate_submission_id,
)

OBSERVATION_ENVELOPE_MAX_BYTES = 16 * 1024
BROKER_REFERENCE_MAX_CODEPOINTS = 256
BROKER_MESSAGE_MAX_CODEPOINTS = 2048

EXECSTATE_STATE_LITERALS = frozenset(
    {
        "PREPARED",
        "DISPATCHING",
        "ACKNOWLEDGED",
        "REJECTED",
        "OUTCOME_UNKNOWN",
        "NO_EFFECT_CONFIRMED",
    }
)

OBSERVATION_KINDS = frozenset(
    {"BROKER_OBSERVATION", "OBSERVATION_REPLAY"}
)

_ENVELOPE_KEYS = frozenset(
    {
        "adapter_id",
        "adapter_version",
        "broker_message",
        "broker_reference",
        "generation",
        "observation_id",
        "observation_kind",
        "observed_at",
        "recovery_epoch",
        "status",
        "submission_id",
        "target_json",
    }
)


class AttemptPhase(StrEnum):
    """Read-only projection label; never dispatch authority."""

    PREPARED = "PREPARED"
    CLAIMED = "CLAIMED"
    DISPATCHING = "DISPATCHING"
    UNKNOWN = "UNKNOWN"
    OBSERVED = "OBSERVED"
    FINALIZED = "FINALIZED"


class BrokerObservationStatus(StrEnum):
    """Broker-neutral observation status."""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ReconciliationResult(StrEnum):
    """Informational outcome of comparing local truth with an observation."""

    COMPATIBLE = "COMPATIBLE"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class ReconciliationReason(StrEnum):
    """Closed vocabulary explaining one reconciliation assessment."""

    COMPATIBLE_ACCEPTED_ON_DISPATCHING = "COMPATIBLE_ACCEPTED_ON_DISPATCHING"
    COMPATIBLE_REJECTED_ON_DISPATCHING = "COMPATIBLE_REJECTED_ON_DISPATCHING"
    COMPATIBLE_OBSERVATION_ON_UNKNOWN = "COMPATIBLE_OBSERVATION_ON_UNKNOWN"
    COMPATIBLE_FINAL_ACCEPTED = "COMPATIBLE_FINAL_ACCEPTED"
    COMPATIBLE_FINAL_REJECTED = "COMPATIBLE_FINAL_REJECTED"
    UNKNOWN_BROKER_UNKNOWN = "UNKNOWN_BROKER_UNKNOWN"
    UNKNOWN_LOCAL_AMBIGUOUS = "UNKNOWN_LOCAL_AMBIGUOUS"
    CONFLICT_NO_DISPATCH_RECORD = "CONFLICT_NO_DISPATCH_RECORD"
    CONFLICT_TERMINAL_ACCEPTED_VS_REJECTED = (
        "CONFLICT_TERMINAL_ACCEPTED_VS_REJECTED"
    )
    CONFLICT_TERMINAL_REJECTED_VS_ACCEPTED = (
        "CONFLICT_TERMINAL_REJECTED_VS_ACCEPTED"
    )
    CONFLICT_NO_EFFECT_VS_ACCEPTED = "CONFLICT_NO_EFFECT_VS_ACCEPTED"
    CONFLICT_CONTRADICTORY_OBSERVATION = "CONFLICT_CONTRADICTORY_OBSERVATION"
    CONFLICT_STALE_ATTEMPT_TUPLE = "CONFLICT_STALE_ATTEMPT_TUPLE"


def _positive_integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ExecutionLedgerDeniedError(f"{name} must be positive")
    return value


def _required_string(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ExecutionLedgerDeniedError(f"{name} is malformed")
    return value


def _optional_opaque(
    value: str | None, *, name: str, max_codepoints: int
) -> str | None:
    if value is None:
        return None
    return opaque(value, name=name, max_codepoints=max_codepoints)


def _optional_observation_id(value: str | None) -> str | None:
    if value is None:
        return None
    return validate_observation_id(value)


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


@dataclass(frozen=True, slots=True)
class BrokerOrderObservation:
    """Canonical normalized external observation envelope."""

    submission_id: str
    target: ExecutionTargetBinding
    generation: int
    recovery_epoch: int
    status: BrokerObservationStatus
    broker_reference: str | None
    broker_message: str | None
    observed_at: datetime
    observation_id: str | None = None

    def __post_init__(self) -> None:
        submission = validate_submission_id(self.submission_id)
        if not isinstance(self.target, ExecutionTargetBinding):
            raise TypeError("target must be an ExecutionTargetBinding")
        if self.target.submission_id != submission:
            raise ExecutionLedgerDeniedError(
                "target submission identity differs from the observation"
            )
        generation = _positive_integer(self.generation, name="generation")
        epoch = _positive_integer(
            self.recovery_epoch, name="recovery_epoch"
        )
        if not isinstance(self.status, BrokerObservationStatus):
            raise TypeError("status must be a BrokerObservationStatus")
        reference = _optional_opaque(
            self.broker_reference,
            name="broker_reference",
            max_codepoints=BROKER_REFERENCE_MAX_CODEPOINTS,
        )
        message = _optional_opaque(
            self.broker_message,
            name="broker_message",
            max_codepoints=BROKER_MESSAGE_MAX_CODEPOINTS,
        )
        moment = _aware_datetime(self.observed_at, name="observed_at")
        observation_id = _optional_observation_id(self.observation_id)
        object.__setattr__(self, "submission_id", submission)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "recovery_epoch", epoch)
        object.__setattr__(self, "broker_reference", reference)
        object.__setattr__(self, "broker_message", message)
        object.__setattr__(self, "observed_at", moment)
        object.__setattr__(self, "observation_id", observation_id)

    @property
    def adapter_id(self) -> str:
        return self.target.adapter_id

    @property
    def adapter_version(self) -> str:
        return self.target.adapter_version

    @property
    def target_json(self) -> str:
        return self.target.model_dump_json()


@dataclass(frozen=True, slots=True)
class ExecutionAttemptView:
    """Read-only projection of one execution attempt."""

    attempt_id: str
    submission_id: str
    target: ExecutionTargetBinding
    generation: int
    recovery_epoch: int
    phase: AttemptPhase
    phase_reason: str
    execstate_state: str
    active_claim_id: str | None
    current_receipt_id: str | None
    observation_ids: tuple[str, ...]
    observation_count: int

    def __post_init__(self) -> None:
        attempt_id = validate_attempt_id(self.attempt_id)
        submission = validate_submission_id(self.submission_id)
        if not isinstance(self.target, ExecutionTargetBinding):
            raise TypeError("target must be an ExecutionTargetBinding")
        if self.target.submission_id != submission:
            raise ExecutionLedgerDeniedError(
                "target submission identity differs from the attempt"
            )
        generation = _positive_integer(self.generation, name="generation")
        epoch = _positive_integer(
            self.recovery_epoch, name="recovery_epoch"
        )
        if not isinstance(self.phase, AttemptPhase):
            raise TypeError("phase must be an AttemptPhase")
        reason = _required_string(self.phase_reason, name="phase_reason")
        state = _required_string(
            self.execstate_state, name="execstate_state"
        )
        if state not in EXECSTATE_STATE_LITERALS:
            raise ExecutionLedgerDeniedError(
                "execstate_state is not a contracted literal"
            )
        claim = _optional_opaque(
            self.active_claim_id, name="active_claim_id", max_codepoints=256
        )
        receipt = _optional_opaque(
            self.current_receipt_id,
            name="current_receipt_id",
            max_codepoints=256,
        )
        if not isinstance(self.observation_ids, tuple):
            raise TypeError("observation_ids must be a tuple")
        observation_ids = tuple(
            validate_observation_id(value) for value in self.observation_ids
        )
        count = self.observation_count
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError("observation_count must be an integer")
        if count < 0 or count != len(observation_ids):
            raise ExecutionLedgerDeniedError(
                "observation_count does not match observation_ids"
            )
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "submission_id", submission)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "recovery_epoch", epoch)
        object.__setattr__(self, "phase_reason", reason)
        object.__setattr__(self, "execstate_state", state)
        object.__setattr__(self, "active_claim_id", claim)
        object.__setattr__(self, "current_receipt_id", receipt)
        object.__setattr__(self, "observation_ids", observation_ids)


@dataclass(frozen=True, slots=True)
class ReconciliationAssessment:
    """Informational judgement about one external observation."""

    attempt_id: str
    observation_id: str | None
    result: ReconciliationResult
    reason: ReconciliationReason
    required_phase: AttemptPhase
    recorded: bool
    existing_state: str
    assessment_id: str = field(init=False)

    def __post_init__(self) -> None:
        attempt_id = validate_attempt_id(self.attempt_id)
        observation_id = _optional_observation_id(self.observation_id)
        if not isinstance(self.result, ReconciliationResult):
            raise TypeError("result must be a ReconciliationResult")
        if not isinstance(self.reason, ReconciliationReason):
            raise TypeError("reason must be a ReconciliationReason")
        if not isinstance(self.required_phase, AttemptPhase):
            raise TypeError("required_phase must be an AttemptPhase")
        if not isinstance(self.recorded, bool):
            raise TypeError("recorded must be a bool")
        state = _required_string(
            self.existing_state, name="existing_state"
        )
        if state not in EXECSTATE_STATE_LITERALS:
            raise ExecutionLedgerDeniedError(
                "existing_state is not a contracted literal"
            )
        if self.result is ReconciliationResult.CONFLICT:
            if self.recorded or observation_id is not None:
                raise ExecutionLedgerDeniedError(
                    "a conflict records no observation"
                )
            if self.required_phase is not AttemptPhase.UNKNOWN:
                raise ExecutionLedgerDeniedError(
                    "a conflict requires the UNKNOWN phase"
                )
        elif self.recorded and observation_id is None:
            raise ExecutionLedgerDeniedError(
                "a recorded assessment requires an observation_id"
            )
        elif not self.recorded and observation_id is not None:
            raise ExecutionLedgerDeniedError(
                "an unrecorded assessment cannot carry an observation_id"
            )
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "existing_state", state)
        object.__setattr__(
            self,
            "assessment_id",
            derive_assessment_id(
                attempt_id=attempt_id,
                observation_id=observation_id,
                result=self.result.value,
                reason=self.reason.value,
                required_phase=self.required_phase.value,
                recorded=self.recorded,
                existing_state=state,
            ),
        )


def _envelope_mapping(
    observation: BrokerOrderObservation,
    *,
    observation_id: str,
    observation_kind: str,
) -> dict[str, Any]:
    return {
        "adapter_id": observation.adapter_id,
        "adapter_version": observation.adapter_version,
        "broker_message": observation.broker_message,
        "broker_reference": observation.broker_reference,
        "generation": observation.generation,
        "observation_id": observation_id,
        "observation_kind": observation_kind,
        "observed_at": canonical_instant(observation.observed_at),
        "recovery_epoch": observation.recovery_epoch,
        "status": observation.status.value,
        "submission_id": observation.submission_id,
        "target_json": observation.target_json,
    }


def observation_envelope_json(
    observation: BrokerOrderObservation,
    *,
    observation_id: str,
    observation_kind: str = "BROKER_OBSERVATION",
) -> str:
    """Serialize one observation into the canonical persisted envelope."""

    if not isinstance(observation, BrokerOrderObservation):
        raise TypeError("observation must be a BrokerOrderObservation")
    stored_id = validate_observation_id(observation_id)
    if observation.observation_id not in (None, stored_id):
        raise ExecutionLedgerDeniedError(
            "observation carries a different observation_id"
        )
    if observation_kind not in OBSERVATION_KINDS:
        raise ExecutionLedgerDeniedError(
            "observation_kind is not contracted"
        )
    payload = canonical_json(
        _envelope_mapping(
            observation,
            observation_id=stored_id,
            observation_kind=observation_kind,
        )
    )
    if len(payload.encode("utf-8")) > OBSERVATION_ENVELOPE_MAX_BYTES:
        raise ExecutionLedgerDeniedError(
            "observation envelope exceeds the contracted bound"
        )
    return payload


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(text: str) -> Any:
    raise ValueError(f"forbidden JSON constant: {text}")


def observation_from_envelope_json(
    payload_json: str,
) -> BrokerOrderObservation:
    """Parse one persisted canonical observation envelope."""

    observation, _observation_kind = _parse_envelope_json(payload_json)
    return observation


def observation_envelope_fields(
    payload_json: str,
) -> tuple[BrokerOrderObservation, str]:
    """Parse one persisted envelope and its stored observation_kind."""

    return _parse_envelope_json(payload_json)


def _parse_envelope_json(
    payload_json: str,
) -> tuple[BrokerOrderObservation, str]:
    if not isinstance(payload_json, str):
        raise TypeError("payload_json must be a string")
    if len(payload_json.encode("utf-8")) > OBSERVATION_ENVELOPE_MAX_BYTES:
        raise ExecutionLedgerCorruptionError(
            "stored observation envelope exceeds the contracted bound"
        )
    try:
        parsed = json.loads(
            payload_json,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionLedgerCorruptionError(
            "stored observation envelope is malformed"
        ) from exc
    if not isinstance(parsed, dict) or set(parsed) != _ENVELOPE_KEYS:
        raise ExecutionLedgerCorruptionError(
            "stored observation envelope has unexpected fields"
        )
    if canonical_json(parsed) != payload_json:
        raise ExecutionLedgerCorruptionError(
            "stored observation envelope is not canonical"
        )
    try:
        observation_id = validate_observation_id(parsed["observation_id"])
        observation_kind = parsed["observation_kind"]
        if observation_kind not in OBSERVATION_KINDS:
            raise ExecutionLedgerDeniedError(
                "observation_kind is not contracted"
            )
        target_json = parsed["target_json"]
        if not isinstance(target_json, str):
            raise ExecutionLedgerDeniedError("target_json is malformed")
        target = ExecutionTargetBinding.from_json(target_json)
        if target.model_dump_json() != target_json:
            raise ExecutionLedgerDeniedError("target_json is not canonical")
        submission_id = validate_submission_id(parsed["submission_id"])
        generation = _positive_integer(parsed["generation"], name="generation")
        epoch = _positive_integer(
            parsed["recovery_epoch"], name="recovery_epoch"
        )
        status_value = parsed["status"]
        if not isinstance(status_value, str):
            raise ExecutionLedgerDeniedError("status is malformed")
        status = BrokerObservationStatus(status_value)
        reference = _optional_opaque(
            parsed["broker_reference"],
            name="broker_reference",
            max_codepoints=BROKER_REFERENCE_MAX_CODEPOINTS,
        )
        message = _optional_opaque(
            parsed["broker_message"],
            name="broker_message",
            max_codepoints=BROKER_MESSAGE_MAX_CODEPOINTS,
        )
        observed_at = parse_canonical_instant(parsed["observed_at"])
    except ExecutionLedgerCorruptionError:
        raise
    except (TypeError, ValueError, ExecutionLedgerDeniedError) as exc:
        raise ExecutionLedgerCorruptionError(
            "stored observation envelope is invalid"
        ) from exc
    try:
        return (
            BrokerOrderObservation(
                submission_id=submission_id,
                target=target,
                generation=generation,
                recovery_epoch=epoch,
                status=status,
                broker_reference=reference,
                broker_message=message,
                observed_at=observed_at,
                observation_id=observation_id,
            ),
            observation_kind,
        )
    except (TypeError, ValueError, ExecutionLedgerDeniedError) as exc:
        raise ExecutionLedgerCorruptionError(
            "stored observation envelope is invalid"
        ) from exc


def observation_semantic_key(observation: BrokerOrderObservation) -> str:
    """Return the replay/contradiction identity of one observation."""

    if not isinstance(observation, BrokerOrderObservation):
        raise TypeError("observation must be a BrokerOrderObservation")
    return canonical_json(
        {
            "adapter_id": observation.adapter_id,
            "adapter_version": observation.adapter_version,
            "broker_message": observation.broker_message,
            "broker_reference": observation.broker_reference,
            "generation": observation.generation,
            "observed_at": canonical_instant(observation.observed_at),
            "recovery_epoch": observation.recovery_epoch,
            "status": observation.status.value,
            "submission_id": observation.submission_id,
            "target_json": observation.target_json,
        }
    )
