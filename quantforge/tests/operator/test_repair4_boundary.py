"""OPERATOR-001 Implementation Repair #4 store trust-boundary invariants.

Covers the Review #4 finding OP-001-REPAIR3-BOUNDARY-MISSING-ATTRIBUTE:
forged value objects with absent required attributes must map to the exact
contracted caller error family, before identity derivation and any write.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from quantforge.frontier import ExecutionTargetBinding
from quantforge.operator import (
    ControlAction,
    OperatorArgumentError,
    OperatorAuthenticationError,
    OperatorAuthenticationProof,
    OperatorControlRequest,
    OperatorControlScope,
    SQLiteOperatorControlStore,
    operator_evidence_for_frontier,
)
from quantforge.operator.identity import request_commitment

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


def _store(tmp_path: Path, name: str = "operator.sqlite") -> SQLiteOperatorControlStore:
    return SQLiteOperatorControlStore(
        tmp_path / name, policy=make_policy(), verifier=AcceptingVerifier()
    )


def _request_fields() -> dict[str, object]:
    return dict(make_request(request_id="base").__dict__)


def _proof_fields(request: OperatorControlRequest) -> dict[str, object]:
    return {
        "schema": "operator-proof-v1",
        "proof_id": "proof-base",
        "operator_reference": request.operator_reference,
        "control_source": request.control_source,
        "action": request.action,
        "scope": request.scope,
        "method": "LOCAL_VERIFIER",
        "request_commitment": request_commitment(request),
    }


def _valid_proof() -> OperatorAuthenticationProof:
    return OperatorAuthenticationProof.model_construct(
        **_proof_fields(make_request(request_id="base"))
    )


def _durable_counts(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path)
    try:
        state = connection.execute(
            "SELECT COUNT(*) FROM control_state"
        ).fetchone()[0]
        audit = connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]
    finally:
        connection.close()
    return int(state), int(audit)


@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "operator_reference",
        "control_source",
        "scope",
        "observed_at",
        "maximum_age",
        "schema",
        "action",
    ],
)
def test_missing_request_field_is_argument_error_zero_write(
    tmp_path, field: str
) -> None:
    path = tmp_path / "request.sqlite"
    store = _store(tmp_path, "request.sqlite")
    fields = _request_fields()
    fields.pop(field, None)
    request = OperatorControlRequest.model_construct(**fields)
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            request,
            _valid_proof(),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    assert _durable_counts(path) == (0, 0)
    store.close()


@pytest.mark.parametrize(
    "field",
    [
        "proof_id",
        "operator_reference",
        "control_source",
        "method",
        "request_commitment",
        "scope",
        "schema",
        "action",
    ],
)
def test_missing_proof_field_is_authentication_error_zero_write(
    tmp_path, field: str
) -> None:
    path = tmp_path / "proof.sqlite"
    store = _store(tmp_path, "proof.sqlite")
    request = make_request(request_id="proof-missing")
    values = _proof_fields(request)
    values.pop(field, None)
    proof = OperatorAuthenticationProof.model_construct(**values)
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert store.read_control_history() == ()
    assert _durable_counts(path) == (0, 0)
    store.close()


@pytest.mark.parametrize(
    "field",
    [
        "broker",
        "environment",
        "account_reference",
        "adapter_id",
        "adapter_version",
        "schema",
    ],
)
def test_missing_request_scope_field_is_argument_error(
    tmp_path, field: str
) -> None:
    store = _store(tmp_path, "request-scope.sqlite")
    scope_fields = dict(make_scope().__dict__)
    scope_fields.pop(field, None)
    fields = _request_fields()
    fields["scope"] = OperatorControlScope.model_construct(**scope_fields)
    request = OperatorControlRequest.model_construct(**fields)
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            request,
            _valid_proof(),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    store.close()


@pytest.mark.parametrize(
    "field",
    ["broker", "environment", "account_reference", "adapter_id", "adapter_version"],
)
def test_missing_proof_scope_field_is_authentication_error(
    tmp_path, field: str
) -> None:
    store = _store(tmp_path, "proof-scope.sqlite")
    request = make_request(request_id="proof-scope-missing")
    scope_fields = dict(make_scope().__dict__)
    scope_fields.pop(field, None)
    values = _proof_fields(request)
    values["scope"] = OperatorControlScope.model_construct(**scope_fields)
    proof = OperatorAuthenticationProof.model_construct(**values)
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert store.read_control_history() == ()
    store.close()


def test_dict_deletion_and_model_construct_variants_are_mapped(tmp_path) -> None:
    store = _store(tmp_path, "mutations.sqlite")
    request = make_request(request_id="mutated")
    del request.__dict__["control_source"]
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            request,
            _valid_proof(),
            committed_at=COMMITTED_AT,
        )
    scope = make_scope()
    del scope.__dict__["adapter_id"]
    fields = _request_fields()
    fields["scope"] = scope
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            OperatorControlRequest.model_construct(**fields),
            _valid_proof(),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    store.close()


def test_missing_attribute_rejected_before_identity_derivation(
    tmp_path, monkeypatch
) -> None:
    import quantforge.operator.store as store_module

    calls = {"commitment": 0, "action_id": 0}
    original_commitment = store_module.request_commitment
    original_action_id = store_module.action_id

    def _commitment(*args: object, **kwargs: object) -> str:
        calls["commitment"] += 1
        return original_commitment(*args, **kwargs)  # type: ignore[arg-type]

    def _action_id(*args: object, **kwargs: object) -> str:
        calls["action_id"] += 1
        return original_action_id(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "request_commitment", _commitment)
    monkeypatch.setattr(store_module, "action_id", _action_id)
    store = _store(tmp_path, "ordering.sqlite")
    fields = _request_fields()
    fields.pop("request_id")
    request = OperatorControlRequest.model_construct(**fields)
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            request,
            _valid_proof(),
            committed_at=COMMITTED_AT,
        )
    values = _proof_fields(make_request(request_id="base"))
    values.pop("proof_id")
    proof = OperatorAuthenticationProof.model_construct(**values)
    valid = make_request(request_id="ordering")
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(valid, proof, committed_at=COMMITTED_AT)
    assert calls == {"commitment": 0, "action_id": 0}
    assert store.read_control_history() == ()
    store.close()


def test_read_apis_reject_malformed_scope(tmp_path) -> None:
    store = _store(tmp_path, "read.sqlite")
    request = make_request(request_id="read")
    store.request_control_change(
        request,
        make_proof(request, proof_id="read-proof"),
        committed_at=COMMITTED_AT,
    )
    scope_fields = dict(make_scope().__dict__)
    scope_fields.pop("broker")
    malformed = OperatorControlScope.model_construct(**scope_fields)
    with pytest.raises(OperatorArgumentError):
        store.read_control_state(malformed)
    with pytest.raises(OperatorArgumentError):
        store.read_control_history(malformed)
    with pytest.raises(OperatorArgumentError):
        store.read_control_state(object())  # type: ignore[arg-type]
    store.close()


def test_producer_apis_reject_forged_target(tmp_path) -> None:
    store = _store(tmp_path, "producer.sqlite")
    request = make_request(request_id="producer")
    store.request_control_change(
        request,
        make_proof(request, proof_id="producer-proof"),
        committed_at=COMMITTED_AT,
    )
    forged = ExecutionTargetBinding.model_construct(
        schema="frontier-target-v1", environment="SANDBOX"
    )
    with pytest.raises(OperatorArgumentError):
        store.produce_operator_evidence(forged)
    with pytest.raises(OperatorArgumentError):
        operator_evidence_for_frontier(store, forged)
    assert operator_evidence_for_frontier(store, make_target()) is not None
    store.close()


def test_valid_request_still_succeeds_and_mutation_preserves_zero_write(
    tmp_path,
) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    request = make_request(request_id="valid-repair4")
    outcome = store.request_control_change(
        request,
        make_proof(request, proof_id="proof-valid-repair4"),
        committed_at=COMMITTED_AT,
    )
    assert outcome.replay is False
    history = store.read_control_history()
    assert len(history) == 1
    fields = _request_fields()
    fields.pop("observed_at")
    malformed = OperatorControlRequest.model_construct(**fields)
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            malformed,
            OperatorAuthenticationProof.model_construct(**_proof_fields(request)),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == history
    store.close()
    assert _durable_counts(path) == (1, 1)


def test_scope_limit_still_rejected_as_argument_error(tmp_path) -> None:
    store = _store(tmp_path, "policy.sqlite")
    bound = timedelta(minutes=5)
    wider = make_request(
        request_id="wider",
        action=ControlAction.DISABLE,
        maximum_age=bound + timedelta(microseconds=1),
    )
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            wider,
            make_proof(wider, proof_id="wider-proof"),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    store.close()
