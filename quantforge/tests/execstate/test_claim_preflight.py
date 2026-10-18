from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _helpers import (
    CLAIM_EXPIRES_AT,
    RECOVER_RESCAN_AT,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)

from quantforge.execstate import ExecutionStateDeniedError


def test_stale_claim_is_denied_before_competing_for_the_write_lock(tmp_path, monkeypatch) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    stale = make_store(tmp_path)
    current = None
    try:
        recover_store(stale, target)
        prepare_store(stale, provenance, target)
        current = make_store(tmp_path)
        current.recover(target, observed_at=RECOVER_RESCAN_AT)

        def refuse_write_lock() -> None:
            pytest.fail("An already stale session must not acquire the competing writer lock")

        monkeypatch.setattr(stale, "_begin_immediate", refuse_write_lock)
        with pytest.raises(ExecutionStateDeniedError, match="store recovery epoch is stale"):
            stale.claim(
                provenance.pre_dispatch_decision.envelope.submission_id,
                target,
                owner_id="stale-worker",
                token="stale-token",
                acquired_at=datetime(2030, 1, 1, 12, 1, 58, tzinfo=UTC),
                expires_at=CLAIM_EXPIRES_AT,
            )
    finally:
        stale.close()
        if current is not None:
            current.close()
