"""Fresh-process determinism probe for the Slice 8 stress matrix.

Builds one complete ledger history through the real public APIs and prints a
canonical projection of identities, phases, and stored evidence bytes. The
output must be byte-identical across PYTHONHASHSEED values and fresh
processes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ledger_support import (
    RECOVER_AT,
    advance_to_dispatching,
    make_provenance,
    make_store,
    make_target,
    recover_store,
)
from _support import OBSERVED_AT, make_observation

from quantforge.execution_ledger import (
    BrokerObservationStatus,
    ReconciliationConflict,
)
from quantforge.execution_ledger.ledger import DurableExecutionLedger


def _canonical(view) -> dict[str, object]:
    return {
        "attempt_id": view.attempt_id,
        "generation": view.generation,
        "recovery_epoch": view.recovery_epoch,
        "execstate_state": view.execstate_state,
        "phase": view.phase.value,
        "phase_reason": view.phase_reason,
        "active_claim_id": view.active_claim_id,
        "current_receipt_id": view.current_receipt_id,
        "observation_ids": list(view.observation_ids),
        "observation_count": view.observation_count,
    }


def _raw_rows(sidecar: Path) -> list[dict[str, object]]:
    connection = sqlite3.connect(str(sidecar))
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT observation_sequence, observation_id, attempt_id, "
                "observation_kind, status, broker_reference, observed_at, "
                "recorded_at, payload_json FROM observations "
                "ORDER BY observation_sequence"
            )
        ]
    finally:
        connection.close()


def main() -> None:
    directory = Path(sys.argv[1])
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    store = make_store(directory / "state")
    sidecar = directory / "ledger.sqlite3"
    ledger = None
    try:
        recover_store(store, target, observed_at=RECOVER_AT)
        advance_to_dispatching(store, provenance, target)
        ledger = DurableExecutionLedger(store, sidecar)
        record, _receipt = store.read(target.submission_id, target)
        first = ledger.record_observation(
            make_observation(
                submission_id=target.submission_id,
                target=target,
                generation=record.generation,
                recovery_epoch=record.recovery_epoch,
                broker_reference="broker-ref-a",
                observed_at=OBSERVED_AT,
            )
        )
        second = ledger.record_observation(
            make_observation(
                submission_id=target.submission_id,
                target=target,
                generation=record.generation,
                recovery_epoch=record.recovery_epoch,
                broker_reference="broker-ref-b",
                observed_at=OBSERVED_AT,
            )
        )
        conflict: dict[str, object] | None = None
        try:
            ledger.record_observation(
                make_observation(
                    submission_id=target.submission_id,
                    target=target,
                    generation=record.generation,
                    recovery_epoch=record.recovery_epoch,
                    status=BrokerObservationStatus.REJECTED,
                    broker_reference="broker-ref-c",
                    observed_at=OBSERVED_AT,
                )
            )
        except ReconciliationConflict as exc:
            conflict = {
                "attempt_id": exc.assessment.attempt_id,
                "result": exc.assessment.result.value,
                "reason": exc.assessment.reason.value,
                "required_phase": exc.assessment.required_phase.value,
                "existing_state": exc.assessment.existing_state,
            }
        history = ledger.read_attempt_history(
            target.submission_id, target
        )
        current = ledger.read_attempt(target.submission_id, target)
        observations = ledger.read_observations(first.attempt_id)
        payload = {
            "observation_ids": [
                first.observation_id,
                second.observation_id,
            ],
            "attempt_id": first.attempt_id,
            "attempt_ids": [view.attempt_id for view in history],
            "history": [_canonical(view) for view in history],
            "current": _canonical(current),
            "recorded": [
                {
                    "observation_id": item.observation_id,
                    "status": item.status.value,
                    "broker_reference": item.broker_reference,
                    "observed_at": item.observed_at.isoformat(),
                }
                for item in observations
            ],
            "conflict": conflict,
            "raw_rows": _raw_rows(sidecar),
        }
        print(json.dumps(payload, sort_keys=True))
    finally:
        if ledger is not None:
            ledger.close()
        store.close()


if __name__ == "__main__":
    main()
