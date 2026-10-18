from __future__ import annotations

import os
import sys
from pathlib import Path

from _helpers import (
    CLAIM_AT,
    CLAIM_EXPIRES_AT,
    OUTCOME_AT,
    PREPARE_AT,
    RECOVER_AT,
    RECOVER_RESCAN_AT,
    build_eligibility,
)

from quantforge.broker import BrokerSubmissionOutcome
from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.frontier import ExecutionTargetBinding
from quantforge.provenance import FreshRiskProvenance


class _CrashStore(SQLiteExecutionStateStore):
    def __init__(
        self,
        path: Path,
        *,
        crash_call: int,
        crash_when: str,
    ) -> None:
        self.crash_call = crash_call
        self.crash_when = crash_when
        self.commit_calls = 0
        super().__init__(path)

    def _commit(self) -> None:
        self.commit_calls += 1
        if self.commit_calls == self.crash_call and self.crash_when == "before":
            os._exit(99)
        super()._commit()
        if self.commit_calls == self.crash_call and self.crash_when == "after":
            os._exit(99)


def main() -> None:
    (
        database,
        target_json,
        provenance_json,
        crash_call,
        crash_when,
    ) = sys.argv[1:]
    target = ExecutionTargetBinding.from_json(target_json)
    provenance = FreshRiskProvenance.from_json(provenance_json)
    envelope = provenance.pre_dispatch_decision.envelope
    store = _CrashStore(
        Path(database),
        crash_call=int(crash_call),
        crash_when=crash_when,
    )
    try:
        store.recover(target, observed_at=RECOVER_AT)
        record, receipt = store.prepare(
            envelope,
            provenance,
            target,
            prepared_at=PREPARE_AT,
        )
        claim = store.claim(
            envelope.submission_id,
            target,
            owner_id="worker-1",
            token="token-1",
            acquired_at=CLAIM_AT,
            expires_at=CLAIM_EXPIRES_AT,
        )
        recovery = store.recover(
            target,
            observed_at=RECOVER_RESCAN_AT,
        )
        decision = build_eligibility(
            store,
            provenance,
            target,
            record,
            receipt,
            recovery,
            claim,
        )
        store.mark_dispatching(
            decision,
            owner_id="worker-1",
            token="token-1",
        )
        store.persist_transport_outcome(
            BrokerSubmissionOutcome(
                submission_id=envelope.submission_id,
                status="ACCEPTED",
            ),
            target,
            owner_id="worker-1",
            token="token-1",
            observed_at=OUTCOME_AT,
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
