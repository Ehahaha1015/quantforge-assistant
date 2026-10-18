"""OPERATOR-001 implementation Repair #2 invariant and oracle matrix.

Covers the five mandatory Review #2 findings: residual candidate discovery,
multi-target alias provenance, projection decode error class, request/proof
grammar re-validation, and the mandatory section-30 oracle families.
"""

from __future__ import annotations

import re
import runpy
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quantforge.operator import (
    ControlAction,
    OperatorArgumentError,
    OperatorAuthenticationError,
    OperatorAuthenticationProof,
    OperatorControlRequest,
    OperatorCorruptionError,
    OperatorSchemaError,
    SQLiteOperatorControlStore,
    VerifiedOperatorIdentity,
    operator_evidence_for_frontier,
)
from quantforge.operator.canonical import normalize_sql
from quantforge.operator.identity import request_commitment
from quantforge.operator.static_checks import (
    execstate_topology,
    scan_tripwire_a,
    scan_tripwire_b,
    scan_tripwire_c,
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

_ROOT = Path(__file__).resolve().parents[2]
_P = "src/quantforge/transport/probe.py"
_EV = "from quantforge.frontier import evaluate_frontier_eligibility\n"
_FE = "from quantforge.frontier.evaluate import evaluate_frontier_eligibility\n"
_DS = "from quantforge.execstate.store import SQLiteExecutionStateStore\n"
_HELP = "from quantforge.operator import operator_evidence_for_frontier\n"


def _b(source: str) -> tuple:
    return scan_tripwire_b({_P: source})


def _c(source: str) -> tuple:
    return scan_tripwire_c({_P: source})


def _a(source: str) -> tuple:
    return scan_tripwire_a({_P: source})


def _a_at(path: str, source: str) -> tuple:
    return scan_tripwire_a({path: source})


# ---------------------------------------------------------------------------
# Finding 1 - residual candidate discovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        # partial-held evaluator
        (
            "import functools\n"
            "def f(store, target, raw, **kw):\n"
            "    p = functools.partial(evaluate_frontier_eligibility)\n"
            "    return p(..., operator_evidence=raw, **kw)\n"
        ),
        # container/subscript retrieval
        (
            "def f(store, target, raw, **kw):\n"
            "    table = [evaluate_frontier_eligibility]\n"
            "    return table[0](..., operator_evidence=raw, **kw)\n"
        ),
        # conditional callable selection
        (
            "def f(store, target, raw, flag, **kw):\n"
            "    ev = evaluate_frontier_eligibility if flag else None\n"
            "    return ev(..., operator_evidence=raw, **kw)\n"
        ),
        # destructured assignment
        (
            "def f(store, target, raw, **kw):\n"
            "    ev, other = evaluate_frontier_eligibility, None\n"
            "    return ev(..., operator_evidence=raw, **kw)\n"
        ),
        # walrus binding
        (
            "def f(**kw):\n"
            "    if (ev := evaluate_frontier_eligibility):\n"
            "        return ev(..., operator_evidence=raw, **kw)\n"
        ),
    ],
)
def test_tripwire_b_residual_candidate_forms_reject(body: str) -> None:
    assert _b(_EV + body)


def test_tripwire_b_closure_held_alias_rejects() -> None:
    source = (
        _EV
        + "def outer():\n"
        + "    ev = evaluate_frontier_eligibility\n"
        + "    def inner(**kw):\n"
        + "        return ev(..., operator_evidence=raw, **kw)\n"
        + "    return inner\n"
    )
    assert _b(source)


def test_tripwire_b_two_hop_name_alias_rejects() -> None:
    source = (
        _EV
        + "def f(store, target, raw, **kw):\n"
        + "    a = evaluate_frontier_eligibility\n"
        + "    b = a\n"
        + "    return b(..., operator_evidence=raw, **kw)\n"
    )
    assert _b(source)


def test_tripwire_b_conditional_alias_rejects() -> None:
    source = (
        _EV
        + "def f(store, target, raw, flag, other, **kw):\n"
        + "    base = evaluate_frontier_eligibility\n"
        + "    ev = base if flag else other\n"
        + "    return ev(..., operator_evidence=raw, **kw)\n"
    )
    assert _b(source)


def test_tripwire_b_container_then_partial_rejects() -> None:
    source = (
        _EV
        + "import functools\n"
        + "def f(store, target, raw, **kw):\n"
        + "    table = [evaluate_frontier_eligibility]\n"
        + "    p = functools.partial(table[0])\n"
        + "    return p(..., operator_evidence=raw, **kw)\n"
    )
    assert _b(source)


def test_tripwire_b_container_name_binding_rejects() -> None:
    source = (
        _EV
        + "def f(store, target, raw, **kw):\n"
        + "    table = [evaluate_frontier_eligibility]\n"
        + "    ev = table[0]\n"
        + "    return ev(..., operator_evidence=raw, **kw)\n"
    )
    assert _b(source)


def test_tripwire_b_closure_return_rejects() -> None:
    source = (
        _EV
        + "def outer():\n"
        + "    def inner():\n"
        + "        return evaluate_frontier_eligibility\n"
        + "    return inner\n"
        + "def f(store, target, raw, **kw):\n"
        + "    ev = outer()()\n"
        + "    return ev(..., operator_evidence=raw, **kw)\n"
    )
    assert _b(source)


def test_tripwire_b_unresolved_safety_candidate_fails_closed() -> None:
    # A getattr lookup keyed by a safety-relevant symbol name is discovered
    # even though the callee cannot be resolved to a canonical identity.
    source = (
        _EV
        + "def f(store, target, raw, module, **kw):\n"
        + "    ev = getattr(module, 'evaluate_frontier_eligibility')\n"
        + "    return ev(..., operator_evidence=raw, **kw)\n"
    )
    assert _b(source)


def test_tripwire_b_reflection_table_callee_fails_closed() -> None:
    source = (
        _EV
        + "def f(store, target, raw, funcs, **kw):\n"
        + "    ev = funcs['evaluate_frontier_eligibility']\n"
        + "    return ev(..., operator_evidence=raw, **kw)\n"
    )
    assert _b(source)


