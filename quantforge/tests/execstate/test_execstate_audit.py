from __future__ import annotations

import inspect
import json
import random
import socket
import subprocess
import time
import urllib.request
import uuid
from datetime import datetime

import pytest
from _helpers import (
    OUTCOME_AT,
    RECOVER_RESCAN_AT,
    build_eligibility,
    claim_store,
    make_provenance,
    make_store,
    make_target,
    prepare_claim_recover,
    prepare_store,
    recover_store,
)

from quantforge.broker import BrokerPreDispatchError, BrokerSubmissionOutcome
from quantforge.execstate import (
    ExecutionStateArgumentError,
    SQLiteExecutionStateStore,
)
from quantforge.execstate import store as store_module

_REAL_DATETIME = datetime


class _TripwireDateTimeMeta(type):
    def __instancecheck__(self, instance: object) -> bool:
        return isinstance(instance, _REAL_DATETIME)


class _TripwireDateTime(_REAL_DATETIME, metaclass=_TripwireDateTimeMeta):
    @classmethod
    def now(cls, tz=None):  # type: ignore[no-untyped-def]
        raise AssertionError("implicit datetime.now() was invoked")

    @classmethod
    def utcnow(cls):  # type: ignore[no-untyped-def]
        raise AssertionError("implicit datetime.utcnow() was invoked")


def _forbidden_capability(*_args, **_kwargs):  # type: ignore[no-untyped-def]
    raise AssertionError("forbidden runtime capability was invoked")


def _event_json(store: SQLiteExecutionStateStore, index: int = -1) -> dict:
    row = store._connection.execute(
        "SELECT event_json FROM audit_events ORDER BY event_sequence"
    ).fetchall()[index]
    return json.loads(row["event_json"])


def test_ah_audit_history_is_consistent(tmp_path) -> None:
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
            status="ACCEPTED",
        ),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    events = [
        json.loads(value) for value in store.read_audit(envelope.submission_id, target)
    ]
    assert events
    assert all(
        (event["event_kind"], event["reason"])
        in {
            ("PREPARED", "NEW_PREPARE"),
            ("CLAIM_ACQUIRED", "CLAIM_ACQUIRED"),
            ("DISPATCHING", "MARK_DISPATCHING"),
            ("TRANSPORT_OUTCOME", "TRANSPORT_ACCEPTED"),
        }
        for event in events
    )
    current, _ = store.read(envelope.submission_id, target)
    assert events[-1]["new_state"] == current.state
    store.close()


def test_ap_exact_replays_add_one_audit_row_and_no_authoritative_change(
    tmp_path,
) -> None:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)

    prepare_store(store, provenance, target)
    prepare_store(store, provenance, target)
    assert store._connection.execute(
        "SELECT COUNT(*) FROM commit_receipts"
    ).fetchone()[0] == 1
    assert store._connection.execute(
        "SELECT COUNT(*) FROM submissions"
    ).fetchone()[0] == 1

    first_claim = claim_store(store, envelope.submission_id, target)
    claim_store(store, envelope.submission_id, target)
    assert store._connection.execute(
        "SELECT COUNT(*) FROM claims"
    ).fetchone()[0] == 1

    recovery = recover_store(store, target, observed_at=RECOVER_RESCAN_AT)
    record, receipt = store.read(envelope.submission_id, target)
    decision = build_eligibility(
        store, provenance, target, record, receipt, recovery, first_claim
    )
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    receipts_after_dispatching = store._connection.execute(
        "SELECT COUNT(*) FROM commit_receipts"
    ).fetchone()[0]
    store.mark_dispatching(decision, owner_id="worker-1", token="token-1")
    assert store._connection.execute(
        "SELECT COUNT(*) FROM commit_receipts"
    ).fetchone()[0] == receipts_after_dispatching

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
    before_payloads = store._connection.execute(
        "SELECT COUNT(*) FROM operational_payloads"
    ).fetchone()[0]
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
    assert store._connection.execute(
        "SELECT COUNT(*) FROM operational_payloads"
    ).fetchone()[0] == before_payloads
    before_record, _ = store.read(envelope.submission_id, target)
    assert before_record.state == "ACKNOWLEDGED"
    after_record, _ = store.read(envelope.submission_id, target)
    assert after_record == before_record
    reasons = [
        json.loads(value)["reason"]
        for value in store.read_audit(envelope.submission_id, target)
    ]
    assert "DUPLICATE_PREPARE_REPLAY" in reasons
    assert "CLAIM_REPLAY" in reasons
    assert "DISPATCHING_REPLAY" in reasons
    assert "TRANSPORT_OUTCOME_REPLAY" in reasons
    store.close()


def test_ap_invalid_event_reason_pair_is_rejected(tmp_path) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    prepare_store(store, provenance, target)
    with pytest.raises(ExecutionStateArgumentError):
        store._write_audit(
            submission_id=provenance.pre_dispatch_decision.envelope.submission_id,
            target_json=target.model_dump_json(),
            prior_state="PREPARED",
            new_state="PREPARED",
            generation=1,
            recovery_epoch=1,
            event_kind="PREPARED",
            reason="UNCONTRACTED_REASON",
            observed_at=OUTCOME_AT,
        )
    store.close()


