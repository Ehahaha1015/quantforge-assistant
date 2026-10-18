"""OPERATOR-001 Implementation Repair #4 static provenance invariants.

Covers the Review #4 mandatory findings: returned-value provenance,
container/attribute mutation provenance and duplicate closure-name ambiguity.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from quantforge.operator.static_checks import (
    scan_tripwire_a,
    scan_tripwire_b,
    scan_tripwire_c,
)

_ROOT = Path(__file__).resolve().parents[2]
_P = "src/quantforge/transport/probe.py"
_FRONTIER_MODELS = "src/quantforge/frontier/models.py"
_OPERATOR_STORE = "src/quantforge/operator/store.py"
_EV = "from quantforge.frontier import evaluate_frontier_eligibility\n"
_FE = "from quantforge.frontier.evaluate import evaluate_frontier_eligibility\n"
_EC = "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
_DS = "from quantforge.execstate.store import SQLiteExecutionStateStore\n"
_HELP = "from quantforge.operator import operator_evidence_for_frontier\n"


def _src(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _b(source: str) -> tuple:
    return scan_tripwire_b({_P: source})


def _a(source: str, path: str = _P) -> tuple:
    return scan_tripwire_a({path: source})


def _c(source: str) -> tuple:
    return scan_tripwire_c({_P: source})


# ---------------------------------------------------------------------------
# Finding 1 - returned-value provenance (Tripwire B)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "def get():",
            "    x = evaluate_frontier_eligibility",
            "    return x",
            "def f(raw, **kw):",
            "    y = get()",
            "    return y(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def get():",
            "    x = evaluate_frontier_eligibility",
            "    return x",
            "def f(raw, **kw):",
            "    first = get()",
            "    second = first",
            "    return second(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "make = lambda: evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    y = make()",
            "    return y(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def make():",
            "    return evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    return make()(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def outer(flag):",
            "    def inner():",
            "        return evaluate_frontier_eligibility",
            "    return inner if flag else None",
            "def f(raw, **kw):",
            "    return outer(True)()(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def pick(a, b, flag):",
            "    return a if flag else b",
            "def f(raw, flag, other, **kw):",
            "    return pick(evaluate_frontier_eligibility, other, flag)(",
            "        ..., operator_evidence=raw, **kw",
            "    )",
        ),
        _src(
            "def make():",
            "    return (evaluate_frontier_eligibility,)",
            "def f(raw, **kw):",
            "    return make()[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def make():",
            "    return [evaluate_frontier_eligibility]",
            "def f(raw, **kw):",
            "    return make()[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def make():",
            "    return {'k': evaluate_frontier_eligibility}",
            "def f(raw, **kw):",
            "    return make()['k'](..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_returned_value_provenance_rejects(body: str) -> None:
    assert _b(_EV + body)


def test_returned_value_provenance_tripwire_a_rejects() -> None:
    body = _src(
        "def make():",
        "    E = OperatorExecutionEnableEvidence",
        "    return E",
        "def f(values):",
        "    F = make()",
        "    return F(**values)",
    )
    assert _a(_EC + body)


def test_returned_value_provenance_tripwire_c_rejects() -> None:
    body = _src(
        "def make(store: SQLiteExecutionStateStore):",
        "    md = store.mark_dispatching",
        "    return md",
        "def f(store: SQLiteExecutionStateStore, dec):",
        "    fn = make(store)",
        "    return fn(dec)",
    )
    assert _c(_DS + body)


def test_returned_value_chain_terminates_deterministically() -> None:
    chain = [f"    a{index} = a{index - 1}" for index in range(1, 60)]
    body = _src(
        "def get():",
        "    a0 = evaluate_frontier_eligibility",
        *chain,
        "    return a59",
        "def f(raw, **kw):",
        "    return get()(..., operator_evidence=raw, **kw)",
    )
    start = time.perf_counter()
    results = {bool(_b(_EV + body)) for _ in range(5)}
    assert time.perf_counter() - start < 10
    assert results == {True}


# ---------------------------------------------------------------------------
# Finding 2 - container / attribute mutation provenance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "def f(raw, **kw):",
            "    ev = evaluate_frontier_eligibility",
            "    table = []",
            "    table.append(ev)",
            "    return table[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    ev = evaluate_frontier_eligibility",
            "    table = {}",
            "    table['k'] = ev",
            "    return table['k'](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, holder, **kw):",
            "    ev = evaluate_frontier_eligibility",
            "    holder.ev = ev",
            "    return holder.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "class C:",
            "    ev = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    return C.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "class C:",
            "    TABLE = [evaluate_frontier_eligibility]",
            "def f(raw, **kw):",
            "    return C.TABLE[0](..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_container_attribute_provenance_rejects(body: str) -> None:
    assert _b(_EV + body)


@pytest.mark.parametrize(
    ("tripwire", "body"),
    [
        (
            "A",
            _src(
                "def f(values):",
                "    E = OperatorExecutionEnableEvidence",
                "    table = []",
                "    table.append(E)",
                "    return table[0](**values)",
            ),
        ),
        (
            "A",
            _src(
                "class C:",
                "    E = OperatorExecutionEnableEvidence",
                "def f(values):",
                "    return C.E(**values)",
            ),
        ),
        (
            "C",
            _src(
                "def f(store: SQLiteExecutionStateStore, dec, holder):",
                "    md = store.mark_dispatching",
                "    holder.md = md",
                "    return holder.md(dec)",
            ),
        ),
        (
            "C",
            _src(
                "class C:",
                "    TABLE = [SQLiteExecutionStateStore.mark_dispatching]",
                "def f(store: SQLiteExecutionStateStore, dec):",
                "    return C.TABLE[0](store, dec)",
            ),
        ),
    ],
)
def test_container_attribute_provenance_a_c_rejects(
    tripwire: str, body: str
) -> None:
    prefix = _EC if tripwire == "A" else _DS
    scanner = _a if tripwire == "A" else _c
    assert scanner(prefix + body)


# ---------------------------------------------------------------------------
# Finding 3 - duplicate closure-name ambiguity
# ---------------------------------------------------------------------------


def _ambiguous_closure(duplicate_wrapper: str) -> str:
    return _src(
        "def outer():",
        "    def inner():",
        "        return evaluate_frontier_eligibility",
        "    return inner",
        *duplicate_wrapper.splitlines(),
        "def f(raw, **kw):",
        "    ev = outer()()",
        "    return ev(..., operator_evidence=raw, **kw)",
    )


def test_duplicate_nested_name_still_rejects() -> None:
    duplicate = _src(
        "def unrelated():",
        "    def inner():",
        "        return None",
        "    return inner",
    )
    assert _b(_EV + _ambiguous_closure(duplicate))


def test_benign_duplicate_nested_name_is_not_authority() -> None:
    source = _src(
        "def outer():",
        "    def inner():",
        "        return len",
        "    return inner",
        "def unrelated():",
        "    def inner():",
        "        return None",
        "    return inner",
        "def f(values):",
        "    fn = outer()()",
        "    return fn(values)",
    )
    assert _b(source) == ()


def test_ambiguous_constructor_closure_rejects() -> None:
    body = _src(
        "def outer():",
        "    def inner():",
        "        return OperatorExecutionEnableEvidence",
        "    return inner",
        "def unrelated():",
        "    def inner():",
        "        return None",
        "    return inner",
        "def f(values):",
        "    E = outer()()",
        "    return E(**values)",
    )
    assert _a(_EC + body)


# ---------------------------------------------------------------------------
# Positive controls
# ---------------------------------------------------------------------------


def test_positive_controls_remain_accepted() -> None:
    direct = (
        _EV
        + _HELP
        + "def f(store, target, **kw):\n"
        "    return evaluate_frontier_eligibility(\n"
        "        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kw\n"
        "    )\n"
    )
    binding = (
        _EV
        + _HELP
        + "def f(store, target, **kw):\n"
        "    evidence = operator_evidence_for_frontier(store, target)\n"
        "    return evaluate_frontier_eligibility(..., operator_evidence=evidence, **kw)\n"
    )
    decision = (
        _FE
        + _DS
        + _HELP
        + "def d(store: SQLiteExecutionStateStore, target, **kw):\n"
        "    dec = evaluate_frontier_eligibility(\n"
        "        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kw\n"
        "    )\n"
        "    return store.mark_dispatching(dec, owner_id='o', token='t')\n"
    )
    assert _b(direct) == ()
    assert _b(binding) == ()
    assert _c(decision) == ()


def test_frozen_allowlist_sites_remain_accepted() -> None:
    reconstruction = _src(
        "def _parse_frontier_model(values):",
        "    return OperatorExecutionEnableEvidence(**values)",
    )
    producer = _src(
        "def operator_evidence_for_frontier(store, target):",
        "    return OperatorExecutionEnableEvidence(**values)",
    )
    assert _a(_EC + reconstruction, _FRONTIER_MODELS) == ()
    assert _a(_EC + producer, _OPERATOR_STORE) == ()


@pytest.mark.parametrize(
    "source",
    [
        _src(
            "def make():",
            "    return len",
            "def f(values):",
            "    fn = make()",
            "    return fn(values)",
        ),
        _src(
            "def make():",
            "    return (len,)",
            "def f(values):",
            "    return make()[0](values)",
        ),
        _src(
            "def make():",
            "    return {'k': len}",
            "def f(values):",
            "    return make()['k'](values)",
        ),
        _src(
            "def f(cb):",
            "    table = []",
            "    table.append(cb)",
            "    return table[0]()",
        ),
        _src(
            "def f(cb):",
            "    table = {}",
            "    table['k'] = cb",
            "    return table['k']()",
        ),
        _src(
            "def f(cb, holder):",
            "    holder.fn = cb",
            "    return holder.fn()",
        ),
        _src(
            "class C:",
            "    fn = len",
            "def f(values):",
            "    return C.fn(values)",
        ),
        _src(
            "class C:",
            "    TABLE = [len]",
            "def f(values):",
            "    return C.TABLE[0](values)",
        ),
        _src(
            "def pick(flag):",
            "    return len if flag else str",
            "def f(values):",
            "    return pick(True)(values)",
        ),
        "import functools\ndef f(fn, x):\n    return functools.partial(fn, x)()\n",
        "def f(a, b, flag):\n    g = a if flag else b\n    return g()\n",
    ],
)
def test_unrelated_composed_python_remains_accepted(source: str) -> None:
    assert _b(source) == ()


def test_frozen_repository_tripwires_stay_green() -> None:
    sources = {
        str(path): path.read_text(encoding="utf-8")
        for path in (_ROOT / "src").rglob("*.py")
    }
    assert scan_tripwire_a(sources) == ()
    assert scan_tripwire_b(sources) == ()
    assert scan_tripwire_c(sources) == ()
