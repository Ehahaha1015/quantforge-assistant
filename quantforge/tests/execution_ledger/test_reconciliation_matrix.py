from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _support import make_observation

from quantforge.execution_ledger import (
    AttemptPhase,
    BrokerObservationStatus,
    ExecutionLedgerDeniedError,
    ReconciliationReason,
    ReconciliationResult,
)
from quantforge.execution_ledger import reconciliation as reconciliation_module
from quantforge.execution_ledger.models import EXECSTATE_STATE_LITERALS
from quantforge.execution_ledger.reconciliation import (
    COMPATIBILITY_MATRIX,
    assess,
)

_EXPECTED_MATRIX = {
    ("PREPARED", BrokerObservationStatus.ACCEPTED): (
        ReconciliationResult.CONFLICT,
        ReconciliationReason.CONFLICT_NO_DISPATCH_RECORD,
        AttemptPhase.UNKNOWN,
    ),
    ("PREPARED", BrokerObservationStatus.REJECTED): (
        ReconciliationResult.CONFLICT,
        ReconciliationReason.CONFLICT_NO_DISPATCH_RECORD,
        AttemptPhase.UNKNOWN,
    ),
    ("PREPARED", BrokerObservationStatus.UNKNOWN): (
        ReconciliationResult.UNKNOWN,
        ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    ("DISPATCHING", BrokerObservationStatus.ACCEPTED): (
        ReconciliationResult.COMPATIBLE,
        ReconciliationReason.COMPATIBLE_ACCEPTED_ON_DISPATCHING,
        AttemptPhase.OBSERVED,
    ),
    ("DISPATCHING", BrokerObservationStatus.REJECTED): (
        ReconciliationResult.COMPATIBLE,
        ReconciliationReason.COMPATIBLE_REJECTED_ON_DISPATCHING,
        AttemptPhase.OBSERVED,
    ),
    ("DISPATCHING", BrokerObservationStatus.UNKNOWN): (
        ReconciliationResult.UNKNOWN,
        ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    ("ACKNOWLEDGED", BrokerObservationStatus.ACCEPTED): (
        ReconciliationResult.COMPATIBLE,
        ReconciliationReason.COMPATIBLE_FINAL_ACCEPTED,
        AttemptPhase.FINALIZED,
    ),
    ("ACKNOWLEDGED", BrokerObservationStatus.REJECTED): (
        ReconciliationResult.CONFLICT,
        ReconciliationReason.CONFLICT_TERMINAL_ACCEPTED_VS_REJECTED,
        AttemptPhase.UNKNOWN,
    ),
    ("ACKNOWLEDGED", BrokerObservationStatus.UNKNOWN): (
        ReconciliationResult.UNKNOWN,
        ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    ("REJECTED", BrokerObservationStatus.ACCEPTED): (
        ReconciliationResult.CONFLICT,
        ReconciliationReason.CONFLICT_TERMINAL_REJECTED_VS_ACCEPTED,
        AttemptPhase.UNKNOWN,
    ),
    ("REJECTED", BrokerObservationStatus.REJECTED): (
        ReconciliationResult.COMPATIBLE,
        ReconciliationReason.COMPATIBLE_FINAL_REJECTED,
        AttemptPhase.FINALIZED,
    ),
    ("REJECTED", BrokerObservationStatus.UNKNOWN): (
        ReconciliationResult.UNKNOWN,
        ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    ("OUTCOME_UNKNOWN", BrokerObservationStatus.ACCEPTED): (
        ReconciliationResult.COMPATIBLE,
        ReconciliationReason.COMPATIBLE_OBSERVATION_ON_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    ("OUTCOME_UNKNOWN", BrokerObservationStatus.REJECTED): (
        ReconciliationResult.COMPATIBLE,
        ReconciliationReason.COMPATIBLE_OBSERVATION_ON_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    ("OUTCOME_UNKNOWN", BrokerObservationStatus.UNKNOWN): (
        ReconciliationResult.COMPATIBLE,
        ReconciliationReason.COMPATIBLE_OBSERVATION_ON_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
    ("NO_EFFECT_CONFIRMED", BrokerObservationStatus.ACCEPTED): (
        ReconciliationResult.CONFLICT,
        ReconciliationReason.CONFLICT_NO_EFFECT_VS_ACCEPTED,
        AttemptPhase.UNKNOWN,
    ),
    ("NO_EFFECT_CONFIRMED", BrokerObservationStatus.REJECTED): (
        ReconciliationResult.UNKNOWN,
        ReconciliationReason.UNKNOWN_LOCAL_AMBIGUOUS,
        AttemptPhase.UNKNOWN,
    ),
    ("NO_EFFECT_CONFIRMED", BrokerObservationStatus.UNKNOWN): (
        ReconciliationResult.UNKNOWN,
        ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        AttemptPhase.UNKNOWN,
    ),
}


def test_matrix_is_total_and_frozen() -> None:
    assert len(COMPATIBILITY_MATRIX) == 18
    actual = {
        (state, status): (result, reason, phase)
        for state, status, result, reason, phase in COMPATIBILITY_MATRIX
    }
    assert actual == _EXPECTED_MATRIX
    assert {state for state, _ in actual} == set(EXECSTATE_STATE_LITERALS)
    assert {status for _, status in actual} == set(BrokerObservationStatus)


@pytest.mark.parametrize(
    ("state", "status"),
    sorted(_EXPECTED_MATRIX, key=lambda item: (item[0], item[1].value)),
)
def test_assess_matches_matrix(
    state: str, status: BrokerObservationStatus
) -> None:
    expected_result, expected_reason, expected_phase = _EXPECTED_MATRIX[
        (state, status)
    ]
    assessment = assess(
        execstate_state=state,
        observation=make_observation(status=status),
    )
    assert assessment.result is expected_result
    assert assessment.reason is expected_reason
    assert assessment.required_phase is expected_phase
    assert assessment.recorded is False
    assert assessment.observation_id is None
    assert assessment.existing_state == state
    assert assessment.attempt_id.startswith("execstate-attempt-v1-")


def test_assess_is_deterministic() -> None:
    for state in sorted(EXECSTATE_STATE_LITERALS):
        for status in BrokerObservationStatus:
            first = assess(
                execstate_state=state,
                observation=make_observation(status=status),
            )
            second = assess(
                execstate_state=state,
                observation=make_observation(status=status),
            )
            assert first == second
            assert first is not second


def test_assess_rejects_unknown_state_and_wrong_types() -> None:
    with pytest.raises(ExecutionLedgerDeniedError):
        assess(execstate_state="UNKNOWN", observation=make_observation())
    with pytest.raises(TypeError):
        assess(execstate_state=1, observation=make_observation())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        assess(execstate_state="PREPARED", observation=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        assess(
            execstate_state="PREPARED",
            observation=make_observation(),
            recorded=[],  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        assess(
            execstate_state="PREPARED",
            observation=make_observation(),
            recorded=(object(),),  # type: ignore[arg-type]
        )


def test_reconciliation_module_is_pure() -> None:
    source = Path(reconciliation_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 0:
                roots.add(node.module.split(".", 1)[0])
            else:
                relative.add(node.module)
    assert roots <= {"__future__", "dataclasses", "typing"}
    assert relative <= {"errors", "identity", "models"}
    for token in (
        "sqlite3",
        "socket",
        "urllib",
        "subprocess",
        "os.environ",
        "getenv",
        "datetime",
        "time.time",
        "random",
        "uuid4",
    ):
        assert token not in source, token
