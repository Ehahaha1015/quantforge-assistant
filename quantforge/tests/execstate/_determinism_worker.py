from __future__ import annotations

import json
import sys
from pathlib import Path

from _helpers import (
    RECOVER_RESCAN_AT,
    build_eligibility,
    claim_store,
    make_provenance,
    make_target,
    prepare_store,
    recover_store,
)

from quantforge.execstate import SQLiteExecutionStateStore


def main() -> None:
    database = Path(sys.argv[1])
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = SQLiteExecutionStateStore(database)
    try:
        recover_store(store, target)
        record, receipt = prepare_store(store, provenance, target)
        claim = claim_store(store, envelope.submission_id, target)
        recovery = recover_store(store, target, observed_at=RECOVER_RESCAN_AT)
        decision = build_eligibility(
            store, provenance, target, record, receipt, recovery, claim
        )
        print(
            json.dumps(
                {
                    "record": record.model_dump_json(),
                    "receipt": receipt.model_dump_json(),
                    "claim": claim.model_dump_json(),
                    "recovery": recovery.model_dump_json(),
                    "decision": decision.model_dump_json(),
                },
                sort_keys=True,
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