def test_aj_pre_dispatch_no_effect_proof(tmp_path) -> None:
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
    result = store.persist_pre_dispatch_no_effect(
        envelope.submission_id,
        target,
        BrokerPreDispatchError("broker-pre-dispatch-no-effect"),
        owner_id="worker-1",
        token="token-1",
        observed_at=OUTCOME_AT,
    )
    assert result.state == "NO_EFFECT_CONFIRMED"
    events = [
        json.loads(value) for value in store.read_audit(envelope.submission_id, target)
    ]
    assert events[-1]["event_kind"] == "PRE_DISPATCH_NO_EFFECT"
    assert events[-1]["reason"] == "PRE_DISPATCH_NO_EFFECT"
    with pytest.raises(TypeError):
        store.persist_pre_dispatch_no_effect(
            envelope.submission_id,
            target,
            RuntimeError("timeout"),  # type: ignore[arg-type]
            owner_id="worker-1",
            token="token-1",
            observed_at=OUTCOME_AT,
        )
    store.close()


def test_af_no_retry_or_resubmit_api() -> None:
    forbidden = {
        "retry",
        "resubmit",
        "retry_unknown",
        "force_release_and_send",
        "place_order",
        "submit_order",
        "send",
        "cancel",
        "replace",
    }
    assert not (forbidden & set(dir(SQLiteExecutionStateStore)))


def test_ag_no_fill_or_portfolio_mutation() -> None:
    source = inspect.getsource(store_module)
    assert "Fill" not in source
    assert "apply_fill" not in source
    assert "execute_paper_order" not in source


def test_ae_no_network_broker_or_credentials() -> None:
    source = inspect.getsource(store_module)
    for forbidden in (
        "socket",
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "ccxt",
        "alpaca",
        "binance",
        "coinbase",
        "keyring",
    ):
        assert forbidden not in source


def test_ad_no_implicit_clock_or_randomness() -> None:
    source = inspect.getsource(store_module)
    for forbidden in (
        "datetime.now",
        "datetime.utcnow",
        "time.time",
        "CURRENT_TIMESTAMP",
        "datetime('now')",
        "unixepoch",
        "julianday",
        "mtime",
        "random",
        "uuid",
        "getpid",
    ):
        assert forbidden not in source


def test_ad_runtime_tripwires_cover_real_store_operations(tmp_path, monkeypatch) -> None:
    provenance = make_provenance()
    target = make_target(provenance.pre_dispatch_decision.envelope.submission_id)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "datetime", _TripwireDateTime)
        patch.setattr(time, "time", _forbidden_capability)
        patch.setattr(time, "monotonic", _forbidden_capability)
        patch.setattr(random, "random", _forbidden_capability)
        patch.setattr(random, "randint", _forbidden_capability)
        patch.setattr(uuid, "uuid4", _forbidden_capability)
        patch.setattr(store_module.os, "getpid", _forbidden_capability)

        store = make_store(tmp_path)
        first_recovery = recover_store(store, target)
        record, receipt = prepare_store(store, provenance, target)
        claim = claim_store(store, record.submission_id, target)
        second_recovery = recover_store(
            store, target, observed_at=RECOVER_RESCAN_AT
        )
        current, current_receipt = store.read(record.submission_id, target)
        audit = store.read_audit(record.submission_id, target)
        store.close()

    assert first_recovery.status == "COMPLETE"
    assert second_recovery.status == "COMPLETE"
    assert current == record
    assert current_receipt == receipt
    assert claim.status == "ACQUIRED"
    assert audit


def test_ae_runtime_capability_tripwires_cover_real_store_operations(
    tmp_path, monkeypatch
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

    with monkeypatch.context() as patch:
        patch.setattr(socket, "socket", _forbidden_capability)
        patch.setattr(socket, "create_connection", _forbidden_capability)
        patch.setattr(socket, "getaddrinfo", _forbidden_capability)
        patch.setattr(urllib.request, "urlopen", _forbidden_capability)
        patch.setattr(subprocess, "Popen", _forbidden_capability)
        patch.setattr(store_module.os, "getenv", _forbidden_capability)

        dispatched = store.mark_dispatching(
            decision, owner_id=claim.owner_id, token=claim.token
        )
        outcome = store.persist_transport_outcome(
            BrokerSubmissionOutcome(
                submission_id=envelope.submission_id,
                status="OUTCOME_UNKNOWN",
            ),
            target,
            owner_id=claim.owner_id,
            token=claim.token,
            observed_at=OUTCOME_AT,
        )
        current, _current_receipt = store.read(envelope.submission_id, target)
        audit = store.read_audit(envelope.submission_id, target)

    assert dispatched.state == "DISPATCHING"
    assert outcome.state == "OUTCOME_UNKNOWN"
    assert current.state == "OUTCOME_UNKNOWN"
    assert audit
    store.close()
