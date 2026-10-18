from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from _support import (
    GOLDEN_ATTEMPT_ID,
    OBSERVED_AT,
    SUBMISSION_ID,
    TARGET_JSON,
    make_observation,
    make_target,
)

from quantforge.execution_ledger import (
    AttemptPhase,
    BrokerObservationStatus,
    ExecutionLedgerCorruptionError,
    ExecutionLedgerDeniedError,
    ReconciliationAssessment,
    ReconciliationReason,
    ReconciliationResult,
)
from quantforge.execution_ledger.models import (
    EXECSTATE_STATE_LITERALS,
    OBSERVATION_KINDS,
    observation_envelope_json,
    observation_from_envelope_json,
    observation_semantic_key,
)

_ENVELOPE_KEYS = {
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


def test_enum_literals() -> None:
    assert {phase.value for phase in AttemptPhase} == {
        "PREPARED",
        "CLAIMED",
        "DISPATCHING",
        "UNKNOWN",
        "OBSERVED",
        "FINALIZED",
    }
    assert {status.value for status in BrokerObservationStatus} == {
        "ACCEPTED",
        "REJECTED",
        "UNKNOWN",
    }
    assert {result.value for result in ReconciliationResult} == {
        "COMPATIBLE",
        "UNKNOWN",
        "CONFLICT",
    }
    assert {reason.value for reason in ReconciliationReason} == {
        "COMPATIBLE_ACCEPTED_ON_DISPATCHING",
        "COMPATIBLE_REJECTED_ON_DISPATCHING",
        "COMPATIBLE_OBSERVATION_ON_UNKNOWN",
        "COMPATIBLE_FINAL_ACCEPTED",
        "COMPATIBLE_FINAL_REJECTED",
        "UNKNOWN_BROKER_UNKNOWN",
        "UNKNOWN_LOCAL_AMBIGUOUS",
        "CONFLICT_NO_DISPATCH_RECORD",
        "CONFLICT_TERMINAL_ACCEPTED_VS_REJECTED",
        "CONFLICT_TERMINAL_REJECTED_VS_ACCEPTED",
        "CONFLICT_NO_EFFECT_VS_ACCEPTED",
        "CONFLICT_CONTRADICTORY_OBSERVATION",
        "CONFLICT_STALE_ATTEMPT_TUPLE",
    }
    assert EXECSTATE_STATE_LITERALS == {
        "PREPARED",
        "DISPATCHING",
        "ACKNOWLEDGED",
        "REJECTED",
        "OUTCOME_UNKNOWN",
        "NO_EFFECT_CONFIRMED",
    }
    assert OBSERVATION_KINDS == {
        "BROKER_OBSERVATION",
        "OBSERVATION_REPLAY",
    }


def test_observation_valid_and_frozen() -> None:
    observation = make_observation()
    assert observation.submission_id == SUBMISSION_ID
    assert observation.adapter_id == "adapter-x"
    assert observation.adapter_version == "1"
    assert observation.target_json == TARGET_JSON
    assert observation.observed_at == OBSERVED_AT
    assert observation.observation_id is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        observation.generation = 2  # type: ignore[misc]


def test_observation_normalizes_offset_datetime_to_utc() -> None:
    moment = datetime(
        2030, 1, 1, 20, 2, 1, tzinfo=timezone(timedelta(hours=8))
    )
    observation = make_observation(observed_at=moment)
    assert observation.observed_at == OBSERVED_AT
    assert observation.observed_at.tzinfo is UTC


def test_observation_validation_matrix() -> None:
    with pytest.raises(TypeError):
        make_observation(
            submission_id=None,  # type: ignore[arg-type]
            target=make_target(),
        )
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(
            submission_id="not-a-submission", target=make_target()
        )
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(target=make_target("broker1-" + "b" * 32))
    with pytest.raises(TypeError):
        make_observation(generation=True)  # type: ignore[arg-type]
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(generation=0)
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(recovery_epoch=0)
    with pytest.raises(TypeError):
        make_observation(status="ACCEPTED")  # type: ignore[arg-type]
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(broker_reference="")
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(broker_reference="a" * 257)
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(broker_reference="bad\nreference")
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(broker_message="a" * 2049)
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(
            observed_at=datetime(  # noqa: DTZ001 - intentional naive input
                2030, 1, 1, 12, 2, 1
            )
        )
    with pytest.raises(TypeError):
        make_observation(observed_at="2030-01-01T12:02:01.000000Z")  # type: ignore[arg-type]
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(observation_id="other-1")


def test_envelope_round_trip_is_canonical() -> None:
    observation = make_observation()
    text = observation_envelope_json(
        observation, observation_id="execstate-observation-v1-1"
    )
    parsed = json.loads(text)
    assert set(parsed) == _ENVELOPE_KEYS
    assert text == json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    restored = observation_from_envelope_json(text)
    assert restored == dataclasses.replace(
        observation, observation_id="execstate-observation-v1-1"
    )
    assert restored.observation_id == "execstate-observation-v1-1"


def test_envelope_rejects_unknown_fields_and_non_canonical_bytes() -> None:
    observation = make_observation()
    text = observation_envelope_json(
        observation, observation_id="execstate-observation-v1-1"
    )
    parsed = json.loads(text)
    with pytest.raises(ExecutionLedgerCorruptionError):
        observation_from_envelope_json(
            json.dumps(
                {**parsed, "raw_response": "forbidden"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    with pytest.raises(ExecutionLedgerCorruptionError):
        observation_from_envelope_json(
            json.dumps(parsed, sort_keys=True, indent=2)
        )
    with pytest.raises(TypeError):
        observation_from_envelope_json(123)  # type: ignore[arg-type]


def test_envelope_rejects_oversized_persisted_payload() -> None:
    observation = make_observation()
    text = observation_envelope_json(
        observation, observation_id="execstate-observation-v1-1"
    )
    parsed = json.loads(text)
    parsed["broker_message"] = "a" * 20000
    with pytest.raises(ExecutionLedgerCorruptionError):
        observation_from_envelope_json(
            json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        )


def test_semantic_key_excludes_bookkeeping_fields() -> None:
    first = make_observation(observation_id="execstate-observation-v1-1")
    second = make_observation(observation_id="execstate-observation-v1-2")
    assert observation_semantic_key(first) == observation_semantic_key(second)
    third = make_observation(
        status=BrokerObservationStatus.REJECTED,
        observation_id="execstate-observation-v1-3",
    )
    assert observation_semantic_key(first) != observation_semantic_key(third)


def test_attempt_view_validation() -> None:
    view = _attempt_view()
    assert view.phase is AttemptPhase.OBSERVED
    assert view.observation_count == 1
    with pytest.raises(TypeError):
        _attempt_view(phase="OBSERVED")  # type: ignore[arg-type]
    with pytest.raises(ExecutionLedgerDeniedError):
        _attempt_view(execstate_state="UNKNOWN")
    with pytest.raises(ExecutionLedgerDeniedError):
        _attempt_view(observation_count=2)


def test_assessment_validation() -> None:
    assessment = ReconciliationAssessment(
        attempt_id=GOLDEN_ATTEMPT_ID,
        observation_id="execstate-observation-v1-1",
        result=ReconciliationResult.COMPATIBLE,
        reason=ReconciliationReason.COMPATIBLE_ACCEPTED_ON_DISPATCHING,
        required_phase=AttemptPhase.OBSERVED,
        recorded=True,
        existing_state="DISPATCHING",
    )
    assert assessment.recorded is True
    with pytest.raises(ExecutionLedgerDeniedError):
        ReconciliationAssessment(
            attempt_id=GOLDEN_ATTEMPT_ID,
            observation_id="execstate-observation-v1-1",
            result=ReconciliationResult.CONFLICT,
            reason=ReconciliationReason.CONFLICT_NO_DISPATCH_RECORD,
            required_phase=AttemptPhase.OBSERVED,
            recorded=True,
            existing_state="PREPARED",
        )
    with pytest.raises(ExecutionLedgerDeniedError):
        ReconciliationAssessment(
            attempt_id=GOLDEN_ATTEMPT_ID,
            observation_id=None,
            result=ReconciliationResult.COMPATIBLE,
            reason=ReconciliationReason.COMPATIBLE_ACCEPTED_ON_DISPATCHING,
            required_phase=AttemptPhase.OBSERVED,
            recorded=True,
            existing_state="DISPATCHING",
        )


def _attempt_view(
    *,
    phase: object = AttemptPhase.OBSERVED,
    execstate_state: str = "DISPATCHING",
    observation_count: int = 1,
) -> object:
    from quantforge.execution_ledger import ExecutionAttemptView

    return ExecutionAttemptView(
        attempt_id=GOLDEN_ATTEMPT_ID,
        submission_id=SUBMISSION_ID,
        target=make_target(),
        generation=1,
        recovery_epoch=1,
        phase=phase,  # type: ignore[arg-type]
        phase_reason="OBSERVATION_RECORDED",
        execstate_state=execstate_state,
        active_claim_id="execstate-claim-v1-1",
        current_receipt_id="execstate-receipt-v1-2",
        observation_ids=("execstate-observation-v1-1",),
        observation_count=observation_count,
    )