def test_tripwire_b_direct_reflection_table_call_fails_closed() -> None:
    source = (
        _EV
        + "def f(store, target, raw, funcs, **kw):\n"
        + "    return funcs['evaluate_frontier_eligibility'](\n"
        + "        ..., operator_evidence=raw, **kw\n"
        + "    )\n"
    )
    assert _b(source)


@pytest.mark.parametrize(
    "source",
    [
        # closure-held mark_dispatching
        _DS
        + "def outer():\n"
        + "    md = SQLiteExecutionStateStore.mark_dispatching\n"
        + "    def inner(store, dec):\n"
        + "        return md(store, dec)\n"
        + "    return inner\n",
        # non-literal getattr key whose result is invoked
        "def go(store, dec, key):\n"
        + "    return getattr(store, key)(dec)\n",
        # container-held dispatch callable
        _DS
        + "def go(store: SQLiteExecutionStateStore, dec):\n"
        + "    table = [store.mark_dispatching]\n"
        + "    return table[0](dec)\n",
        # conditional dispatch callable
        _DS
        + "def go(store: SQLiteExecutionStateStore, dec, flag):\n"
        + "    md = store.mark_dispatching if flag else None\n"
        + "    return md(dec)\n",
    ],
)
def test_tripwire_c_residual_candidate_forms_reject(source: str) -> None:
    assert _c(source)


def test_tripwire_a_function_local_constructor_alias_rejects() -> None:
    source = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "def build(values):\n"
        "    E = OperatorExecutionEnableEvidence\n"
        "    return E(**values)\n"
    )
    assert _a(source)


def test_tripwire_a_closure_constructor_alias_rejects() -> None:
    source = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "def outer():\n"
        "    E = OperatorExecutionEnableEvidence\n"
        "    def inner(values):\n"
        "        return E(**values)\n"
        "    return inner\n"
    )
    assert _a(source)


def test_tripwire_a_constructor_two_hop_alias_rejects() -> None:
    source = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "def f(values):\n"
        "    E = OperatorExecutionEnableEvidence\n"
        "    F2 = E\n"
        "    return F2(**values)\n"
    )
    assert _a(source)


def test_tripwire_c_composed_dispatch_alias_rejects() -> None:
    source = (
        _DS
        + "def f(store: SQLiteExecutionStateStore, dec):\n"
        + "    table = [store.mark_dispatching]\n"
        + "    md = table[0]\n"
        + "    return md(dec)\n"
    )
    assert _c(source)


def test_tripwire_c_two_hop_dispatch_alias_rejects() -> None:
    source = (
        _DS
        + "def f(store: SQLiteExecutionStateStore, dec):\n"
        + "    a = store.mark_dispatching\n"
        + "    b = a\n"
        + "    return b(dec)\n"
    )
    assert _c(source)


@pytest.mark.parametrize(
    "source",
    [
        "import functools\ndef f(fn, x):\n    return functools.partial(fn, x)()\n",
        "def f(obj):\n    return getattr(obj, 'ordinary')()\n",
        "def f(fn):\n    g = lambda x: fn(x)\n    return g(1)\n",
        "def f(cb):\n    fns = [cb]\n    return fns[0]()\n",
        "def f(a, b, flag):\n    g = a if flag else b\n    return g()\n",
        "def f(fn):\n    if (g := fn):\n        return g(1)\n",
    ],
)
def test_benign_dynamic_python_is_not_rejected(source: str) -> None:
    assert _b(source) == ()


def test_canonical_negative_controls_remain_accepted() -> None:
    direct = (
        _EV
        + _HELP
        + "def f(store, target, **kw):\n"
        + "    return evaluate_frontier_eligibility(\n"
        + "        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kw\n"
        + "    )\n"
    )
    binding = (
        _EV
        + _HELP
        + "def f(store, target, **kw):\n"
        + "    evidence = operator_evidence_for_frontier(store, target)\n"
        + "    return evaluate_frontier_eligibility(..., operator_evidence=evidence, **kw)\n"
    )
    decision = (
        _FE
        + _DS
        + _HELP
        + "def dispatch(store: SQLiteExecutionStateStore, target, **kw):\n"
        + "    decision = evaluate_frontier_eligibility(\n"
        + "        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kw\n"
        + "    )\n"
        + "    return store.mark_dispatching(decision, owner_id='o', token='t')\n"
    )
    assert _b(direct) == ()
    assert _b(binding) == ()
    assert _c(decision) == ()


def test_frozen_construction_allowlist_sites_remain_accepted() -> None:
    category_two = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "def _parse_frontier_model(values):\n"
        "    return OperatorExecutionEnableEvidence(**values)\n"
    )
    category_one = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "def operator_evidence_for_frontier(store, target):\n"
        "    return OperatorExecutionEnableEvidence(**values)\n"
    )
    assert _a_at("src/quantforge/frontier/models.py", category_two) == ()
    assert _a_at("src/quantforge/operator/store.py", category_one) == ()


def test_frozen_repository_tripwires_stay_green() -> None:
    assert scan_tripwire_a(
        {
            str(path): path.read_text(encoding="utf-8")
            for path in (_ROOT / "src").rglob("*.py")
        }
    ) == ()


# ---------------------------------------------------------------------------
# Finding 2 - multi-target alias provenance
# ---------------------------------------------------------------------------


def test_multi_target_evidence_alias_rejects() -> None:
    source = (
        _EV
        + _HELP
        + "def f(store, target, **kw):\n"
        + "    e = o = operator_evidence_for_frontier(store, target)\n"
        + "    object.__setattr__(o, 'enabled', True)\n"
        + "    return evaluate_frontier_eligibility(..., operator_evidence=e, **kw)\n"
    )
    assert _b(source)


def test_multi_target_evidence_alias_without_mutation_still_rejects() -> None:
    source = (
        _EV
        + _HELP
        + "def f(store, target, **kw):\n"
        + "    e = o = operator_evidence_for_frontier(store, target)\n"
        + "    return evaluate_frontier_eligibility(..., operator_evidence=e, **kw)\n"
    )
    assert _b(source)


