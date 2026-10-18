"""Reviewer A CH-D: contract 7.1 assessment identity.

Section 7.1 requires `assessment_id` as a deterministic or operational
identifier. It is derived here deterministically from the assessment's
contracted components, so equivalent assessments share one identity, any
bound component change produces a new identity, and no clock, counter, or
mutable global state participates.
"""

from __future__ import annotations

import re

import pytest
from _support import SUBMISSION_ID, TARGET_JSON, make_observation

from quantforge.execution_ledger import (
    AttemptPhase,
    BrokerObservationStatus,
    ExecutionLedgerDeniedError,
    ReconciliationAssessment,
    ReconciliationReason,
    ReconciliationResult,
)
from quantforge.execution_ledger.identity import (
    ASSESSMENT_ID_PREFIX,
    derive_assessment_id,
    derive_attempt_id,
    validate_assessment_id,
)
from quantforge.execution_ledger.reconciliation import (
    assess,
    recorded_assessment,
)

_GOLDEN_ATTEMPT = derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 1, 1)


def _compatible():
    return assess(
        execstate_state="DISPATCHING", observation=make_observation()
    )


def _derivation(**overrides: object) -> str:
    payload = {
        "attempt_id": _GOLDEN_ATTEMPT,
        "observation_id": None,
        "result": "COMPATIBLE",
        "reason": "COMPATIBLE_ACCEPTED_ON_DISPATCHING",
        "required_phase": "OBSERVED",
        "recorded": False,
        "existing_state": "DISPATCHING",
    }
    payload.update(overrides)
    return derive_assessment_id(**payload)


def test_assessment_id_is_present_deterministic_and_formatted() -> None:
    first = _compatible()
    second = _compatible()
    assert first.assessment_id == second.assessment_id
    assert first.assessment_id.startswith(ASSESSMENT_ID_PREFIX)
    body = first.assessment_id[len(ASSESSMENT_ID_PREFIX) :]
    assert re.fullmatch(r"[0-9a-f]{32}", body) is not None
    assert validate_assessment_id(first.assessment_id) == (
        first.assessment_id
    )
    # Equal assessments remain equal values with one shared identity.
    assert first == second


def test_assessment_id_changes_with_each_bound_component() -> None:
    base = _compatible()
    variants = {
        "recorded": recorded_assessment(
            base, "execstate-observation-v1-1"
        ),
        "existing_state": assess(
            execstate_state="OUTCOME_UNKNOWN",
            observation=make_observation(),
        ),
        "status": assess(
            execstate_state="DISPATCHING",
            observation=make_observation(
                status=BrokerObservationStatus.REJECTED,
                broker_reference="broker-ref-2",
            ),
        ),
        "attempt": assess(
            execstate_state="DISPATCHING",
            observation=make_observation(generation=2),
        ),
    }
    identifiers = {base.assessment_id}
    for variant in variants.values():
        identifiers.add(variant.assessment_id)
    assert len(identifiers) == len(variants) + 1

    # Every remaining contracted component is bound exactly once.
    baseline = _derivation()
    sensitivity = {
        "attempt_id": _derivation(attempt_id=_GOLDEN_ATTEMPT[:-1] + "0"),
        "observation_id": _derivation(
            observation_id="execstate-observation-v1-1"
        ),
        "result": _derivation(result="UNKNOWN"),
        "reason": _derivation(reason="UNKNOWN_BROKER_UNKNOWN"),
        "required_phase": _derivation(required_phase="UNKNOWN"),
        "recorded": _derivation(recorded=True),
        "existing_state": _derivation(existing_state="OUTCOME_UNKNOWN"),
    }
    assert len(set(sensitivity.values())) == len(sensitivity)
    assert baseline not in sensitivity.values()
    assert _derivation() == baseline


def test_assessment_id_is_derived_and_never_caller_supplied() -> None:
    with pytest.raises(TypeError):
        ReconciliationAssessment(
            attempt_id=_GOLDEN_ATTEMPT,
            observation_id=None,
            result=ReconciliationResult.COMPATIBLE,
            reason=ReconciliationReason.COMPATIBLE_ACCEPTED_ON_DISPATCHING,
            required_phase=AttemptPhase.OBSERVED,
            recorded=False,
            existing_state="DISPATCHING",
            assessment_id=ASSESSMENT_ID_PREFIX + "0" * 32,
        )


def test_assessment_id_validation_rejects_malformed_values() -> None:
    for malformed in (
        "",
        "execstate-attempt-v1-" + "0" * 32,
        ASSESSMENT_ID_PREFIX + "0" * 31,
        ASSESSMENT_ID_PREFIX + "Z" * 32,
    ):
        with pytest.raises(ExecutionLedgerDeniedError):
            validate_assessment_id(malformed)
