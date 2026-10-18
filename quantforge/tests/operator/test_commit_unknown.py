"""Commit-unknown poisoning and reopen semantics."""

from __future__ import annotations

import pytest

from quantforge.operator import (
    ControlState,
    OperatorCommitUnknownError,
    SQLiteOperatorControlStore,
)

from ._helpers import (
    COMMITTED_AT,
    AcceptingVerifier,
    make_policy,
    make_proof,
    make_request,
    make_scope,
    make_target,
    store_path,
)


def test_commit_unknown_before_sqlite_commit_poison_and_reopen_zero(
    tmp_path, monkeypatch
) -> None:
    path = store_path(tmp_path)
    scope = make_scope()
    request = make_request()
    proof = make_proof(request)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )

    def fail_before_commit() -> None:
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(store, "_commit_connection", fail_before_commit)
    with pytest.raises(OperatorCommitUnknownError):
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
    assert store._poisoned is True
    with pytest.raises(OperatorCommitUnknownError):
        store.read_control_state(scope)
    with pytest.raises(OperatorCommitUnknownError):
        store.produce_operator_evidence(make_target())
    store.close()

    reopened = SQLiteOperatorControlStore(path, policy=make_policy())
    assert reopened.read_control_state(scope) is None
    reopened.close()


def test_commit_unknown_after_sqlite_commit_reopen_one(
    tmp_path, monkeypatch
) -> None:
    path = store_path(tmp_path)
    scope = make_scope()
    request = make_request()
    proof = make_proof(request)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    real_commit = store._commit_connection

    def fail_after_commit() -> None:
        real_commit()
        raise RuntimeError("injected after commit")

    monkeypatch.setattr(store, "_commit_connection", fail_after_commit)
    with pytest.raises(OperatorCommitUnknownError):
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
    assert store._poisoned is True
    store.close()

    reopened = SQLiteOperatorControlStore(path, policy=make_policy())
    state = reopened.read_control_state(scope)
    assert state is not None
    assert state.state is ControlState.ENABLED
    history = reopened.read_control_history(scope)
    assert len(history) == 1
    assert history[0].control_sequence == 1
    reopened.close()