def test_multi_target_decision_alias_rejects() -> None:
    source = (
        _FE
        + _DS
        + _HELP
        + "def dispatch(store: SQLiteExecutionStateStore, target, **kw):\n"
        + "    dec = o = evaluate_frontier_eligibility(\n"
        + "        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kw\n"
        + "    )\n"
        + "    object.__setattr__(o, 'eligible', True)\n"
        + "    return store.mark_dispatching(dec, owner_id='o', token='t')\n"
    )
    assert _c(source)


def test_destructured_evidence_binding_rejects() -> None:
    source = (
        _EV
        + _HELP
        + "def f(store, target, **kw):\n"
        + "    (e,) = (operator_evidence_for_frontier(store, target),)\n"
        + "    return evaluate_frontier_eligibility(..., operator_evidence=e, **kw)\n"
    )
    assert _b(source)


# ---------------------------------------------------------------------------
# Finding 3 - projection decode error class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("maximum_age_microseconds", "abc"),
        ("control_sequence", "abc"),
        ("maximum_age_microseconds", float("inf")),
        ("control_sequence", float("inf")),
        ("maximum_age_microseconds", 1.5),
        ("control_sequence", 1.5),
        ("maximum_age_microseconds", float(2**63)),
        ("control_sequence", float(2**63)),
        ("observed_at", "not-an-instant"),
        ("broker", "bad value"),
        ("state_version_id", "bad value"),
    ],
)
def test_projection_decode_corruption(tmp_path, column: str, value: object) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    request = make_request(request_id=f"proj-{column}")
    store.request_control_change(
        request,
        make_proof(request, proof_id=f"proof-{column}"),
        committed_at=COMMITTED_AT,
    )
    store.close()
    connection = sqlite3.connect(path)
    connection.execute(f"UPDATE control_state SET {column} = ?", (value,))
    connection.commit()
    connection.close()
    with pytest.raises(OperatorCorruptionError):
        SQLiteOperatorControlStore(path, policy=make_policy())


def test_projection_decode_corruption_on_open_store(tmp_path) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    request = make_request(request_id="proj-live")
    store.request_control_change(
        request, make_proof(request, proof_id="proof-proj-live"), committed_at=COMMITTED_AT
    )
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE control_state SET maximum_age_microseconds = 'abc'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(OperatorCorruptionError):
        store.read_control_state(make_scope())
    store.close()


@pytest.mark.parametrize(
    "column",
    ["maximum_age_microseconds", "control_sequence"],
)
def test_projection_overflow_on_open_store_reads_are_corruption(
    tmp_path, column: str
) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    request = make_request(request_id=f"proj-inf-{column}")
    store.request_control_change(
        request,
        make_proof(request, proof_id=f"proof-inf-{column}"),
        committed_at=COMMITTED_AT,
    )
    connection = sqlite3.connect(path)
    connection.execute(
        f"UPDATE control_state SET {column} = ?", (float("inf"),)
    )
    connection.commit()
    connection.close()
    with pytest.raises(OperatorCorruptionError):
        store.read_control_state(make_scope())
    with pytest.raises(OperatorCorruptionError):
        store.read_control_history()
    with pytest.raises(OperatorCorruptionError):
        operator_evidence_for_frontier(store, make_target())
    store.close()


def test_caller_argument_errors_stay_argument_errors() -> None:
    with pytest.raises(OperatorArgumentError):
        make_request(request_id="bad value")
    with pytest.raises(OperatorArgumentError):
        make_policy(timedelta(hours=2))


# ---------------------------------------------------------------------------
# Finding 4 - request/proof grammar bypass
# ---------------------------------------------------------------------------


class _EchoVerifier:
    @property
    def verifier_id(self) -> str:
        return "operator-test-echo-v1"

    def verify(self, request, proof):  # type: ignore[no-untyped-def]
        return VerifiedOperatorIdentity.model_construct(
            schema="operator-verified-v1",
            operator_reference=request.operator_reference,
            control_source=request.control_source,
            verifier_id=self.verifier_id,
            verified_at=request.observed_at + timedelta(seconds=1),
            proof_id=proof.proof_id,
        )


def _echo_store(tmp_path, name: str) -> SQLiteOperatorControlStore:
    return SQLiteOperatorControlStore(
        tmp_path / name, policy=make_policy(), verifier=_EchoVerifier()
    )


_BAD = "__OPCTL_SENTINEL_PROOFID__"


def _base_proof_values(request: OperatorControlRequest) -> dict[str, object]:
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


@pytest.mark.parametrize(
    "field",
    ["proof_id", "operator_reference", "control_source", "request_commitment"],
)
def test_bypass_proof_grammar_is_authentication_error(
    tmp_path, field: str
) -> None:
    request = make_request(request_id=f"proof-{field}")
    values = _base_proof_values(request)
    values[field] = _BAD
    proof = OperatorAuthenticationProof.model_construct(**values)
    store = _echo_store(tmp_path, f"proof-{field}.sqlite")
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert store.read_control_history() == ()
    store.close()


@pytest.mark.parametrize("field", ["request_id", "operator_reference", "control_source"])
def test_bypass_request_grammar_is_argument_error(tmp_path, field: str) -> None:
    values = {
        "schema": "operator-request-v1",
        "request_id": "req-base",
        "action": ControlAction.ENABLE,
        "scope": make_scope(),
        "operator_reference": "operator-1",
        "control_source": "control-source-1",
        "observed_at": make_request().observed_at,
        "maximum_age": timedelta(minutes=1),
    }
    values[field] = _BAD
    request = OperatorControlRequest.model_construct(**values)
    proof = OperatorAuthenticationProof.model_construct(
        **_base_proof_values(request)
    )
    store = _echo_store(tmp_path, f"request-{field}.sqlite")
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert store.read_control_history() == ()
    store.close()


def test_bypass_scope_grammar_is_argument_error(tmp_path) -> None:
    scope = make_scope()
    object.__setattr__(scope, "broker", _BAD)
    request = OperatorControlRequest.model_construct(
        schema="operator-request-v1",
        request_id="req-scope",
        action=ControlAction.ENABLE,
        scope=scope,
        operator_reference="operator-1",
        control_source="control-source-1",
        observed_at=make_request().observed_at,
        maximum_age=timedelta(minutes=1),
    )
    proof = OperatorAuthenticationProof.model_construct(
        **_base_proof_values(request)
    )
    store = _echo_store(tmp_path, "request-scope.sqlite")
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert store.read_control_history() == ()
    store.close()


