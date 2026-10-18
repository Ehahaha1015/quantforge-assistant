from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

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
    ReconciliationReason,
    ReconciliationResult,
)
from quantforge.execution_ledger import projection as projection_module
from quantforge.execution_ledger.models import ReconciliationAssessment
from quantforge.execution_ledger.projection import (
    PHASE_REASONS,
    AuditFact,
    project_attempt,
    project_phase,
)


def _fact(
    sequence: int,
    kind: str,
    reason: str,
    state: str,
    *,
    generation: int = 1,
    epoch: int = 1,
    claim_id: str | None = None,
    observed_at: datetime = OBSERVED_AT,
) -> AuditFact:
    return AuditFact(
        event_sequence=sequence,
        event_kind=kind,
        reason=reason,
        submission_id=SUBMISSION_ID,
        target_json=TARGET_JSON,
        generation=generation,
        recovery_epoch=epoch,
        new_state=state,
        observed_at=observed_at,
        claim_id=claim_id,
    )


_PREPARE = _fact(1, "PREPARED", "NEW_PREPARE", "PREPARED")
_CLAIM = _fact(
    2,
    "CLAIM_ACQUIRED",
    "CLAIM_ACQUIRED",
    "PREPARED",
    claim_id="execstate-claim-v1-1",
)
_REVOKE = _fact(
    3,
    "CLAIM_REVOKED",
    "CLAIM_REVOKED",
    "PREPARED",
    claim_id="execstate-claim-v1-1",
)
_MARK = _fact(3, "DISPATCHING", "MARK_DISPATCHING", "DISPATCHING")
_ACCEPTED = _fact(
    4, "TRANSPORT_OUTCOME", "TRANSPORT_ACCEPTED", "ACKNOWLEDGED"
)
_REJECTED = _fact(
    4, "TRANSPORT_OUTCOME", "TRANSPORT_REJECTED", "REJECTED"
)
_NO_EFFECT = _fact(
    4,
    "PRE_DISPATCH_NO_EFFECT",
    "PRE_DISPATCH_NO_EFFECT",
    "NO_EFFECT_CONFIRMED",
)
_CONVERSION = _fact(
    4,
    "RECOVERY_UNKNOWN_CONVERSION",
    "RECOVERY_UNKNOWN_CONVERSION",
    "OUTCOME_UNKNOWN",
)


