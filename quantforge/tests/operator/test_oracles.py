"""Representative mandatory oracle-family tests for OPERATOR-001."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from quantforge.operator import (
    OperatorArgumentError,
    SQLiteOperatorControlStore,
    operator_evidence_for_frontier,
)
from quantforge.operator.static_checks import (
    execstate_topology,
    scan_tripwire_b,
    scan_tripwire_c,
)

from ._helpers import (
    COMMITTED_AT,
    AcceptingVerifier,
    make_policy,
    make_proof,
    make_request,
    make_target,
    store_path,
)

_ROOT = Path(__file__).resolve().parents[2]


# STORE-A..F ---------------------------------------------------------------


def test_store_a_canonical_store_is_accepted(tmp_path) -> None:
    path = store_path(tmp_path)
    request = make_request()
    proof = make_proof(request)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    ) as store:
        store.request_control_change(
            request, proof, committed_at=COMMITTED_AT
        )
        evidence = operator_evidence_for_frontier(store, make_target())
        assert evidence is not None
        assert evidence.enabled is True


@pytest.mark.parametrize(
    "fake",
    [
        object(),
        type(
            "FakeStore",
            (),
            {"produce_operator_evidence": lambda self, target: None},
        )(),
    ],
)
def test_store_b_c_e_fake_or_proxy_is_rejected(fake: object) -> None:
    with pytest.raises(OperatorArgumentError):
        operator_evidence_for_frontier(fake, make_target())  # type: ignore[arg-type]


def test_store_d_subclass_is_rejected(tmp_path) -> None:
    class Subclass(SQLiteOperatorControlStore):
        pass

    path = store_path(tmp_path)
    subclass = Subclass(path, policy=make_policy())
    try:
        with pytest.raises(OperatorArgumentError):
            operator_evidence_for_frontier(subclass, make_target())
    finally:
        subclass.close()


# DISPATCH-A..R and INJECT-K ------------------------------------------------


def _dispatch_source(body: str) -> dict[str, str]:
    return {
        "src/quantforge/transport/send.py": f"""
from quantforge.frontier.evaluate import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
from quantforge.execstate.store import SQLiteExecutionStateStore
def dispatch(store: SQLiteExecutionStateStore, target, **kwargs):
{body}
"""
    }


def test_dispatch_a_p_direct_decision_is_accepted() -> None:
    source = _dispatch_source(
        """
    return store.mark_dispatching(
        evaluate_frontier_eligibility(
            ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
        ),
        owner_id="o",
        token="t",
    )
"""
    )
    assert scan_tripwire_c(source) == ()


@pytest.mark.parametrize(
    "body",
    [
        """
    decision = evaluate_frontier_eligibility(
        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
    )
    decision = decision.model_copy(update={"eligible": True})
    return store.mark_dispatching(decision, owner_id="o", token="t")
""",
        """
    decision = evaluate_frontier_eligibility(
        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
    )
    decision = decision.model_copy()
    return store.mark_dispatching(decision, owner_id="o", token="t")
""",
        """
    decision = evaluate_frontier_eligibility(
        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
    )
    object.__setattr__(decision, "eligible", True)
    return store.mark_dispatching(decision, owner_id="o", token="t")
""",
    ],
)
def test_dispatch_b_q_r_mutations_are_rejected(body: str) -> None:
    violations = scan_tripwire_c(_dispatch_source(body))
    assert violations
    assert violations[0].code == "TRIPWIRE-C"


def test_inject_k_copied_decision_is_rejected() -> None:
    source = _dispatch_source(
        """
    decision = evaluate_frontier_eligibility(
        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
    )
    decision = decision.model_copy(update={"eligible": True})
    return store.mark_dispatching(decision, owner_id="o", token="t")
"""
    )
    assert scan_tripwire_c(source)


# EVIDENCE-MUT-A..J ---------------------------------------------------------


def _evidence_source(mutation: str) -> dict[str, str]:
    return {
        "src/quantforge/transport/compose.py": f"""
from quantforge.frontier import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
def compose(store, target, **kwargs):
    evidence = operator_evidence_for_frontier(store, target)
{mutation}
    return evaluate_frontier_eligibility(
        ..., operator_evidence=evidence, **kwargs
    )
"""
    }


def test_evidence_mut_b_untouched_is_accepted() -> None:
    assert scan_tripwire_b(_evidence_source("    pass")) == ()


@pytest.mark.parametrize(
    "mutation",
    [
        '    object.__setattr__(evidence, "enabled", True)',
        '    evidence.__dict__["maximum_age"] = kwargs["forged"]',
        '    vars(evidence)["enabled"] = True',
        '    evidence.__dict__.update({"enabled": True})',
        '    setattr(evidence, "enabled", True)',
        '    delattr(evidence, "maximum_age")',
        '    alias = evidence',
        '    copied = evidence.model_copy(update={"enabled": True})',
        '    reconstructed = type(evidence)(**evidence.model_dump())',
    ],
)
def test_evidence_mut_c_i_mutations_are_rejected(mutation: str) -> None:
    violations = scan_tripwire_b(_evidence_source(mutation))
    assert violations
    assert violations[0].code == "TRIPWIRE-B"


# SYMBOL-A..O ---------------------------------------------------------------


def test_symbol_a_d_known_reexport_resolves() -> None:
    source = """
