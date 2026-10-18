"""CH-1 / CH-2 candidate-discovery and provenance matrices."""

from __future__ import annotations

from quantforge.operator.static_checks import (
    scan_tripwire_a,
    scan_tripwire_b,
    scan_tripwire_c,
)

_PATH = "src/quantforge/transport/probe.py"


def _b(source: str):
    return scan_tripwire_b({_PATH: source})


def _c(source: str):
    return scan_tripwire_c({_PATH: source})


def _a(source: str):
    return scan_tripwire_a({_PATH: source})


# CANDIDATE-A..H / SYMBOL-A..O ---------------------------------------------


def test_candidate_a_callable_alias_is_discovered_and_rejected() -> None:
    source = """
from quantforge.frontier import evaluate_frontier_eligibility
f = evaluate_frontier_eligibility
def go(**kwargs):
    return f(..., operator_evidence=forged, **kwargs)
"""
    assert _b(source)


def test_candidate_b_module_level_evaluator_is_discovered_and_rejected() -> None:
    source = """
from quantforge.frontier import evaluate_frontier_eligibility
EV = evaluate_frontier_eligibility(..., operator_evidence=forged)
"""
    assert _b(source)


def test_candidate_c_lambda_held_evaluator_is_discovered_and_rejected() -> None:
    source = """
from quantforge.frontier import evaluate_frontier_eligibility
f = lambda forged: evaluate_frontier_eligibility(..., operator_evidence=forged)
"""
    assert _b(source)


def test_candidate_d_getattr_evaluator_is_discovered_and_rejected() -> None:
    source = """
import quantforge.frontier as F
ev = getattr(F, "evaluate_frontier_eligibility")
def go(**kwargs):
    return ev(..., operator_evidence=forged, **kwargs)
"""
    assert _b(source)


def test_candidate_e_getattr_dispatch_is_discovered_and_rejected() -> None:
    source = """
def go(store, dec):
    md = getattr(store, "mark_dispatching")
    return md(dec)
"""
    assert _c(source)


def test_candidate_f_module_level_dispatch_is_discovered_and_rejected() -> None:
    source = """
import quantforge.execstate as EX
S = EX.SQLiteExecutionStateStore("x.sqlite")
S.mark_dispatching(dec)
"""
    assert _c(source)


def test_candidate_g_partial_dispatch_is_discovered_and_rejected() -> None:
    source = """
import functools
def go(store, dec):
    return functools.partial(store.mark_dispatching, dec)()
"""
    assert _c(source)


def test_candidate_h_container_and_conditional_are_discovered_and_rejected() -> None:
    container = """
def go(store, dec):
    return [store.mark_dispatching][0](dec)
"""
    conditional = """
def go(store, dec, flag):
    md = store.mark_dispatching
    return (md if flag else other)(dec)
"""
    assert _c(container)
    assert _c(conditional)


def test_symbol_e_i_callable_alias_and_dynamic_lookup_reject() -> None:
    cases = [
        (
            "from quantforge.frontier import evaluate_frontier_eligibility\n"
            "f = evaluate_frontier_eligibility\n"
            "def go(forged):\n"
            "    return f(..., operator_evidence=forged)\n"
        ),
        (
            "def go(forged):\n"
            "    return getattr(m, 'evaluate_frontier_eligibility')(..., operator_evidence=forged)\n"
        ),
    ]
    for source in cases:
        assert _b(source)


def test_symbol_j_star_import_is_ambiguous() -> None:
    source = """
from quantforge.frontier import *
def go(**kwargs):
    return evaluate_frontier_eligibility(..., operator_evidence=evidence, **kwargs)
"""
    assert _b(source)


def test_symbol_k_direct_import_and_module_alias_resolve() -> None:
    direct = """
from quantforge.frontier import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
def compose(store, target, **kwargs):
    evidence = operator_evidence_for_frontier(store, target)
    return evaluate_frontier_eligibility(
        ..., operator_evidence=evidence, **kwargs
    )
"""
    module_alias = """
import quantforge.frontier.evaluate as fe
import quantforge.operator.store as opstore
def compose(store, target, **kwargs):
    evidence = opstore.operator_evidence_for_frontier(store, target)
    return fe.evaluate_frontier_eligibility(
        ..., operator_evidence=evidence, **kwargs
    )
"""
    assert _b(direct) == ()
    assert _b(module_alias) == ()


def test_symbol_l_unknown_mark_dispatching_receiver_rejects() -> None:
    source = """
def go(store, dec):
    return store.mark_dispatching(dec)
"""
    assert _c(source)


