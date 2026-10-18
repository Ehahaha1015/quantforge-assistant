from __future__ import annotations

import pytest
from _support import GOLDEN_ATTEMPT_ID, SUBMISSION_ID, TARGET_JSON

from quantforge.execution_ledger import (
    ExecutionLedgerCorruptionError,
    ExecutionLedgerDeniedError,
)
from quantforge.execution_ledger.identity import (
    derive_attempt_id,
    observation_id_for,
    parse_observation_id,
    validate_attempt_id,
    validate_observation_id,
    verify_attempt_binding,
)


def test_golden_attempt_id_vector() -> None:
    assert (
        derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 1, 1)
        == GOLDEN_ATTEMPT_ID
    )


def test_attempt_id_is_deterministic_and_tuple_sensitive() -> None:
    first = derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 1, 1)
    second = derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 1, 1)
    assert first == second == GOLDEN_ATTEMPT_ID
    assert derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 2, 1) != first
    assert derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 1, 2) != first
    assert derive_attempt_id(SUBMISSION_ID, TARGET_JSON + " ", 1, 1) != first


def test_attempt_id_derivation_validation() -> None:
    with pytest.raises(TypeError):
        derive_attempt_id(None, TARGET_JSON, 1, 1)  # type: ignore[arg-type]
    with pytest.raises(ExecutionLedgerDeniedError):
        derive_attempt_id("not-a-submission", TARGET_JSON, 1, 1)
    with pytest.raises(TypeError):
        derive_attempt_id(SUBMISSION_ID, TARGET_JSON, True, 1)  # type: ignore[arg-type]
    with pytest.raises(ExecutionLedgerDeniedError):
        derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 0, 1)
    with pytest.raises(ExecutionLedgerDeniedError):
        derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 1, -1)
    with pytest.raises(TypeError):
        derive_attempt_id(SUBMISSION_ID, TARGET_JSON, 1, "1")  # type: ignore[arg-type]


def test_binding_verification_accepts_exact_tuple() -> None:
    assert (
        verify_attempt_binding(
            GOLDEN_ATTEMPT_ID, SUBMISSION_ID, TARGET_JSON, 1, 1
        )
        is None
    )


@pytest.mark.parametrize(
    ("submission_id", "target_json", "generation", "recovery_epoch"),
    [
        (SUBMISSION_ID, TARGET_JSON, 2, 1),
        (SUBMISSION_ID, TARGET_JSON, 1, 2),
        (SUBMISSION_ID, TARGET_JSON + " ", 1, 1),
        ("broker1-" + "b" * 32, TARGET_JSON, 1, 1),
    ],
)
def test_binding_mismatch_fails_closed_as_corruption(
    submission_id: str,
    target_json: str,
    generation: int,
    recovery_epoch: int,
) -> None:
    with pytest.raises(ExecutionLedgerCorruptionError):
        verify_attempt_binding(
            GOLDEN_ATTEMPT_ID,
            submission_id,
            target_json,
            generation,
            recovery_epoch,
        )


def test_no_alternate_identifier_is_produced_on_mismatch() -> None:
    try:
        verify_attempt_binding(
            GOLDEN_ATTEMPT_ID, SUBMISSION_ID, TARGET_JSON, 2, 1
        )
    except ExecutionLedgerCorruptionError as exc:
        assert "collision" in str(exc) or "mismatch" in str(exc)
    else:  # pragma: no cover
        pytest.fail("mismatch did not fail closed")


def test_attempt_id_validation() -> None:
    assert validate_attempt_id(GOLDEN_ATTEMPT_ID) == GOLDEN_ATTEMPT_ID
    with pytest.raises(TypeError):
        validate_attempt_id(None)  # type: ignore[arg-type]
    with pytest.raises(ExecutionLedgerDeniedError):
        validate_attempt_id("execstate-attempt-v1-" + "Z" * 32)
    with pytest.raises(ExecutionLedgerDeniedError):
        validate_attempt_id("execstate-attempt-v1-" + "a" * 31)


def test_observation_id_allocation_and_parsing() -> None:
    assert observation_id_for(1) == "execstate-observation-v1-1"
    assert observation_id_for(123456) == "execstate-observation-v1-123456"
    assert parse_observation_id("execstate-observation-v1-7") == 7
    assert (
        validate_observation_id("execstate-observation-v1-7")
        == "execstate-observation-v1-7"
    )
    with pytest.raises(TypeError):
        observation_id_for(True)  # type: ignore[arg-type]
    with pytest.raises(ExecutionLedgerDeniedError):
        observation_id_for(0)
    with pytest.raises(TypeError):
        parse_observation_id(None)  # type: ignore[arg-type]
    for malformed in (
        "execstate-observation-v1-0",
        "execstate-observation-v1-x",
        "execstate-observation-v1-",
        "other-1",
    ):
        with pytest.raises(ExecutionLedgerCorruptionError):
            parse_observation_id(malformed)
