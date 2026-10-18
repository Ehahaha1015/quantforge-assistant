"""Core OPERATOR-001 durable store behaviour."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from quantforge.operator import (
    ControlAction,
    ControlState,
    OperatorArgumentError,
    OperatorAuthenticationError,
    OperatorClosedError,
    OperatorConflictError,
    OperatorCorruptionError,
    OperatorNotFoundError,
    OperatorTargetMismatchError,
    SQLiteOperatorControlStore,
    operator_evidence_for_frontier,
)

from ._helpers import (
    COMMITTED_AT,
    OBSERVED_AT,
    AcceptingVerifier,
    RejectingDouble,
    make_policy,
    make_proof,
    make_request,
    make_scope,
    make_target,
    store_path,
)


def test_fresh_store_denies(tmp_path) -> None:
    path = store_path(tmp_path)
    scope = make_scope()
    with SQLiteOperatorControlStore(path, policy=make_policy()) as store:
        assert store.read_control_state(scope) is None
        with pytest.raises(OperatorNotFoundError):
            store.produce_operator_evidence(make_target())
        assert operator_evidence_for_frontier(store, make_target()) is None


def test_enable_and_evidence(tmp_path) -> None:
    path = store_path(tmp_path)
    scope = make_scope()
    request = make_request()
    proof = make_proof(request)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    ) as store:
        outcome = store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
        assert outcome.replay is False
        assert outcome.current_effective_state is ControlState.ENABLED
        state = store.read_control_state(scope)
        assert state is not None
        assert state.state is ControlState.ENABLED
        evidence = store.produce_operator_evidence(make_target())
        assert evidence.enabled is True
        assert evidence.action_id == state.action_id
        assert evidence.observed_at == OBSERVED_AT
        assert evidence.maximum_age == timedelta(minutes=1)


def test_disable_and_evidence(tmp_path) -> None:
    path = store_path(tmp_path)
    scope = make_scope()
    request = make_request(action=ControlAction.DISABLE)
    proof = make_proof(request)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    ) as store:
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
        state = store.read_control_state(scope)
        assert state is not None
        assert state.state is ControlState.DISABLED
        evidence = store.produce_operator_evidence(make_target())
        assert evidence.enabled is False


def test_default_rejecting_verifier_fails_closed(tmp_path) -> None:
    path = store_path(tmp_path)
    request = make_request()
    proof = make_proof(request)
    with (
        SQLiteOperatorControlStore(path, policy=make_policy()) as store,
        pytest.raises(OperatorAuthenticationError),
    ):
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )


def test_injected_rejecting_verifier_fails_closed(tmp_path) -> None:
    path = store_path(tmp_path)
    request = make_request()
    proof = make_proof(request)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=RejectingDouble()
    ) as store, pytest.raises(OperatorAuthenticationError):
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )


def test_wrong_target_scope_is_rejected(tmp_path) -> None:
    path = store_path(tmp_path)
    request = make_request()
    proof = make_proof(request)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    ) as store:
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
        wrong = make_target()
        wrong = wrong.model_copy(update={"account_reference": "acct-other"})
        with pytest.raises(OperatorTargetMismatchError):
            store.produce_operator_evidence(wrong)


def test_replay_is_zero_mutation(tmp_path) -> None:
    path = store_path(tmp_path)
    request = make_request()
    proof = make_proof(request)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    ) as store:
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
        history = store.read_control_history()
        replay = store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
        assert replay.replay is True
        assert replay.original_control_sequence == 1
        assert len(store.read_control_history()) == len(history)


def test_conflict_is_zero_mutation(tmp_path) -> None:
    path = store_path(tmp_path)
    request = make_request()
    proof = make_proof(request)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    ) as store:
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
        history = store.read_control_history()
        conflict = make_request(action=ControlAction.DISABLE)
        conflict_proof = make_proof(conflict, proof_id="proof-2")
        with pytest.raises(OperatorConflictError):
            store.request_control_change(
                conflict, conflict_proof, committed_at=COMMITTED_AT
            )
        assert len(store.read_control_history()) == len(history)


def test_restart_preserves_enable(tmp_path) -> None:
    path = store_path(tmp_path)
    scope = make_scope()
    request = make_request()
    proof = make_proof(request)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    ) as store:
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
    with SQLiteOperatorControlStore(path, policy=make_policy()) as reopened:
        state = reopened.read_control_state(scope)
        assert state is not None
        assert state.state is ControlState.ENABLED
        assert reopened.produce_operator_evidence(make_target()).enabled is True


def test_close_rejects_further_use(tmp_path) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(path, policy=make_policy())
    store.close()
    with pytest.raises(OperatorClosedError):
        store.read_control_state(make_scope())


def test_context_manager_closes(tmp_path) -> None:
    path = store_path(tmp_path)
    with SQLiteOperatorControlStore(path, policy=make_policy()) as store:
        assert store.read_control_state(make_scope()) is None
    with pytest.raises(OperatorClosedError):
        store.read_control_state(make_scope())


def test_append_only_triggers(tmp_path) -> None:
    path = store_path(tmp_path)
    request = make_request()
    proof = make_proof(request)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    ) as store:
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
        connection = sqlite3.connect(path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE control_audit SET requested_action = 'DISABLE'"
                )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM control_audit")
        finally:
            connection.close()


def test_projection_tamper_is_detected(tmp_path) -> None:
    path = store_path(tmp_path)
    request = make_request()
    proof = make_proof(request)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    ) as store:
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE control_state SET state = 'DISABLED'"
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(OperatorCorruptionError):
            store.read_control_state(make_scope())


def test_policy_missing_fails_closed(tmp_path) -> None:
    path = store_path(tmp_path)
    with pytest.raises(OperatorArgumentError):
        SQLiteOperatorControlStore(path)


def test_policy_ceiling_is_enforced() -> None:
    from quantforge.operator import OperatorArgumentError

    with pytest.raises(OperatorArgumentError):
        make_policy(timedelta(hours=2))


def test_schema_object_set_is_exact(tmp_path) -> None:
    path = store_path(tmp_path)
    with SQLiteOperatorControlStore(path, policy=make_policy()):
        pass
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 11
    names = {row[1] for row in rows}
    assert names == {
        "metadata",
        "control_state",
        "control_audit",
        "control_audit_scope_sequence",
        "sqlite_autoindex_metadata_1",
        "sqlite_autoindex_control_state_1",
        "sqlite_autoindex_control_state_2",
        "sqlite_autoindex_control_audit_1",
        "sqlite_autoindex_control_audit_2",
        "control_audit_no_update",
        "control_audit_no_delete",
    }