def test_symbol_m_n_constructor_and_evaluator_alias_reject() -> None:
    construction = """
from quantforge.frontier.models import OperatorExecutionEnableEvidence
E = OperatorExecutionEnableEvidence
def go(values):
    return E(**values)
"""
    evaluator = """
from quantforge.frontier import evaluate_frontier_eligibility
f = evaluate_frontier_eligibility
def go(forged):
    return f(..., operator_evidence=forged)
"""
    assert _a(construction)
    assert _b(evaluator)


def test_symbol_o_dispatch_consumer_alias_rejects() -> None:
    source = """
from quantforge.execstate.store import SQLiteExecutionStateStore
def go(store: SQLiteExecutionStateStore, dec):
    md = store.mark_dispatching
    return md(dec)
"""
    assert _c(source)


def test_supported_direct_and_reexport_forms_remain_accepted() -> None:
    source = """
from quantforge.frontier.evaluate import evaluate_frontier_eligibility
from quantforge.execstate.store import SQLiteExecutionStateStore
from quantforge.operator import operator_evidence_for_frontier
def dispatch(store: SQLiteExecutionStateStore, target, **kwargs):
    return store.mark_dispatching(
        evaluate_frontier_eligibility(
            ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
        ),
        owner_id="o",
        token="t",
    )
"""
    assert _c(source) == ()


# EVIDENCE-MUT / DISPATCH same-call provenance -----------------------------


def test_same_call_evidence_alias_and_mutation_reject() -> None:
    for extra in ("other=mutate(e)", "other=[e]"):
        source = (
            "from quantforge.frontier import evaluate_frontier_eligibility\n"
            "from quantforge.operator import operator_evidence_for_frontier\n"
            "def compose(store, target, **kwargs):\n"
            "    e = operator_evidence_for_frontier(store, target)\n"
            "    return evaluate_frontier_eligibility(\n"
            f"        ..., operator_evidence=e, {extra}, **kwargs\n"
            "    )\n"
        )
        assert _b(source), extra


def test_binding_after_evaluator_call_rejects() -> None:
    source = """
from quantforge.frontier import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
def compose(store, target, **kwargs):
    result = evaluate_frontier_eligibility(..., operator_evidence=e, **kwargs)
    e = operator_evidence_for_frontier(store, target)
    return result
"""
    assert _b(source)


def test_same_call_decision_alias_and_mutation_reject() -> None:
    for extra in ("other=mutate(decision)", "other=[decision]"):
        source = (
            "from quantforge.frontier.evaluate import evaluate_frontier_eligibility\n"
            "from quantforge.execstate.store import SQLiteExecutionStateStore\n"
            "from quantforge.operator import operator_evidence_for_frontier\n"
            "def dispatch(store: SQLiteExecutionStateStore, target, **kwargs):\n"
            "    decision = evaluate_frontier_eligibility(\n"
            "        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs\n"
            "    )\n"
            "    return store.mark_dispatching(decision, " + extra + ")\n"
        )
        assert _c(source), extra


def test_decision_binding_after_dispatch_call_rejects() -> None:
    source = """
from quantforge.frontier.evaluate import evaluate_frontier_eligibility
from quantforge.execstate.store import SQLiteExecutionStateStore
from quantforge.operator import operator_evidence_for_frontier
def dispatch(store: SQLiteExecutionStateStore, target, **kwargs):
    result = store.mark_dispatching(decision, owner_id="o", token="t")
    decision = evaluate_frontier_eligibility(
        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
    )
    return result
"""
    assert _c(source)


def test_valid_single_binding_forms_remain_accepted() -> None:
    source = """
from quantforge.frontier import evaluate_frontier_eligibility
from quantforge.operator import operator_evidence_for_frontier
def compose(store, target, **kwargs):
    evidence = operator_evidence_for_frontier(store, target)
    return evaluate_frontier_eligibility(
        ..., operator_evidence=evidence, **kwargs
    )
"""
    assert _b(source) == ()


def test_supported_reexport_modules_remain_accepted_for_dispatch() -> None:
    source = """
from quantforge.frontier.evaluate import evaluate_frontier_eligibility
from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.operator import operator_evidence_for_frontier
def dispatch(store: SQLiteExecutionStateStore, target, **kwargs):
    decision = evaluate_frontier_eligibility(
        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kwargs
    )
    return store.mark_dispatching(decision, owner_id="o", token="t")
"""
    assert _c(source) == ()