def test_valid_request_still_succeeds(tmp_path) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    request = make_request(request_id="valid-repair2")
    outcome = store.request_control_change(
        request,
        make_proof(request, proof_id="proof-valid-repair2"),
        committed_at=COMMITTED_AT,
    )
    assert outcome.replay is False
    assert len(store.read_control_history()) == 1
    store.close()


# ---------------------------------------------------------------------------
# Finding 3 (Review #3) - schema literal and action revalidation
# ---------------------------------------------------------------------------


def _forged_request(
    request_id: str, **overrides: object
) -> OperatorControlRequest:
    values: dict[str, object] = {
        "schema": "operator-request-v1",
        "request_id": request_id,
        "action": ControlAction.ENABLE,
        "scope": make_scope(),
        "operator_reference": "operator-1",
        "control_source": "control-source-1",
        "observed_at": make_request().observed_at,
        "maximum_age": timedelta(minutes=1),
    }
    values.update(overrides)
    return OperatorControlRequest.model_construct(**values)


def _forged_proof(
    request: OperatorControlRequest, **overrides: object
) -> OperatorAuthenticationProof:
    values: dict[str, object] = {
        "schema": "operator-proof-v1",
        "proof_id": "proof-forged",
        "operator_reference": request.operator_reference,
        "control_source": request.control_source,
        "action": request.action,
        "scope": request.scope,
        "method": "LOCAL_VERIFIER",
        "request_commitment": "sha256:" + "0" * 64,
    }
    values.update(overrides)
    return OperatorAuthenticationProof.model_construct(**values)


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
    "schema",
    ["operator-request-9", "", None, object()],
)
def test_forged_request_schema_is_argument_error_zero_write(
    tmp_path, schema: object
) -> None:
    path = tmp_path / "forged-request-schema.sqlite"
    store = _echo_store(tmp_path, "forged-request-schema.sqlite")
    request = _forged_request("req-schema-9", schema=schema)
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            request,
            _forged_proof(request, schema="operator-proof-9"),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    assert _durable_counts(path) == (0, 0)
    store.close()
    assert b"operator-request-9" not in path.read_bytes()


def test_forged_request_schema_via_dict_mutation_is_argument_error(
    tmp_path,
) -> None:
    store = _echo_store(tmp_path, "forged-request-dict.sqlite")
    request = make_request(request_id="req-dict-schema")
    vars(request)["schema"] = "operator-request-9"
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            request,
            _forged_proof(request),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    store.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "operator-proof-9"},
        {"schema": None},
        {"schema": object()},
        {"action": "ENABLE"},
        {"action": object()},
    ],
)
def test_forged_proof_schema_or_action_is_authentication_error_zero_write(
    tmp_path, overrides: dict[str, object]
) -> None:
    store = _echo_store(
        tmp_path, f"forged-proof-{min(overrides)}.sqlite"
    )
    request = make_request(request_id="req-forged-proof")
    proof = _forged_proof(request, **overrides)
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert store.read_control_history() == ()
    store.close()


@pytest.mark.parametrize("action", ["ENABLE", "DISABLE", object()])
def test_forged_request_action_is_argument_error_zero_write(
    tmp_path, action: object
) -> None:
    path = tmp_path / "forged-action.sqlite"
    store = _echo_store(tmp_path, "forged-action.sqlite")
    request = _forged_request("req-forged-action", action=action)
    proof = _forged_proof(request, action=action)
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert store.read_control_history() == ()
    assert _durable_counts(path) == (0, 0)
    store.close()


def test_forged_request_scope_schema_is_argument_error(tmp_path) -> None:
    store = _echo_store(tmp_path, "forged-request-scope.sqlite")
    scope = make_scope()
    object.__setattr__(scope, "schema", "operator-scope-9")
    request = _forged_request("req-scope-schema", scope=scope)
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            request, _forged_proof(request), committed_at=COMMITTED_AT
        )
    assert store.read_control_history() == ()
    store.close()


def test_forged_proof_scope_schema_is_authentication_error(tmp_path) -> None:
    store = _echo_store(tmp_path, "forged-proof-scope.sqlite")
    request = make_request(request_id="req-proof-scope-schema")
    scope = make_scope()
    object.__setattr__(scope, "schema", "operator-scope-9")
    proof = _forged_proof(request, scope=scope)
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert store.read_control_history() == ()
    store.close()


def test_forged_request_rejected_before_identity_derivation(
    tmp_path, monkeypatch
) -> None:
    import quantforge.operator.store as store_module

    def _forbidden(*args: object, **kwargs: object) -> str:
        raise AssertionError("identity derivation must not run")

    monkeypatch.setattr(store_module, "request_commitment", _forbidden)
    monkeypatch.setattr(store_module, "action_id", _forbidden)
    store = _echo_store(tmp_path, "forged-before-identity.sqlite")
    request = _forged_request("req-before-identity", schema="operator-request-9")
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            request,
            _forged_proof(request, schema="operator-proof-9"),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    store.close()


# ---------------------------------------------------------------------------
# Finding 5 - mandatory oracles
# ---------------------------------------------------------------------------


def _fresh_store(path: Path) -> SQLiteOperatorControlStore:
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    return store


_SQL_OBJECT_TARGETS = (
    ("table", "metadata"),
    ("table", "control_state"),
    ("table", "control_audit"),
    ("index", "control_audit_scope_sequence"),
    ("trigger", "control_audit_no_update"),
    ("trigger", "control_audit_no_delete"),
)