from quantforge.frontier import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
def compose(store, target, **kwargs):
    evidence = operator_evidence_for_frontier(store, target)
    return evaluate_frontier_eligibility(
        ..., operator_evidence=evidence, **kwargs
    )
"""
    assert scan_tripwire_b({"src/quantforge/transport/compose.py": source}) == ()


@pytest.mark.parametrize(
    ("imports", "helper", "evaluator"),
    [
        (
            (
                "import quantforge.operator.store as opstore\n"
                "import quantforge.frontier.evaluate as fe"
            ),
            "opstore.operator_evidence_for_frontier",
            "fe.evaluate_frontier_eligibility",
        ),
        (
            (
                "from quantforge.operator.store import operator_evidence_for_frontier\n"
                "from quantforge.frontier.evaluate import evaluate_frontier_eligibility"
            ),
            "operator_evidence_for_frontier",
            "evaluate_frontier_eligibility",
        ),
    ],
)
def test_symbol_b_c_import_alias_forms_resolve(
    imports: str,
    helper: str,
    evaluator: str,
) -> None:
    source = f"""
    {imports}
    def compose(store, target, **kwargs):
        evidence = {helper}(store, target)
        return {evaluator}(
            ..., operator_evidence=evidence, **kwargs
        )
    """
    assert scan_tripwire_b({"src/quantforge/transport/compose.py": source}) == ()


@pytest.mark.parametrize(
    "value",
    [
        "getattr(operator_evidence_for_frontier, '__call__')(store, target)",
        "functools.partial(operator_evidence_for_frontier, store, target)()",
        "[operator_evidence_for_frontier][0](store, target)",
        "(operator_evidence_for_frontier if flag else other)(store, target)",
    ],
)
def test_symbol_e_i_dynamic_forms_are_rejected(value: str) -> None:
    source = f"""
from quantforge.frontier import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
def compose(store, target, **kwargs):
    return evaluate_frontier_eligibility(
        ..., operator_evidence={value}, **kwargs
    )
"""
    assert scan_tripwire_b({"src/quantforge/transport/compose.py": source})


# CANDIDATE-A..H ------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "helper(store, target)",
        "bound(store, target)",
        "getattr(store, 'produce')(target)",
        "partial(helper, store, target)()",
        "container[0](store, target)",
        "(helper if flag else other)(store, target)",
        "unknown(store, target)",
    ],
)
def test_candidate_a_f_h_unknown_callables_are_rejected(value: str) -> None:
    source = f"""
from quantforge.frontier import evaluate_frontier_eligibility
def compose(store, target, helper, bound, partial, container, unknown, flag, other, **kwargs):
    return evaluate_frontier_eligibility(
        ..., operator_evidence={value}, **kwargs
    )
"""
    assert scan_tripwire_b({"src/quantforge/transport/compose.py": source})


# BOUNDARY-A..F -------------------------------------------------------------


def test_boundary_external_private_writer_is_rejected() -> None:
    source = """
def bad(store):
    return store._write_audit(...)
"""
    violations = scan_tripwire_c(
        {"src/quantforge/broker/bad.py": source}
    )
    assert violations
    assert violations[0].code == "BOUNDARY-PRIVATE-WRITER"


def test_boundary_f_exact_topology() -> None:
    source = (_ROOT / "src/quantforge/execstate/store.py").read_text(
        encoding="utf-8"
    )
    result = execstate_topology(source)
    assert result.exact is True
    assert len(result.actual) == 31
    assert len(result.approved) == 31


# Capability and sentinel boundaries ---------------------------------------


def test_operator_package_has_no_forbidden_capability_import() -> None:
    forbidden = {
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "websocket",
        "subprocess",
        "keyring",
        "secrets",
        "random",
        "uuid",
    }
    for path in (_ROOT / "src/quantforge/operator").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            assert not (names & forbidden), (path, names & forbidden)


def test_secret_sentinel_is_rejected_before_durable_write(tmp_path) -> None:
    sentinel = "__OPCTL_SENTINEL_TESTVALUE__"
    path = store_path(tmp_path)
    with SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    ) as store:
        with pytest.raises(OperatorArgumentError):
            make_request(request_id=sentinel)
        assert store.read_control_history() == ()
    assert sentinel.encode("utf-8") not in path.read_bytes()
