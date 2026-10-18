from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from _support import (
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
)
from quantforge.execution_ledger.projection import (
    AuditFact,
    project_history,
)


def _fact(
    sequence: int,
    kind: str,
    reason: str,
    state: str,
    *,
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
        generation=1,
        recovery_epoch=epoch,
        new_state=state,
        observed_at=observed_at,
        claim_id=claim_id,
    )


_EPOCH_ONE = (
    _fact(1, "PREPARED", "NEW_PREPARE", "PREPARED"),
    _fact(
        2,
        "CLAIM_ACQUIRED",
        "CLAIM_ACQUIRED",
        "PREPARED",
        claim_id="execstate-claim-v1-1",
    ),
    _fact(3, "DISPATCHING", "MARK_DISPATCHING", "DISPATCHING"),
)

_EPOCH_TWO = (
    _fact(4, "RECOVERY_EPOCH_REBIND", "RECOVERY_EPOCH_REBIND", "DISPATCHING", epoch=2),
    _fact(
        5,
        "RECOVERY_UNKNOWN_CONVERSION",
        "RECOVERY_UNKNOWN_CONVERSION",
        "OUTCOME_UNKNOWN",
        epoch=2,
    ),
)


def test_single_epoch_history_is_deterministic() -> None:
    first = project_history(
        submission_id=SUBMISSION_ID,
        target=make_target(),
        audit_facts=_EPOCH_ONE,
        current_execstate_state="DISPATCHING",
    )
    second = project_history(
        submission_id=SUBMISSION_ID,
        target=make_target(),
        audit_facts=_EPOCH_ONE,
        current_execstate_state="DISPATCHING",
    )
    assert first == second
    assert len(first) == 1
    assert first[0].phase is AttemptPhase.DISPATCHING
    assert first[0].phase_reason == "DISPATCHING_RECORDED"


def test_epoch_rebind_produces_separate_attempts() -> None:
    observations = (
        make_observation(
            observation_id="execstate-observation-v1-1"
        ),
        make_observation(
            status=BrokerObservationStatus.UNKNOWN,
            recovery_epoch=2,
            observation_id="execstate-observation-v1-2",
        ),
    )
    views = project_history(
        submission_id=SUBMISSION_ID,
        target=make_target(),
        audit_facts=_EPOCH_ONE + _EPOCH_TWO,
        observations=observations,
        current_execstate_state="OUTCOME_UNKNOWN",
        current_receipt_id="execstate-receipt-v1-3",
    )
    assert len(views) == 2
    assert views[0].recovery_epoch == 1
    assert views[1].recovery_epoch == 2
    assert views[0].attempt_id != views[1].attempt_id
    assert views[0].phase is AttemptPhase.OBSERVED
    assert views[0].observation_ids == ("execstate-observation-v1-1",)
    assert views[1].phase is AttemptPhase.UNKNOWN
    assert views[1].observation_ids == ("execstate-observation-v1-2",)
    assert views[0].current_receipt_id is None
    assert views[1].current_receipt_id == "execstate-receipt-v1-3"


def test_history_order_uses_sequences_not_timestamps() -> None:
    later = OBSERVED_AT + timedelta(minutes=5)
    earlier = OBSERVED_AT - timedelta(minutes=5)
    facts = (
        _fact(1, "PREPARED", "NEW_PREPARE", "PREPARED", observed_at=later),
        _fact(2, "DISPATCHING", "MARK_DISPATCHING", "DISPATCHING", observed_at=earlier),
    )
    observations = (
        make_observation(
            observed_at=later,
            observation_id="execstate-observation-v1-1",
        ),
        make_observation(
            observed_at=earlier,
            broker_reference="broker-ref-2",
            observation_id="execstate-observation-v1-2",
        ),
    )
    views = project_history(
        submission_id=SUBMISSION_ID,
        target=make_target(),
        audit_facts=facts,
        observations=observations,
        current_execstate_state="DISPATCHING",
    )
    assert views[0].observation_ids == (
        "execstate-observation-v1-1",
        "execstate-observation-v1-2",
    )
    assert views[0].phase is AttemptPhase.OBSERVED


def test_observation_with_unknown_attempt_is_corruption() -> None:
    with pytest.raises(ExecutionLedgerCorruptionError):
        project_history(
            submission_id=SUBMISSION_ID,
            target=make_target(),
            audit_facts=_EPOCH_ONE,
            observations=(
                make_observation(
                    recovery_epoch=3,
                    observation_id="execstate-observation-v1-1",
                ),
            ),
            current_execstate_state="DISPATCHING",
        )


def test_current_state_mismatch_is_corruption() -> None:
    with pytest.raises(ExecutionLedgerCorruptionError):
        project_history(
            submission_id=SUBMISSION_ID,
            target=make_target(),
            audit_facts=_EPOCH_ONE,
            current_execstate_state="ACKNOWLEDGED",
        )


def test_empty_history_returns_empty_without_current_state() -> None:
    assert (
        project_history(
            submission_id=SUBMISSION_ID,
            target=make_target(),
            audit_facts=(),
        )
        == ()
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        project_history(
            submission_id=SUBMISSION_ID,
            target=make_target(),
            audit_facts=(),
            current_execstate_state="PREPARED",
        )