def _contract_sql_objects() -> dict[tuple[str, str], tuple[str, str]]:
    """Normative SQL objects read from the frozen contract, not production."""

    contract = (_ROOT / "tasks/OPERATOR-001.md").read_text(encoding="utf-8")
    marker = "```sql"
    assert contract.count(marker) == 1
    start = contract.index(marker) + len(marker)
    end = contract.index("```", start)
    block = contract[start:end]
    statements = [
        statement
        for statement in re.split(r"(?m)^(?=CREATE )", block)
        if statement.strip()
    ]
    objects: dict[tuple[str, str], tuple[str, str]] = {}
    for statement in statements:
        match = re.match(r"CREATE (TABLE|INDEX|TRIGGER)\s+(\w+)", statement)
        assert match is not None, statement
        kind = match.group(1).lower()
        name = match.group(2)
        if kind == "table":
            table_name = name
        else:
            table_match = re.search(r"\bON\s+(\w+)", statement)
            assert table_match is not None, statement
            table_name = table_match.group(1)
        objects[(kind, name)] = (table_name, statement.strip())
    return objects


def _rewrite_schema_sql(
    path: Path, kind: str, name: str, rewrite: Callable[[str], str]
) -> None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
            (kind, name),
        ).fetchone()
        assert row is not None and row[0] is not None
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = ? AND name = ?",
            (rewrite(str(row[0])), kind, name),
        )
        connection.commit()
        connection.execute("PRAGMA writable_schema = OFF")
    finally:
        connection.close()


def test_fr01_a_malformed_proof_maps_to_authentication_error(tmp_path) -> None:
    path = store_path(tmp_path)
    store = _fresh_store(path)
    request = make_request(request_id="fr01a")
    malformed = request.model_copy(update={"request_id": "different-request"})
    proof = make_proof(request, proof_id="proof-fr01a")
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(
            malformed, proof, committed_at=COMMITTED_AT
        )
    assert store.read_control_history() == ()
    store.close()


def test_fr01_b_observed_after_verified_maps_to_authentication_error(
    tmp_path,
) -> None:
    path = store_path(tmp_path)

    class FutureInstantVerifier(AcceptingVerifier):
        def verify(self, request, proof):  # type: ignore[no-untyped-def]
            return VerifiedOperatorIdentity.model_construct(
                schema="operator-verified-v1",
                operator_reference=request.operator_reference,
                control_source=request.control_source,
                verifier_id=self.verifier_id,
                verified_at=request.observed_at - timedelta(seconds=5),
                proof_id=proof.proof_id,
            )

    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=FutureInstantVerifier()
    )
    request = make_request(request_id="fr01b")
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(
            request,
            make_proof(request, proof_id="proof-fr01b"),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    store.close()


def test_fr01_c_noncanonical_verified_at_maps_to_authentication_error(
    tmp_path,
) -> None:
    path = store_path(tmp_path)
    non_instant = "2030-01-01T12:00:01Z"  # not a datetime object at all

    class NonCanonicalInstantVerifier(AcceptingVerifier):
        def verify(self, request, proof):  # type: ignore[no-untyped-def]
            return VerifiedOperatorIdentity.model_construct(
                schema="operator-verified-v1",
                operator_reference=request.operator_reference,
                control_source=request.control_source,
                verifier_id=self.verifier_id,
                verified_at=non_instant,
                proof_id=proof.proof_id,
            )

    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=NonCanonicalInstantVerifier()
    )
    request = make_request(request_id="fr01c")
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(
            request,
            make_proof(request, proof_id="proof-fr01c"),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    store.close()


def test_fr01_d_stale_enable_does_not_block_disable(tmp_path) -> None:
    path = store_path(tmp_path)
    store = _fresh_store(path)
    enable = make_request(request_id="fr01d-enable", observed_at=COMMITTED_AT)
    store.request_control_change(
        enable,
        make_proof(enable, proof_id="proof-fr01d-enable"),
        committed_at=COMMITTED_AT,
    )
    disable = make_request(
        request_id="fr01d-disable",
        action=ControlAction.DISABLE,
        observed_at=COMMITTED_AT - timedelta(days=30),
    )
    outcome = store.request_control_change(
        disable,
        make_proof(disable, proof_id="proof-fr01d-disable"),
        committed_at=COMMITTED_AT,
    )
    assert outcome.replay is False
    assert outcome.current_effective_state is not None
    assert outcome.current_effective_state.value == "DISABLED"
    store.close()


def test_fr01_e_invalid_auth_time_prevents_disable(tmp_path) -> None:
    path = store_path(tmp_path)

    class BackwardsVerifier(AcceptingVerifier):
        def verify(self, request, proof):  # type: ignore[no-untyped-def]
            return VerifiedOperatorIdentity.model_construct(
                schema="operator-verified-v1",
                operator_reference=request.operator_reference,
                control_source=request.control_source,
                verifier_id=self.verifier_id,
                verified_at=request.observed_at - timedelta(seconds=1),
                proof_id=proof.proof_id,
            )

    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=BackwardsVerifier()
    )
    request = make_request(
        request_id="fr01e", action=ControlAction.DISABLE
    )
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(
            request,
            make_proof(request, proof_id="proof-fr01e"),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    store.close()


def test_sqlnorm_a_normative_schema_self_accepts(tmp_path) -> None:
    path = store_path(tmp_path)
    store = _fresh_store(path)
    store.close()
    expected = _contract_sql_objects()
    assert len(expected) == 6
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
        ).fetchall()
    finally:
        connection.close()
    stored = {(row[0], row[1]): row for row in rows if row[3] is not None}
    assert set(stored) == set(expected)
    for key, (tbl_name, normative_sql) in expected.items():
        row = stored[key]
        assert row[2] == tbl_name, key
        assert normalize_sql(normative_sql) == normalize_sql(str(row[3])), key
    automatic = {
        row[1]: row[2] for row in rows if row[0] == "index" and row[3] is None
    }
    assert automatic == {
        "sqlite_autoindex_metadata_1": "metadata",
        "sqlite_autoindex_control_state_1": "control_state",
        "sqlite_autoindex_control_state_2": "control_state",
        "sqlite_autoindex_control_audit_1": "control_audit",
        "sqlite_autoindex_control_audit_2": "control_audit",
    }


def test_sqlnorm_b_trailing_terminator_does_not_change_equality() -> None:
    base = "CREATE TABLE t (a TEXT)"
    assert normalize_sql(base) == normalize_sql(base + ";")
    assert normalize_sql(base) != normalize_sql(base + ";;")


