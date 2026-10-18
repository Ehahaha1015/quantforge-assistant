"""OPERATOR-001 Implementation Repair #5 root-invariant static tests.

Covers the four Review #5 mandatory findings as one bounded provenance
invariant: class-body alias environments, returned safety-named attributes,
call-valued function-local aliases and nested compound mutations.
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
_EV = "from quantforge.frontier import evaluate_frontier_eligibility\n"
_EC = "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
_DS = "from quantforge.execstate.store import SQLiteExecutionStateStore\n"


def _src(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _b(source: str) -> tuple:
    return scan_tripwire_b({_P: source})


def _a(source: str) -> tuple:
    return scan_tripwire_a({_P: source})


def _c(source: str) -> tuple:
    return scan_tripwire_c({_P: source})


# ---------------------------------------------------------------------------
# F1 - class-body alias environment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "ALIAS = evaluate_frontier_eligibility",
            "class C:",
            "    ev = ALIAS",
            "def f(raw, **kw):",
            "    return C.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "ALIAS = evaluate_frontier_eligibility",
            "class C:",
            "    TABLE = [ALIAS]",
            "def f(raw, **kw):",
            "    return C.TABLE[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def outer():",
            "    alias = evaluate_frontier_eligibility",
            "    class C:",
            "        ev = alias",
            "    return C",
            "def f(raw, **kw):",
            "    return outer().ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def outer():",
            "    alias = evaluate_frontier_eligibility",
            "    class C:",
            "        TABLE = [alias]",
            "    return C",
            "def f(raw, **kw):",
            "    cls = outer()",
            "    return cls.TABLE[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def make():",
            "    return evaluate_frontier_eligibility",
            "class C:",
            "    ev = make()",
            "def f(raw, **kw):",
            "    return C.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def make():",
            "    return evaluate_frontier_eligibility",
            "class C:",
            "    TABLE = [make()]",
            "def f(raw, **kw):",
            "    return C.TABLE[0](..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_f1_class_body_alias_rejects(body: str) -> None:
    assert _b(_EV + body)


@pytest.mark.parametrize(
    ("tripwire", "body"),
    [
        (
            "A",
            _src(
                "ALIAS = OperatorExecutionEnableEvidence",
                "class C:",
                "    E = ALIAS",
                "def f(values):",
                "    return C.E(**values)",
            ),
        ),
        (
            "A",
            _src(
                "def make():",
                "    return OperatorExecutionEnableEvidence",
                "class C:",
                "    TABLE = [make()]",
                "def f(values):",
                "    return C.TABLE[0](**values)",
            ),
        ),
        (
            "C",
            _src(
                "ALIAS = SQLiteExecutionStateStore.mark_dispatching",
                "class C:",
                "    TABLE = [ALIAS]",
                "def f(store: SQLiteExecutionStateStore, dec):",
                "    return C.TABLE[0](store, dec)",
            ),
        ),
        (
            "C",
            _src(
                "def make(store: SQLiteExecutionStateStore):",
                "    return store.mark_dispatching",
                "class C:",
                "    md = make(None)",
                "def f(store: SQLiteExecutionStateStore, dec):",
                "    return C.md(store, dec)",
            ),
        ),
    ],
)
def test_f1_class_body_alias_a_c_rejects(tripwire: str, body: str) -> None:
    prefix = _EC if tripwire == "A" else _DS
    scanner = _a if tripwire == "A" else _c
    assert scanner(prefix + body)


def test_f1_unrelated_classes_do_not_contaminate() -> None:
    source = _src(
        "ALIAS = len",
        "class C1:",
        "    fn = ALIAS",
        "class C2:",
        "    fn = str",
        "def f(values):",
        "    return C1.fn(values)",
    )
    assert _b(source) == ()


def test_f1_benign_enclosing_class_alias_is_accepted() -> None:
    source = _src(
        "def outer():",
        "    alias = len",
        "    class C:",
        "        fn = alias",
        "    return C",
        "def f(values):",
        "    return outer().fn(values)",
    )
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# F2 - returned safety-named attribute
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "def get(holder):",
            "    return holder.evaluate_frontier_eligibility",
            "def f(holder, raw, **kw):",
            "    fn = get(holder)",
            "    return fn(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def get(holder):",
            "    return holder.evaluate_frontier_eligibility",
            "def f(holder, raw, **kw):",
            "    return get(holder)(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def get(holder):",
            "    return holder.evaluate_frontier_eligibility",
            "def f(holder, raw, **kw):",
            "    table = [get(holder)]",
            "    return table[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def get(holder):",
            "    return holder.evaluate_frontier_eligibility",
            "def f(holder, raw, **kw):",
            "    first = get(holder)",
            "    second = first",
            "    return second(..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_f2_returned_safety_attribute_b_rejects(body: str) -> None:
    assert _b(_EV + body)


def test_f2_returned_safety_attribute_a_rejects() -> None:
    body = _src(
        "def get(holder):",
        "    return holder.OperatorExecutionEnableEvidence",
        "def f(values, holder):",
        "    E = get(holder)",
        "    return E(**values)",
    )
    assert _a(_EC + body)


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "def get(store: SQLiteExecutionStateStore):",
            "    return store.mark_dispatching",
            "def f(store: SQLiteExecutionStateStore, dec):",
            "    fn = get(store)",
            "    return fn(dec)",
        ),
        _src(
            "def get(store: SQLiteExecutionStateStore):",
            "    return store.mark_dispatching",
            "def f(store: SQLiteExecutionStateStore, dec):",
            "    return get(store)(dec)",
        ),
        _src(
            "def get(store: SQLiteExecutionStateStore):",
            "    return store.mark_dispatching",
            "def f(store: SQLiteExecutionStateStore, dec):",
            "    table = [get(store)]",
            "    return table[0](dec)",
        ),
    ],
)
def test_f2_returned_safety_attribute_c_rejects(body: str) -> None:
    assert _c(_DS + body)


def test_f2_benign_normal_attribute_is_accepted() -> None:
    source = _src(
        "def get(holder):",
        "    return holder.normal_method",
        "def f(holder, values):",
        "    fn = get(holder)",
        "    return fn(values)",
    )
    assert _b(source) == ()
    source_table = _src(
        "def get(holder):",
        "    return holder.normal_method",
        "def f(holder, values):",
        "    table = [get(holder)]",
        "    return table[0](values)",
    )
    assert _b(source_table) == ()


# ---------------------------------------------------------------------------
# F3 - call-valued function-local aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "def make():",
            "    return evaluate_frontier_eligibility",
            "def get():",
            "    x = make()",
            "    return x",
            "def f(raw, **kw):",
            "    y = get()",
            "    return y(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def make():",
            "    return evaluate_frontier_eligibility",
            "def get():",
            "    x = make()",
            "    y = x",
            "    return y",
            "def f(raw, **kw):",
            "    z = get()",
            "    return z(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def make():",
            "    return evaluate_frontier_eligibility",
            "def get(flag):",
            "    x = make()",
            "    y = x if flag else None",
            "    return y",
            "def f(raw, **kw):",
            "    z = get(True)",
            "    return z(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def make():",
            "    return evaluate_frontier_eligibility",
            "def get():",
            "    x = make()",
            "    return [x]",
            "def f(raw, **kw):",
            "    return get()[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def make():",
            "    return evaluate_frontier_eligibility",
            "def get():",
            "    x = make()",
            "    return {'k': x}",
            "def f(raw, **kw):",
            "    return get()['k'](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def make1():",
            "    return evaluate_frontier_eligibility",
            "def make2():",
            "    return make1()",
            "def get():",
            "    x = make2()",
            "    return x",
            "def f(raw, **kw):",
            "    y = get()",
            "    return y(..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_f3_helper_call_local_alias_rejects(body: str) -> None:
    assert _b(_EV + body)


def test_f3_helper_call_local_alias_a_c_reject() -> None:
    a_source = _src(
        "def make():",
        "    return OperatorExecutionEnableEvidence",
        "def get():",
        "    x = make()",
        "    return x",
        "def f(values):",
        "    E = get()",
        "    return E(**values)",
    )
    c_source = _src(
        "def make(store: SQLiteExecutionStateStore):",
        "    return store.mark_dispatching",
        "def get(store: SQLiteExecutionStateStore):",
        "    x = make(store)",
        "    return x",
        "def f(store: SQLiteExecutionStateStore, dec):",
        "    fn = get(store)",
        "    return fn(dec)",
    )
    assert _a(_EC + a_source)
    assert _c(_DS + c_source)


def test_f3_benign_helper_chain_is_accepted() -> None:
    source = _src(
        "def make():",
        "    return len",
        "def get():",
        "    x = make()",
        "    return x",
        "def f(values):",
        "    y = get()",
        "    return y(values)",
    )
    assert _b(source) == ()


def test_f3_helper_chain_terminates_deterministically() -> None:
    lines = ["from quantforge.frontier import evaluate_frontier_eligibility"]
    lines.append("def h0():\n    return evaluate_frontier_eligibility")
    for index in range(1, 40):
        lines.append(f"def h{index}():\n    x = h{index - 1}()\n    return x")
    lines.append(
        "def f(raw, **kw):\n    y = h39()\n"
        "    return y(..., operator_evidence=raw, **kw)"
    )
    source = "\n".join(lines) + "\n"
    start = time.perf_counter()
    results = {bool(_b(source)) for _ in range(3)}
    assert time.perf_counter() - start < 10
    assert results == {True}


# ---------------------------------------------------------------------------
# F4 - nested compound mutations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "def f(raw, flag, **kw):",
            "    ev = evaluate_frontier_eligibility",
            "    table = []",
            "    if flag:",
            "        table.append(ev)",
            "    return table[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, flag, **kw):",
            "    ev = evaluate_frontier_eligibility",
            "    table = []",
            "    if flag:",
            "        pass",
            "    else:",
            "        table.append(ev)",
            "    return table[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    ev = evaluate_frontier_eligibility",
            "    table = {}",
            "    for _ in range(1):",
            "        table['k'] = ev",
            "    return table['k'](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    ev = evaluate_frontier_eligibility",
            "    table = {}",
            "    while False:",
            "        table['k'] = ev",
            "    return table['k'](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, holder, **kw):",
            "    ev = evaluate_frontier_eligibility",
            "    try:",
            "        holder.ev = ev",
            "    except Exception:",
            "        pass",
            "    return holder.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, holder, ctx, **kw):",
            "    ev = evaluate_frontier_eligibility",
            "    with ctx:",
            "        holder.ev = ev",
            "    return holder.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, flag, **kw):",
            "    ev = evaluate_frontier_eligibility",
            "    table = []",
            "    if flag:",
            "        for _ in range(1):",
            "            if flag:",
            "                table.append(ev)",
            "    return table[0](..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_f4_nested_compound_mutation_rejects(body: str) -> None:
    assert _b(_EV + body)


@pytest.mark.parametrize(
    ("tripwire", "body"),
    [
        (
            "A",
            _src(
                "def f(values, flag):",
                "    E = OperatorExecutionEnableEvidence",
                "    table = []",
                "    if flag:",
                "        table.append(E)",
                "    return table[0](**values)",
            ),
        ),
        (
            "A",
            _src(
                "def f(values):",
                "    E = OperatorExecutionEnableEvidence",
                "    table = {}",
                "    for _ in range(1):",
                "        table['k'] = E",
                "    return table['k'](**values)",
            ),
        ),
        (
            "C",
            _src(
                "def f(store: SQLiteExecutionStateStore, dec, holder):",
                "    md = store.mark_dispatching",
                "    try:",
                "        holder.md = md",
                "    except Exception:",
                "        pass",
                "    return holder.md(dec)",
            ),
        ),
        (
            "C",
            _src(
                "def f(store: SQLiteExecutionStateStore, dec):",
                "    md = store.mark_dispatching",
                "    table = []",
                "    while False:",
                "        table.append(md)",
                "    return table[0](dec)",
            ),
        ),
    ],
)
def test_f4_nested_compound_a_c_rejects(tripwire: str, body: str) -> None:
    prefix = _EC if tripwire == "A" else _DS
    scanner = _a if tripwire == "A" else _c
    assert scanner(prefix + body)


@pytest.mark.parametrize(
    "source",
    [
        _src(
            "def f(cb, flag):",
            "    table = []",
            "    if flag:",
            "        table.append(cb)",
            "    return table[0]()",
        ),
        _src(
            "def f(cb, flag):",
            "    table = {}",
            "    if flag:",
            "        table['k'] = cb",
            "    return table['k']()",
        ),
        _src(
            "def f(cb, holder, flag):",
            "    if flag:",
            "        holder.fn = cb",
            "    return holder.fn()",
        ),
        _src(
            "def f(flag):",
            "    table = []",
            "    if flag:",
            "        def inner(cb):",
            "            table.append(cb)",
            "    return table",
        ),
    ],
)
def test_f4_benign_nested_mutation_is_accepted(source: str) -> None:
    assert _b(source) == ()


def test_f4_nested_function_body_is_not_outer_mutation() -> None:
    source = _src(
        "def f(cb):",
        "    table = []",
        "    def inner():",
        "        table.append(cb)",
        "    return table",
    )
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# Cross-finding compositions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tripwire", "body"),
    [
        (
            "B",
            _src(
                "def make():",
                "    return evaluate_frontier_eligibility",
                "def f(raw, **kw):",
                "    helper = make()",
                "    class C:",
                "        ev = helper",
                "    return C.ev(..., operator_evidence=raw, **kw)",
            ),
        ),
        (
            "B",
            _src(
                "def make():",
                "    return [evaluate_frontier_eligibility]",
                "def f(raw, **kw):",
                "    table = make()",
                "    class C:",
                "        TABLE = table",
                "    return C.TABLE[0](..., operator_evidence=raw, **kw)",
            ),
        ),
        (
            "B",
            _src(
                "def get(holder):",
                "    return holder.evaluate_frontier_eligibility",
                "def f(holder, raw, flag, **kw):",
                "    table = []",
                "    if flag:",
                "        table.append(get(holder))",
                "    return table[0](..., operator_evidence=raw, **kw)",
            ),
        ),
        (
            "B",
            _src(
                "def make():",
                "    return evaluate_frontier_eligibility",
                "def get():",
                "    x = make()",
                "    return x",
                "def f(raw, **kw):",
                "    table = {}",
                "    for _ in range(1):",
                "        table['k'] = get()",
                "    return table['k'](..., operator_evidence=raw, **kw)",
            ),
        ),
        (
            "B",
            _src(
                "ALIAS = evaluate_frontier_eligibility",
                "class C:",
                "    ev = ALIAS",
                "def get():",
                "    return C.ev",
                "def f(raw, **kw):",
                "    return get()(..., operator_evidence=raw, **kw)",
            ),
        ),
        (
            "B",
            _src(
                "def f(holder, raw, flag, **kw):",
                "    ev = evaluate_frontier_eligibility",
                "    if flag:",
                "        holder.ev = ev",
                "    def inner():",
                "        return holder.ev",
                "    return inner()(..., operator_evidence=raw, **kw)",
            ),
        ),
        (
            "B",
            _src(
                "def make():",
                "    return evaluate_frontier_eligibility",
                "def outer():",
                "    def inner():",
                "        return make()",
                "    return inner",
                "def unrelated():",
                "    def inner():",
                "        return None",
                "    return inner",
                "def f(raw, **kw):",
                "    ev = outer()()",
                "    return ev(..., operator_evidence=raw, **kw)",
            ),
        ),
        (
            "A",
            _src(
                "def make():",
                "    return OperatorExecutionEnableEvidence",
                "def get():",
                "    x = make()",
                "    return x",
                "class C:",
                "    E = get()",
                "def f(values):",
                "    return C.E(**values)",
            ),
        ),
        (
            "C",
            _src(
                "def make(store: SQLiteExecutionStateStore):",
                "    return store.mark_dispatching",
                "def get(store: SQLiteExecutionStateStore):",
                "    x = make(store)",
                "    return x",
                "def f(store: SQLiteExecutionStateStore, dec, flag):",
                "    table = []",
                "    if flag:",
                "        table.append(get(store))",
                "    return table[0](dec)",
            ),
        ),
    ],
)
def test_cross_finding_compositions_reject(tripwire: str, body: str) -> None:
    if tripwire == "B":
        assert _b(_EV + body)
    elif tripwire == "A":
        assert _a(_EC + body)
    else:
        assert _c(_DS + body)


def test_frozen_repository_tripwires_stay_green() -> None:
    sources = {
        str(path): path.read_text(encoding="utf-8")
        for path in (_ROOT / "src").rglob("*.py")
    }
    assert scan_tripwire_a(sources) == ()
    assert scan_tripwire_b(sources) == ()
    assert scan_tripwire_c(sources) == ()