def test_phase_reason_vocabulary_is_closed() -> None:
    assert PHASE_REASONS == {
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


def test_all_six_phases_are_reachable() -> None:
    scenarios = {
        AttemptPhase.PREPARED: (
            "PREPARED",
            (_PREPARE,),
            (),
            False,
        ),
        AttemptPhase.CLAIMED: (
            "PREPARED",
            (_PREPARE, _CLAIM),
            (),
            True,
        ),
        AttemptPhase.DISPATCHING: (
            "DISPATCHING",
            (_PREPARE, _CLAIM, _MARK),
            (),
            True,
        ),
        AttemptPhase.OBSERVED: (
            "DISPATCHING",
            (_PREPARE, _CLAIM, _MARK),
            (make_observation(
                observation_id="execstate-observation-v1-1"
            ),),
            True,
        ),
        AttemptPhase.FINALIZED: (
            "ACKNOWLEDGED",
            (_PREPARE, _CLAIM, _MARK, _ACCEPTED),
            (make_observation(
                observation_id="execstate-observation-v1-1"
            ),),
            True,
        ),
        AttemptPhase.UNKNOWN: (
            "OUTCOME_UNKNOWN",
            (_PREPARE, _CLAIM, _MARK, _CONVERSION),
            (),
            True,
        ),
    }
    observed_phases = set()
    for expected_phase, (state, facts, observations, claim_active) in (
        scenarios.items()
    ):
        used_facts = tuple(
            _fact(
                fact.event_sequence,
                fact.event_kind,
                fact.reason,
                fact.new_state,
                claim_id=fact.claim_id,
            )
            for fact in facts
        )
        phase, reason = project_phase(
            execstate_state=state,
            observations=observations,
            assessments=(),
            claim_active=claim_active,
        )
        assert phase is expected_phase, (state, used_facts)
        assert reason in PHASE_REASONS
        observed_phases.add(phase)
    assert observed_phases == set(AttemptPhase)


def test_revoked_claim_returns_to_prepared() -> None:
    phase, reason = project_phase(
        execstate_state="PREPARED",
        claim_active=False,
    )
    assert phase is AttemptPhase.PREPARED
    assert reason == "PREPARED_NO_CLAIM"
    assert _REVOKE.event_kind == "CLAIM_REVOKED"


def test_unknown_observation_is_sticky() -> None:
    unknown = make_observation(
        status=BrokerObservationStatus.UNKNOWN,
        observation_id="execstate-observation-v1-1",
    )
    later = make_observation(
        observation_id="execstate-observation-v1-2"
    )
    for state in (
        "PREPARED",
        "DISPATCHING",
        "ACKNOWLEDGED",
        "REJECTED",
        "NO_EFFECT_CONFIRMED",
    ):
        phase, reason = project_phase(
            execstate_state=state,
            observations=(unknown, later),
        )
        assert phase is AttemptPhase.UNKNOWN
        assert reason == "UNKNOWN_RECORDED_OBSERVATION"


def test_local_outcome_unknown_takes_precedence() -> None:
    phase, reason = project_phase(
        execstate_state="OUTCOME_UNKNOWN",
        observations=(
            make_observation(
                observation_id="execstate-observation-v1-1"
            ),
        ),
    )
    assert phase is AttemptPhase.UNKNOWN
    assert reason == "UNKNOWN_LOCAL_OUTCOME_UNKNOWN"


def test_conflict_and_unknown_assessments_force_unknown() -> None:
    conflict = ReconciliationAssessment(
        attempt_id=GOLDEN_ATTEMPT_ID,
        observation_id=None,
        result=ReconciliationResult.CONFLICT,
        reason=ReconciliationReason.CONFLICT_NO_DISPATCH_RECORD,
        required_phase=AttemptPhase.UNKNOWN,
        recorded=False,
        existing_state="PREPARED",
    )
    unknown = ReconciliationAssessment(
        attempt_id=GOLDEN_ATTEMPT_ID,
        observation_id=None,
        result=ReconciliationResult.UNKNOWN,
        reason=ReconciliationReason.UNKNOWN_BROKER_UNKNOWN,
        required_phase=AttemptPhase.UNKNOWN,
        recorded=False,
        existing_state="DISPATCHING",
    )
    phase, reason = project_phase(
        execstate_state="DISPATCHING", assessments=(conflict,)
    )
    assert phase is AttemptPhase.UNKNOWN
    assert reason == "UNKNOWN_ASSESSMENT_CONFLICT"
    phase, reason = project_phase(
        execstate_state="DISPATCHING", assessments=(unknown,)
    )
    assert phase is AttemptPhase.UNKNOWN
    assert reason == "UNKNOWN_ASSESSMENT"


def test_recorded_observation_matrix_tension_is_unknown() -> None:
    no_effect_confirmed = _fact(
        4,
        "PRE_DISPATCH_NO_EFFECT",
        "PRE_DISPATCH_NO_EFFECT",
        "NO_EFFECT_CONFIRMED",
    )
    rejected = make_observation(
        status=BrokerObservationStatus.REJECTED,
        observation_id="execstate-observation-v1-1",
    )
    phase, reason = project_phase(
        execstate_state=no_effect_confirmed.new_state,
        observations=(rejected,),
    )
    assert phase is AttemptPhase.UNKNOWN
    assert reason == "UNKNOWN_RECORDED_OBSERVATION"


def test_project_attempt_builds_read_only_view() -> None:
    view = project_attempt(
        submission_id=SUBMISSION_ID,
        target=make_target(),
        generation=1,
        recovery_epoch=1,
        execstate_state="DISPATCHING",
        audit_facts=(_PREPARE, _CLAIM, _MARK),
        observations=(
            make_observation(
                observation_id="execstate-observation-v1-1"
            ),
        ),
        current_receipt_id="execstate-receipt-v1-2",
    )
    assert view.attempt_id == GOLDEN_ATTEMPT_ID
    assert view.phase is AttemptPhase.OBSERVED
    assert view.phase_reason == "OBSERVED_DISPATCHING_WITH_OBSERVATION"
    assert view.execstate_state == "DISPATCHING"
    assert view.active_claim_id == "execstate-claim-v1-1"
    assert view.current_receipt_id == "execstate-receipt-v1-2"
    assert view.observation_ids == ("execstate-observation-v1-1",)
    assert view.observation_count == 1


def test_project_attempt_state_mismatch_is_corruption() -> None:
    with pytest.raises(ExecutionLedgerCorruptionError):
        project_attempt(
            submission_id=SUBMISSION_ID,
            target=make_target(),
            generation=1,
            recovery_epoch=1,
            execstate_state="ACKNOWLEDGED",
            audit_facts=(_PREPARE, _CLAIM, _MARK),
        )


def test_audit_fact_from_mapping() -> None:
    document = {
        "event_sequence": 1,
        "row_scope": "SUBMISSION",
        "submission_id": SUBMISSION_ID,
        "target_json": TARGET_JSON,
        "generation": 1,
        "recovery_epoch": 1,
        "event_kind": "PREPARED",
        "reason": "NEW_PREPARE",
        "new_state": "PREPARED",
        "claim_id": None,
        "observed_at": "2030-01-01T12:02:01.000000Z",
    }
    fact = AuditFact.from_mapping(document)
    assert fact.event_sequence == 1
    assert fact.new_state == "PREPARED"
    assert fact.observed_at == OBSERVED_AT
    with pytest.raises(ExecutionLedgerCorruptionError):
        AuditFact.from_mapping({**document, "row_scope": "RECOVERY_BOUNDARY"})
    with pytest.raises(ExecutionLedgerCorruptionError):
        AuditFact.from_mapping({**document, "observed_at": "not-a-time"})


def test_projection_module_is_pure() -> None:
    source = Path(projection_module.__file__).read_text(encoding="utf-8")
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
    assert roots <= {
        "__future__",
        "dataclasses",
        "datetime",
        "quantforge",
        "typing",
    }
    assert relative <= {
        "_canonical",
        "errors",
        "identity",
        "models",
        "reconciliation",
    }
    for token in (
        "sqlite3",
        "socket",
        "urllib",
        "subprocess",
        "os.environ",
        "getenv",
        "datetime.now",
        "time.time",
        "random",
        "uuid4",
    ):
        assert token not in source, token


def test_project_attempt_rejects_bad_generation() -> None:
    with pytest.raises(ExecutionLedgerDeniedError):
        project_attempt(
            submission_id=SUBMISSION_ID,
            target=make_target(),
            generation=0,
            recovery_epoch=1,
            execstate_state="PREPARED",
            audit_facts=(_PREPARE,),
        )