def test_sqlnorm_c_case_normalization_outside_literals() -> None:
    assert normalize_sql("CREATE TABLE T (A TEXT)") == normalize_sql(
        "create table t (a text)"
    )


def test_sqlnorm_d_case_variation_inside_literal_is_detected(tmp_path) -> None:
    path = store_path(tmp_path)
    store = _fresh_store(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'control_state'"
        ).fetchone()
        assert row is not None
        tampered = str(row[0]).replace("'ENABLED','DISABLED'", "'enabled','DISABLED'")
        assert normalize_sql(tampered) != normalize_sql(str(row[0]))
    finally:
        connection.close()


def test_sqlnorm_e_literals_are_never_rewritten() -> None:
    source = "CREATE TABLE t (a TEXT CHECK (a IN ('A  B','it''s','MiXeD')))"
    normalized = normalize_sql(source)
    assert "'A  B'" in normalized
    assert "'it''s'" in normalized
    assert "'MiXeD'" in normalized


@pytest.mark.parametrize(("kind", "name"), _SQL_OBJECT_TARGETS)
def test_sqlnorm_f_one_rule_accepts_normalization_invariant_rewrite(
    tmp_path, kind: str, name: str
) -> None:
    path = store_path(tmp_path)
    store = _fresh_store(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        original = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
                (kind, name),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    rewritten = original.replace("CREATE", "create", 1)
    assert rewritten != original
    assert normalize_sql(rewritten) == normalize_sql(original)
    _rewrite_schema_sql(path, kind, name, lambda _sql: rewritten)
    reopened = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    reopened.close()


@pytest.mark.parametrize(("kind", "name"), _SQL_OBJECT_TARGETS)
def test_sqlnorm_f_one_rule_rejects_normalization_sensitive_rewrite(
    tmp_path, kind: str, name: str
) -> None:
    path = store_path(tmp_path)
    store = _fresh_store(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        original = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
                (kind, name),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    rewritten = original.rstrip().rstrip(";") + ";;"
    assert normalize_sql(rewritten) != normalize_sql(original)
    _rewrite_schema_sql(path, kind, name, lambda _sql: rewritten)
    with pytest.raises(OperatorSchemaError):
        SQLiteOperatorControlStore(
            path, policy=make_policy(), verifier=AcceptingVerifier()
        )


def test_sqlnorm_g_automatic_indexes_bypass_normalize_sql(tmp_path) -> None:
    path = store_path(tmp_path)
    store = _fresh_store(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NULL"
        ).fetchall()
    finally:
        connection.close()
    names = {row[1] for row in rows}
    assert names == {
        "sqlite_autoindex_metadata_1",
        "sqlite_autoindex_control_state_1",
        "sqlite_autoindex_control_state_2",
        "sqlite_autoindex_control_audit_1",
        "sqlite_autoindex_control_audit_2",
    }
    assert all(row[0] == "index" and row[3] is None for row in rows)


def test_sentinel_families_fail_channel_grammar() -> None:
    pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    for sentinel in (
        "__OPCTL_SENTINEL_TESTVALUE__",
        "_OPCTL-FIXTURE-SECRET-0123456789abcdef0123456789abcdef",
    ):
        assert pattern.fullmatch(sentinel) is None


def test_policy_bound_a_equal_bound_is_valid_and_wider_is_rejected(
    tmp_path,
) -> None:
    bound = timedelta(minutes=5)
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(bound), verifier=AcceptingVerifier()
    )
    at_bound = make_request(request_id="policy-bound-equal", maximum_age=bound)
    outcome = store.request_control_change(
        at_bound,
        make_proof(at_bound, proof_id="proof-policy-bound-equal"),
        committed_at=COMMITTED_AT,
    )
    assert outcome.replay is False
    history = store.read_control_history()
    assert len(history) == 1
    assert history[0].maximum_age == bound
    state = store.read_control_state(make_scope())
    assert state.maximum_age == bound
    wider = make_request(
        request_id="policy-bound-wider",
        action=ControlAction.DISABLE,
        maximum_age=bound + timedelta(microseconds=1),
    )
    with pytest.raises(OperatorArgumentError):
        store.request_control_change(
            wider,
            make_proof(wider, proof_id="proof-policy-bound-wider"),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == history
    assert store.read_control_state(make_scope()).maximum_age == bound
    store.close()


def _complete_frontier_inputs() -> dict[str, object]:
    fixture = runpy.run_path(
        str(_ROOT / "tests/frontier/_frontier_fixtures.py")
    )
    return fixture["complete_inputs"]()


def _human_authorization_and_order():  # type: ignore[no-untyped-def]
    from decimal import Decimal

    from quantforge.domain import OrderIntent
    from quantforge.live import HumanLiveAuthorization, create_live_authorization

    order = OrderIntent(
        strategy_id="alpha",
        symbol="AAPL",
        timestamp=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        side="BUY",
        quantity=Decimal("5.00"),
    )
    authorization = create_live_authorization(
        order, "operator-7", datetime(2030, 1, 1, 12, 1, tzinfo=UTC)
    )
    assert isinstance(authorization, HumanLiveAuthorization)
    return order, authorization


def test_human_authorization_terms_remain_frozen_in_contracts() -> None:
    live_contract = (_ROOT / "tasks/LIVE-001.md").read_text(encoding="utf-8")
    operator_contract = (_ROOT / "tasks/OPERATOR-001.md").read_text(
        encoding="utf-8"
    )
    assert "HumanLiveAuthorization" in live_contract
    assert "HumanLiveAuthorization" in operator_contract
    assert "a valid human authorization NOT imply operator enable" in operator_contract
    assert "a valid operator enable NOT imply a human authorization" in operator_contract


def test_operator_enable_does_not_substitute_for_human_authorization(
    tmp_path,
) -> None:
    from quantforge.frontier import FrontierReason, evaluate_frontier_eligibility
    from quantforge.live import authorize_live_order
    from quantforge.risk import RiskDecision

    order, authorization = _human_authorization_and_order()
    permission = authorize_live_order(
        order, RiskDecision(approved=True), authorization
    )
    assert permission.authorization == authorization

    # A valid human authorization with no operator state still denies.
    values = _complete_frontier_inputs()
    decision = evaluate_frontier_eligibility(
        **{**values, "operator_evidence": None}
    )
    assert FrontierReason.OPERATOR_EVIDENCE_MISSING in decision.reasons

    # A committed DISABLED operator state also still denies.
    disabled = values["operator_evidence"].model_copy(  # type: ignore[union-attr]
        update={"enabled": False, "evidence_id": "human-sep-disabled"}
    )
    decision = evaluate_frontier_eligibility(
        **{**values, "operator_evidence": disabled}
    )
    assert FrontierReason.OPERATOR_DISABLED in decision.reasons

    # A real ENABLE evidence object is not a human authorization.
    operator_store = _fresh_store(tmp_path / "human-sep-operator.sqlite")
    request = make_request(request_id="human-sep-enable")
    operator_store.request_control_change(
        request,
        make_proof(request, proof_id="proof-human-sep-enable"),
        committed_at=COMMITTED_AT,
    )
    evidence = operator_evidence_for_frontier(operator_store, make_target())
    assert evidence is not None and evidence.enabled is True
    with pytest.raises(TypeError):
        authorize_live_order(order, RiskDecision(approved=True), evidence)
    operator_store.close()


def test_human_authorization_does_not_substitute_for_operator_evidence(
    tmp_path,
) -> None:
    from quantforge.frontier import FrontierReason, evaluate_frontier_eligibility
    from quantforge.live import authorize_live_order
    from quantforge.risk import RiskDecision

    order, authorization = _human_authorization_and_order()
    permission = authorize_live_order(
        order, RiskDecision(approved=True), authorization
    )
    operator_store = _fresh_store(tmp_path / "human-sep-missing.sqlite")
    # No committed operator state: there is no CATEGORY 1 evidence at all.
    assert operator_evidence_for_frontier(operator_store, make_target()) is None
    # A real human authorization cannot fill the operator_evidence slot.
    values = _complete_frontier_inputs()
    missing = evaluate_frontier_eligibility(
        **{**values, "operator_evidence": None}
    )
    assert missing.eligible is False
    assert FrontierReason.OPERATOR_EVIDENCE_MISSING in missing.reasons
    with pytest.raises(TypeError):
        evaluate_frontier_eligibility(
            **{**values, "operator_evidence": permission.authorization}
        )
    operator_store.close()


def test_operator_package_does_not_reference_execstate_authority() -> None:
    import ast

    for path in (_ROOT / "src/quantforge/operator").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.level == 0
            ):
                imported.add(node.module)
        for forbidden in (
            "quantforge.execstate",
            "quantforge.execution_ledger",
            "quantforge.live",
            "quantforge.broker",
        ):
            assert not any(
                name == forbidden or name.startswith(forbidden + ".")
                for name in imported
            ), (path.name, forbidden)


def test_operator_package_never_names_outcome_unknown_or_retry() -> None:
    import ast

    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (_ROOT / "src/quantforge/operator").glob("*.py")
    )
    tree = ast.parse(source)
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for forbidden in (
        "OUTCOME_UNKNOWN",
        "clear_unknown",
        "resolve_unknown",
        "retry",
        "resubmit",
    ):
        assert forbidden not in strings, forbidden


