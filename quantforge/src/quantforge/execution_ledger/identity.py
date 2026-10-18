"""Deterministic identity derivation for the execution ledger."""

from __future__ import annotations

import hashlib
import re

from ._canonical import canonical_json
from .errors import ExecutionLedgerCorruptionError, ExecutionLedgerDeniedError

ATTEMPT_ID_PREFIX = "execstate-attempt-v1-"
OBSERVATION_ID_PREFIX = "execstate-observation-v1-"
ASSESSMENT_ID_PREFIX = "execstate-assessment-v1-"

_SUBMISSION_PATTERN = re.compile(r"^broker1-[0-9a-f]{32}$")
_ATTEMPT_BODY = re.compile(r"^[0-9a-f]{32}$")
_ASSESSMENT_BODY = re.compile(r"^[0-9a-f]{32}$")


def validate_submission_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("submission_id must be a string")
    if _SUBMISSION_PATTERN.fullmatch(value) is None:
        raise ExecutionLedgerDeniedError("submission_id is malformed")
    return value


def _positive_integer(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ExecutionLedgerDeniedError(f"{name} must be positive")
    return value


def derive_attempt_id(
    submission_id: str,
    target_json: str,
    generation: int,
    recovery_epoch: int,
) -> str:
    """Derive the deterministic identity of one attempt tuple."""

    submission = validate_submission_id(submission_id)
    if not isinstance(target_json, str):
        raise TypeError("target_json must be a string")
    if not target_json or "\x00" in target_json:
        raise ExecutionLedgerDeniedError("target_json is malformed")
    generation_value = _positive_integer(generation, name="generation")
    epoch_value = _positive_integer(recovery_epoch, name="recovery_epoch")
    payload = {
        "submission_id": submission,
        "target_json": target_json,
        "generation": generation_value,
        "recovery_epoch": epoch_value,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return ATTEMPT_ID_PREFIX + digest[:32]


def verify_attempt_binding(
    attempt_id: str,
    submission_id: str,
    target_json: str,
    generation: int,
    recovery_epoch: int,
) -> None:
    """Fail closed on any derived-identity collision or binding mismatch."""

    expected = derive_attempt_id(
        submission_id, target_json, generation, recovery_epoch
    )
    if validate_attempt_id(attempt_id) != expected:
        raise ExecutionLedgerCorruptionError(
            "execution attempt identity collision or binding mismatch"
        )


def validate_attempt_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("attempt_id must be a string")
    if not value.startswith(ATTEMPT_ID_PREFIX):
        raise ExecutionLedgerDeniedError("attempt_id is malformed")
    if _ATTEMPT_BODY.fullmatch(value[len(ATTEMPT_ID_PREFIX) :]) is None:
        raise ExecutionLedgerDeniedError("attempt_id is malformed")
    return value


def observation_id_for(sequence: int) -> str:
    value = _positive_integer(sequence, name="observation_sequence")
    return f"{OBSERVATION_ID_PREFIX}{value}"


def validate_observation_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("observation_id must be a string")
    if not value.startswith(OBSERVATION_ID_PREFIX):
        raise ExecutionLedgerDeniedError("observation_id is malformed")
    body = value[len(OBSERVATION_ID_PREFIX) :]
    if not body.isdigit() or int(body) < 1:
        raise ExecutionLedgerDeniedError("observation_id is malformed")
    return value


def _contracted_token(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or "\x00" in value:
        raise ExecutionLedgerDeniedError(f"{name} is malformed")
    return value


def derive_assessment_id(
    *,
    attempt_id: str,
    observation_id: str | None,
    result: str,
    reason: str,
    required_phase: str,
    recorded: bool,
    existing_state: str,
) -> str:
    """Derive the deterministic identity of one reconciliation assessment.

    Contract 7.1 requires an assessment identity that is either
    deterministic or operational. This derivation is deterministic over the
    assessment's contracted components, so equivalent assessments share one
    identity and any bound component change produces a new identity, with no
    clock, counter, entropy source, or mutable global state.
    """

    attempt = validate_attempt_id(attempt_id)
    observation = (
        None
        if observation_id is None
        else validate_observation_id(observation_id)
    )
    if not isinstance(recorded, bool):
        raise TypeError("recorded must be a bool")
    payload = {
        "attempt_id": attempt,
        "existing_state": _contracted_token(
            existing_state, name="existing_state"
        ),
        "observation_id": observation,
        "reason": _contracted_token(reason, name="reason"),
        "recorded": recorded,
        "required_phase": _contracted_token(
            required_phase, name="required_phase"
        ),
        "result": _contracted_token(result, name="result"),
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return ASSESSMENT_ID_PREFIX + digest[:32]


def validate_assessment_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("assessment_id must be a string")
    if not value.startswith(ASSESSMENT_ID_PREFIX):
        raise ExecutionLedgerDeniedError("assessment_id is malformed")
    body = value[len(ASSESSMENT_ID_PREFIX) :]
    if _ASSESSMENT_BODY.fullmatch(body) is None:
        raise ExecutionLedgerDeniedError("assessment_id is malformed")
    return value


def parse_observation_id(value: str) -> int:
    """Parse an observation identity from persisted ledger bytes."""

    if not isinstance(value, str):
        raise TypeError("observation_id must be a string")
    if not value.startswith(OBSERVATION_ID_PREFIX):
        raise ExecutionLedgerCorruptionError(
            "stored observation_id is malformed"
        )
    body = value[len(OBSERVATION_ID_PREFIX) :]
    if not body.isdigit() or int(body) < 1:
        raise ExecutionLedgerCorruptionError(
            "stored observation_id is malformed"
        )
    return int(body)
