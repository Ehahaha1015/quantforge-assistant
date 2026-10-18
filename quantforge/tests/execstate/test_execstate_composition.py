from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _helpers import (
    OUTCOME_AT,
    build_eligibility,
    prepare_claim_recover,
)

from quantforge.broker import BrokerSubmissionOutcome
from quantforge.execstate import ExecutionStateConflictError


def test_aa_real_execstate_evidence_composes_with_frozen_frontier(
    tmp_path,
) -> None:
    (
        store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
        envelope,
    ) = prepare_claim_recover(tmp_path)
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    assert decision.eligible is True
    assert decision.reasons == ()
    result = store.mark_dispatching(
        decision, owner_id="worker-1", token="token-1"
    )
    assert result.state == "DISPATCHING"
    persisted, persisted_receipt = store.read(envelope.submission_id, target)
    assert persisted.state == "DISPATCHING"
    assert persisted_receipt.recovery_epoch == persisted.recovery_epoch
    store.close()


def test_reconciliation_from_unknown_and_exact_replay(tmp_path) -> None:
    (
        store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
        envelope,
    ) = prepare_claim_recover(tmp_path)
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    store.persist_transport_outcome(
        BrokerSubmissionOutcome(
            submission_id=envelope.submission_id,
            status="OUTCOME_UNKNOWN",
        ),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    reconciled = store.apply_reconciliation(
        envelope.submission_id,
        target,
        status="ACCEPTED",
        evidence_reference="reconciliation-1",
        observed_at=datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC),
    )
    assert reconciled.state == "ACKNOWLEDGED"
    audit_before = store.read_audit(envelope.submission_id, target)
    replay = store.apply_reconciliation(
        envelope.submission_id,
        target,
        status="ACCEPTED",
        evidence_reference="reconciliation-1",
        observed_at=datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC),
    )
    assert replay.state == "ACKNOWLEDGED"
    audit_after = store.read_audit(envelope.submission_id, target)
    assert len(audit_after) == len(audit_before) + 1
    assert json.loads(audit_after[-1])["reason"] == "RECONCILIATION_REPLAY"
    with pytest.raises(ExecutionStateConflictError):
        store.apply_reconciliation(
            envelope.submission_id,
            target,
            status="REJECTED",
            evidence_reference="reconciliation-2",
            observed_at=datetime(2030, 1, 1, 12, 2, 3, tzinfo=UTC),
        )
    store.close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("REJECTED", "REJECTED"),
        ("NO_EFFECT_CONFIRMED", "NO_EFFECT_CONFIRMED"),
    ],
)
def test_reconciliation_terminal_results(tmp_path, status: str, expected: str) -> None:
    (
        store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
        envelope,
    ) = prepare_claim_recover(tmp_path)
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, claim
    )
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    store.persist_transport_outcome(
        BrokerSubmissionOutcome(
            submission_id=envelope.submission_id,
            status="OUTCOME_UNKNOWN",
        ),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    result = store.apply_reconciliation(
        envelope.submission_id,
        target,
        status=status,
        evidence_reference=f"reference-{status.lower()}",
        observed_at=datetime(2030, 1, 1, 12, 2, 2, tzinfo=UTC),
    )
    assert result.state == expected
    store.close()


def test_ac_determinism_across_processes_and_hash_seeds(tmp_path) -> None:
    worker = Path(__file__).with_name("_determinism_worker.py")
    outputs: list[str] = []
    for index, seed in enumerate(("1", "2")):
        run_dir = tmp_path / f"run-{index}"
        run_dir.mkdir()
        database = run_dir / "execstate.sqlite3"
        completed = subprocess.run(
            [sys.executable, str(worker), str(database)],
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        lines = [
            line for line in completed.stdout.splitlines() if line.startswith("{")
        ]
        assert len(lines) == 1
        outputs.append(lines[0])
    assert outputs[0] == outputs[1]