def test_operator_enable_cannot_clear_outcome_unknown(tmp_path) -> None:
    from quantforge.broker import BrokerSubmissionOutcome
    from quantforge.execstate import ExecutionStateDeniedError
    from quantforge.frontier import FrontierReason, evaluate_frontier_eligibility
    from tests.execstate import _helpers as execstate_helpers

    (
        exec_store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
        envelope,
    ) = execstate_helpers.prepare_claim_recover(tmp_path)
    decision = execstate_helpers.build_eligibility(
        exec_store, provenance, target, record, receipt, recovery, claim
    )
    exec_store.mark_dispatching(
        decision, owner_id="worker-1", token="token-1"
    )
    exec_store.persist_transport_outcome(
        BrokerSubmissionOutcome(
            submission_id=envelope.submission_id,
            status="OUTCOME_UNKNOWN",
        ),
        target,
        owner_id="worker-1",
        token="token-1",
        observed_at=execstate_helpers.OUTCOME_AT,
    )
    before_record, _before_receipt = exec_store.read(
        envelope.submission_id, target
    )
    before_audit = exec_store.read_audit(envelope.submission_id, target)
    assert before_record.state == "OUTCOME_UNKNOWN"

    operator_store = _fresh_store(tmp_path / "operator-outcome-unknown.sqlite")
    enable = make_request(request_id="operator-unknown-enable")
    operator_store.request_control_change(
        enable,
        make_proof(enable, proof_id="proof-operator-unknown-enable"),
        committed_at=COMMITTED_AT,
    )
    evidence = operator_evidence_for_frontier(operator_store, target)
    assert evidence is not None and evidence.enabled is True
    values = _complete_frontier_inputs()
    values.update(
        {
            "target": target,
            "dispatch_timestamp": execstate_helpers.OUTCOME_AT,
            "operator_evidence": evidence,
            "durable_record": before_record,
            "commit_receipt": None,
            "recovery_evidence": None,
            "claim": None,
        }
    )
    decision = evaluate_frontier_eligibility(**values)
    assert FrontierReason.OUTCOME_UNKNOWN in decision.reasons

    # The unknown attempt did not become retryable or resubmittable.
    with pytest.raises(ExecutionStateDeniedError):
        exec_store.claim(
            envelope.submission_id,
            target,
            owner_id="worker-2",
            token="token-2",
            acquired_at=execstate_helpers.CLAIM_AT,
            expires_at=execstate_helpers.CLAIM_EXPIRES_AT,
        )
    assert exec_store.read(envelope.submission_id, target)[0] == before_record
    assert exec_store.read_audit(envelope.submission_id, target) == before_audit

    disable = make_request(
        request_id="operator-unknown-disable",
        action=ControlAction.DISABLE,
    )
    operator_store.request_control_change(
        disable,
        make_proof(disable, proof_id="proof-operator-unknown-disable"),
        committed_at=COMMITTED_AT,
    )
    assert exec_store.read(envelope.submission_id, target)[0] == before_record
    assert exec_store.read_audit(envelope.submission_id, target) == before_audit
    operator_store.close()
    exec_store.close()


