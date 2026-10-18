"""Tripwire A/B/C and EXECSTATE topology guard tests."""

from __future__ import annotations

from pathlib import Path

from quantforge.operator.static_checks import (
    execstate_topology,
    scan_repository,
    scan_tripwire_a,
    scan_tripwire_b,
    scan_tripwire_c,
)

_ROOT = Path(__file__).resolve().parents[2]


def test_real_repository_tripwires_are_green() -> None:
    violations = scan_repository(_ROOT / "src")
    assert violations == ()


def test_tripwire_a_allows_frozen_category_2() -> None:
    source = """
from quantforge.frontier.models import OperatorExecutionEnableEvidence
def _parse_frontier_model(values):
    return OperatorExecutionEnableEvidence(**values)
"""
    violations = scan_tripwire_a(
        {"src/quantforge/frontier/models.py": source}
    )
    assert violations == ()


def test_tripwire_a_rejects_other_construction() -> None:
    source = """
from quantforge.frontier.models import OperatorExecutionEnableEvidence
def build(values):
    return OperatorExecutionEnableEvidence(**values)
"""
    violations = scan_tripwire_a({"src/quantforge/broker/bad.py": source})
    assert len(violations) == 1
    assert violations[0].code == "TRIPWIRE-A"


def test_tripwire_b_allows_direct_helper() -> None:
    source = """
from quantforge.frontier import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
def compose(store, target, **kwargs):
    return evaluate_frontier_eligibility(
        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
    )
"""
    assert scan_tripwire_b({"src/quantforge/transport/compose.py": source}) == ()


def test_tripwire_b_allows_untouched_single_binding() -> None:
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


def test_tripwire_b_rejects_b5_1_mutation() -> None:
    source = """
from quantforge.frontier import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
def compose(store, target, **kwargs):
    evidence = operator_evidence_for_frontier(store, target)
    object.__setattr__(evidence, "enabled", True)
    return evaluate_frontier_eligibility(
        ..., operator_evidence=evidence, **kwargs
    )
"""
    violations = scan_tripwire_b({"src/quantforge/transport/compose.py": source})
    assert len(violations) == 1
    assert violations[0].code == "TRIPWIRE-B"


def test_tripwire_b_rejects_dict_mutation() -> None:
    source = """
from quantforge.frontier import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
def compose(store, target, **kwargs):
    evidence = operator_evidence_for_frontier(store, target)
    evidence.__dict__["maximum_age"] = kwargs["forged"]
    return evaluate_frontier_eligibility(
        ..., operator_evidence=evidence, **kwargs
    )
"""
    violations = scan_tripwire_b({"src/quantforge/transport/compose.py": source})
    assert len(violations) == 1


def test_tripwire_c_allows_direct_decision() -> None:
    source = """
from quantforge.frontier.evaluate import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
from quantforge.execstate.store import SQLiteExecutionStateStore
def dispatch(store: SQLiteExecutionStateStore, target, **kwargs):
    return store.mark_dispatching(
        evaluate_frontier_eligibility(
            ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
        ),
        owner_id="o",
        token="t",
    )
"""
    assert scan_tripwire_c({"src/quantforge/transport/send.py": source}) == ()


def test_tripwire_c_rejects_copied_decision() -> None:
    source = """
from quantforge.frontier.evaluate import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
from quantforge.execstate.store import SQLiteExecutionStateStore
def dispatch(store: SQLiteExecutionStateStore, target, **kwargs):
    decision = evaluate_frontier_eligibility(
        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
    )
    decision = decision.model_copy(update={"eligible": True})
    return store.mark_dispatching(decision, owner_id="o", token="t")
"""
    violations = scan_tripwire_c({"src/quantforge/transport/send.py": source})
    assert len(violations) == 1
    assert violations[0].code == "TRIPWIRE-C"


def test_execstate_topology_is_exactly_31_edges() -> None:
    source = (_ROOT / "src/quantforge/execstate/store.py").read_text(
        encoding="utf-8"
    )
    result = execstate_topology(source)
    assert result.exact is True
    assert len(result.actual) == 31
    assert len(result.approved) == 31
    assert result.missing == frozenset()
    assert result.extra == frozenset()