def test_operator_control_does_not_mutate_execstate(tmp_path) -> None:
    from tests.execstate import _helpers as execstate_helpers

    (
        exec_store,
        _provenance,
        target,
        _record,
        _receipt,
        _recovery,
        _claim,
        envelope,
    ) = execstate_helpers.prepare_claim_recover(tmp_path)
    before_record, before_receipt = exec_store.read(
        envelope.submission_id, target
    )
    before_audit = exec_store.read_audit(envelope.submission_id, target)
    assert before_record.state == "PREPARED"

    operator_store = _fresh_store(tmp_path / "operator-separation.sqlite")
    for index, action in enumerate(
        (ControlAction.ENABLE, ControlAction.DISABLE, ControlAction.ENABLE)
    ):
        request = make_request(
            request_id=f"operator-separation-{index}", action=action
        )
        outcome = operator_store.request_control_change(
            request,
            make_proof(request, proof_id=f"proof-operator-separation-{index}"),
            committed_at=COMMITTED_AT,
        )
        assert outcome.replay is False
        assert not hasattr(outcome, "submission_id")
    evidence = operator_evidence_for_frontier(operator_store, target)
    assert evidence is not None and evidence.enabled is True
    assert not hasattr(evidence, "state")
    assert not hasattr(operator_store, "mark_dispatching")
    assert not hasattr(operator_store, "prepare")
    assert not hasattr(operator_store, "claim")

    assert exec_store.read(envelope.submission_id, target) == (
        before_record,
        before_receipt,
    )
    assert exec_store.read_audit(envelope.submission_id, target) == before_audit
    operator_store.close()
    exec_store.close()


def test_capability_attribute_tripwire() -> None:
    import ast

    forbidden_attributes = (
        "subprocess",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "websocket",
        "keyring",
        "secrets",
        "random",
        "uuid4",
    )
    for path in (_ROOT / "src/quantforge/operator").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.level == 0
            ):
                imported.add(node.module.split(".", 1)[0])
            if isinstance(node, ast.Attribute):
                assert not (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "os"
                    and node.attr in ("environ", "getenv", "system")
                ), (path.name, node.attr)
        for token in forbidden_attributes:
            assert token not in imported, (path.name, token)


_OPERATOR_IMPORT_ALLOWLIST = {
    "__future__",
    "ast",
    "collections",
    "dataclasses",
    "datetime",
    "enum",
    "hashlib",
    "json",
    "os",
    "pathlib",
    "re",
    "sqlite3",
    "stat",
    "types",
    "typing",
    "pydantic",
    "quantforge",
}


def test_closed_import_allowlist() -> None:
    import ast

    for path in (_ROOT / "src/quantforge/operator").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.level == 0
            ):
                roots.add(node.module.split(".", 1)[0])
        assert roots <= _OPERATOR_IMPORT_ALLOWLIST, (path.name, roots)


def _dispatch_source(body: str) -> dict[str, str]:
    return {
        _P: _FE
        + _DS
        + _HELP
        + "def dispatch(store: SQLiteExecutionStateStore, target, decision, **kw):\n"
        + body
    }


@pytest.mark.parametrize(
    "body",
    [
        # DISPATCH-B
        (
            "    decision = decision.model_copy(update={'eligible': True})\n"
            "    return store.mark_dispatching(decision, owner_id='o', token='t')\n"
        ),
        # DISPATCH-C
        (
            "    decision = decision.model_copy()\n"
            "    return store.mark_dispatching(decision, owner_id='o', token='t')\n"
        ),
        # DISPATCH-D
        (
            "    decision = type(decision).from_json(decision.model_dump_json())\n"
            "    return store.mark_dispatching(decision, owner_id='o', token='t')\n"
        ),
        # DISPATCH-E
        (
            "    decision = type(decision).model_validate(decision.model_dump())\n"
            "    return store.mark_dispatching(decision, owner_id='o', token='t')\n"
        ),
        # DISPATCH-F
        (
            "    decision = type(decision).model_validate_json(decision.model_dump_json())\n"
            "    return store.mark_dispatching(decision, owner_id='o', token='t')\n"
        ),
        # DISPATCH-G
        (
            "    decision = type(decision).model_construct(**decision.model_dump())\n"
            "    return store.mark_dispatching(decision, owner_id='o', token='t')\n"
        ),
        # DISPATCH-J
        (
            "    decision = helper(decision)\n"
            "    return store.mark_dispatching(decision, owner_id='o', token='t')\n"
        ),
        # DISPATCH-K
        (
            "    box = [decision]\n"
            "    return store.mark_dispatching(box[0], owner_id='o', token='t')\n"
        ),
        # DISPATCH-L
        (
            "    holder.value = decision\n"
            "    return store.mark_dispatching(holder.value, owner_id='o', token='t')\n"
        ),
        # DISPATCH-M
        (
            "    picked = decision if kw.get('flag') else other\n"
            "    return store.mark_dispatching(picked, owner_id='o', token='t')\n"
        ),
        # DISPATCH-N
        (
            "    decision = other\n"
            "    return store.mark_dispatching(decision, owner_id='o', token='t')\n"
        ),
    ],
)
def test_dispatch_decision_origin_rejects(body: str) -> None:
    assert _c(_dispatch_source(body)[_P])


def test_dispatch_historical_read_only_is_outside_tripwire_c() -> None:
    source = (
        _FE
        + _DS
        + _HELP
        + "def inspect(store: SQLiteExecutionStateStore, decision, raw, **kw):\n"
        + "    historical = type(decision).from_json(raw)\n"
        + "    return historical.operator_evidence\n"
    )
    assert _c(source) == ()


def test_boundary_d_recovery_and_reads_stay_outside_dispatch_authority() -> None:
    source = (_ROOT / "src/quantforge/execstate/store.py").read_text(
        encoding="utf-8"
    )
    result = execstate_topology(source)
    assert result.exact is True
    assert len(result.actual) == 31
    assert result.missing == frozenset()
    assert result.extra == frozenset()
