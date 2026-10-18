"""OPERATOR-001 Implementation Repair #6 finite lexical/binding invariants.

Covers the five Review #6 mandatory findings as one bounded provenance model:
outer alias environments, compound-nested definitions, class composite slots,
nested class references and finite iteration/context/comprehension bindings.
"""

from __future__ import annotations

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
# F1 - outer aliases into function scopes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "def f(raw, **kw):",
            "    alias = A1",
            "    return alias(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    table = []",
            "    table.append(A1)",
            "    return table[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    table = {}",
            "    table['k'] = A1",
            "    return table['k'](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, holder, **kw):",
            "    holder.ev = A1",
            "    return holder.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    first = A1",
            "    second = first",
            "    return second(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, flag, **kw):",
            "    table = []",
            "    if flag:",
            "        table.append(A1)",
            "    return table[0](..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_f1_module_alias_into_function_rejects(body: str) -> None:
    assert _b(_EV + "A1 = evaluate_frontier_eligibility\n" + body)


def test_f1_enclosing_alias_into_nested_function_rejects() -> None:
    source = _src(
        "def outer():",
        "    A1 = evaluate_frontier_eligibility",
        "    def inner(raw, **kw):",
        "        alias = A1",
        "        return alias(..., operator_evidence=raw, **kw)",
        "    return inner",
    )
    assert _b(_EV + source)


def test_f1_module_alias_a_c_reject() -> None:
    a_source = _src(
        "def f(values):",
        "    E = A1",
        "    return E(**values)",
    )
    c_source = _src(
        "def f(store: SQLiteExecutionStateStore, dec):",
        "    md = A1",
        "    return md(store, dec)",
    )
    assert _a(_EC + "A1 = OperatorExecutionEnableEvidence\n" + a_source)
    assert _c(
        _DS + "A1 = SQLiteExecutionStateStore.mark_dispatching\n" + c_source
    )


@pytest.mark.parametrize(
    "source",
    [
        _src(
            "A1 = len",
            "def f(values):",
            "    alias = A1",
            "    return alias(values)",
        ),
        _src(
            "def outer():",
            "    A1 = len",
            "    def inner(values):",
            "        return A1(values)",
            "    return inner",
        ),
        _src(
            "A1 = len",
            "def f(values):",
            "    A1 = str",
            "    return A1(values)",
        ),
    ],
)
def test_f1_benign_outer_aliases_accept(source: str) -> None:
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# F2 - compound-nested definitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wrapper",
    [
        ("    if raw:", "        class C:\n            ev = A1\n"),
        ("    if raw:\n        pass\n    else:", "        class C:\n            ev = A1\n"),
        ("    for _ in range(1):", "        class C:\n            ev = A1\n"),
        ("    while raw:", "        class C:\n            ev = A1\n"),
        (
            "    try:",
            "        class C:\n            ev = A1\n    except Exception:\n        pass\n",
        ),
        ("    with ctx:", "        class C:\n            ev = A1\n"),
    ],
)
def test_f2_class_in_compound_rejects(wrapper: tuple[str, str]) -> None:
    header, body = wrapper
    source = _src(
        "A1 = evaluate_frontier_eligibility",
        "def f(raw, ctx, **kw):",
        header,
        body,
        "    return C.ev(..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_f2_class_in_if_a_c_reject() -> None:
    a_source = _src(
        "def f(values, raw):",
        "    if raw:",
        "        class C:",
        "            E = OperatorExecutionEnableEvidence",
        "    return C.E(**values)",
    )
    c_source = _src(
        "def f(store: SQLiteExecutionStateStore, dec, raw):",
        "    try:",
        "        class C:",
        "            md = store.mark_dispatching",
        "    except Exception:",
        "        pass",
        "    return C.md(store, dec)",
    )
    assert _a(_EC + a_source)
    assert _c(_DS + c_source)


def test_f2_function_in_compound_rejects() -> None:
    source = _src(
        "def f(raw, **kw):",
        "    if True:",
        "        def make():",
        "            return evaluate_frontier_eligibility",
        "    return make()(..., operator_evidence=raw, **kw)",
    )
    assert _b(_EV + source)


def test_f2_ambiguous_compound_class_fails_closed() -> None:
    source = _src(
        "def f(raw, **kw):",
        "    if raw:",
        "        class C:",
        "            ev = evaluate_frontier_eligibility",
        "    else:",
        "        class C:",
        "            ev = len",
        "    return C.ev(..., operator_evidence=raw, **kw)",
    )
    assert _b(_EV + source)


def test_f2_benign_compound_class_accepts() -> None:
    source = _src(
        "def f(values, raw):",
        "    if raw:",
        "        class C:",
        "            fn = len",
        "    return C.fn(values)",
    )
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# F3 - class composite slots
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "class C:",
            "    table = {}",
            "    table['k'] = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    return C.table['k'](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "class C:",
            "    table = []",
            "    table[0] = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    return C.table[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "class C:",
            "    table = {}",
            "    table['k'] = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    first = C.table['k']",
            "    second = first",
            "    return second(..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_f3_class_slot_rejects(body: str) -> None:
    assert _b(_EV + body)


def test_f3_class_slot_helper_return_rejects() -> None:
    source = _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def get():",
        "    return C.table['k']",
        "def f(raw, **kw):",
        "    return get()(..., operator_evidence=raw, **kw)",
    )
    assert _b(_EV + source)


def test_f3_class_slot_a_c_reject() -> None:
    a_source = _src(
        "class C:",
        "    table = {}",
        "    table['k'] = OperatorExecutionEnableEvidence",
        "def f(values):",
        "    return C.table['k'](**values)",
    )
    c_source = _src(
        "class C:",
        "    table = {}",
        "    table['k'] = SQLiteExecutionStateStore.mark_dispatching",
        "def f(store: SQLiteExecutionStateStore, dec):",
        "    return C.table['k'](store, dec)",
    )
    assert _a(_EC + a_source)
    assert _c(_DS + c_source)


def test_f3_class_slot_isolation() -> None:
    authority = _src(
        "class C1:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "class C2:",
        "    table = {}",
        "    table['k'] = len",
        "def f(values):",
        "    return C2.table['k'](values)",
    )
    assert _b(_EV + authority) == ()


def test_f3_benign_class_slot_accepts() -> None:
    source = _src(
        "class C:",
        "    table = {}",
        "    table['k'] = len",
        "def f(values):",
        "    return C.table['k'](values)",
    )
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# F4 - nested class references
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "class Outer:",
            "    class Inner:",
            "        ev = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    return Outer.Inner.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "class Outer:",
            "    class Inner:",
            "        TABLE = [evaluate_frontier_eligibility]",
            "def f(raw, **kw):",
            "    return Outer.Inner.TABLE[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "class Outer:",
            "    class Inner:",
            "        table = {}",
            "        table['k'] = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    return Outer.Inner.table['k'](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "class Outer:",
            "    class Inner:",
            "        ev = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    alias = Outer.Inner",
            "    return alias.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "class Outer:",
            "    class Inner:",
            "        ev = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    x = Outer",
            "    y = x.Inner",
            "    return y.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "class Outer:",
            "    class Middle:",
            "        class Inner:",
            "            ev = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    return Outer.Middle.Inner.ev(",
            "        ..., operator_evidence=raw, **kw",
            "    )",
        ),
    ],
)
def test_f4_nested_class_rejects(body: str) -> None:
    assert _b(_EV + body)


def test_f4_nested_class_a_c_reject() -> None:
    a_source = _src(
        "class Outer:",
        "    class Inner:",
        "        E = OperatorExecutionEnableEvidence",
        "def f(values):",
        "    return Outer.Inner.E(**values)",
    )
    c_source = _src(
        "class Outer:",
        "    class Inner:",
        "        md = SQLiteExecutionStateStore.mark_dispatching",
        "def f(store: SQLiteExecutionStateStore, dec):",
        "    return Outer.Inner.md(store, dec)",
    )
    assert _a(_EC + a_source)
    assert _c(_DS + c_source)


def test_f4_duplicate_nested_class_fails_closed() -> None:
    source = _src(
        "class Outer:",
        "    class Inner:",
        "        ev = evaluate_frontier_eligibility",
        "class Outer2:",
        "    class Inner:",
        "        ev = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    return Outer.Inner.ev(..., operator_evidence=raw, **kw)",
    )
    assert _b(_EV + source)


def test_f4_benign_nested_class_accepts() -> None:
    source = _src(
        "class Outer:",
        "    class Inner:",
        "        fn = len",
        "def f(values):",
        "    return Outer.Inner.fn(values)",
    )
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# F5 - iteration/context/comprehension bindings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "def f(raw, **kw):",
            "    for ev in [evaluate_frontier_eligibility]:",
            "        return ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    for ev, other in [(evaluate_frontier_eligibility, None)]:",
            "        return ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    table = [evaluate_frontier_eligibility]",
            "    for ev in table:",
            "        return ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    tables = {'k': evaluate_frontier_eligibility}",
            "    for ev in tables:",
            "        return ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    ctx = evaluate_frontier_eligibility",
            "    with ctx as ev:",
            "        return ev(..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_f5_for_and_context_rejects(body: str) -> None:
    assert _b(_EV + body)


def test_f5_for_a_c_reject() -> None:
    a_source = _src(
        "def f(values):",
        "    for E in [OperatorExecutionEnableEvidence]:",
        "        return E(**values)",
    )
    c_source = _src(
        "def f(store: SQLiteExecutionStateStore, dec):",
        "    for fn in [store.mark_dispatching]:",
        "        return fn(dec)",
    )
    assert _a(_EC + a_source)
    assert _c(_DS + c_source)


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "def f(raw, **kw):",
            "    table = [x for x in [evaluate_frontier_eligibility]]",
            "    got = table[0]",
            "    return got(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    table = list({x for x in [evaluate_frontier_eligibility]})",
            "    got = table[0]",
            "    return got(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    table = {k: k for k in [evaluate_frontier_eligibility]}",
            "    for k in table:",
            "        return k(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    table = list(x for x in [evaluate_frontier_eligibility])",
            "    got = table[0]",
            "    return got(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    return [",
            "        ev(..., operator_evidence=raw, **kw)",
            "        for ev in [evaluate_frontier_eligibility]",
            "    ][0]",
        ),
    ],
)
def test_f5_comprehension_rejects(body: str) -> None:
    assert _b(_EV + body)


@pytest.mark.parametrize(
    "source",
    [
        _src(
            "def f(values):",
            "    for fn in [len]:",
            "        return fn(values)",
        ),
        _src(
            "def f(values, ctx):",
            "    with ctx as fn:",
            "        return fn(values)",
        ),
        _src(
            "def f(values):",
            "    table = [x for x in [len]]",
            "    return table[0](values)",
        ),
        _src(
            "def f(values):",
            "    table = {k: k for k in [len]}",
            "    for k in table:",
            "        return k(values)",
        ),
    ],
)
def test_f5_benign_bindings_accept(source: str) -> None:
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# Cross-finding compositions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "A1 = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    alias = A1",
            "    class C:",
            "        ev = alias",
            "    return C.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "A1 = evaluate_frontier_eligibility",
            "def f(raw, flag, **kw):",
            "    table = []",
            "    if flag:",
            "        table.append(A1)",
            "    return table[0](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "A1 = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    for ev in [A1]:",
            "        alias = ev",
            "        return alias(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, flag, **kw):",
            "    if flag:",
            "        class C:",
            "            table = {}",
            "            table['k'] = evaluate_frontier_eligibility",
            "    return C.table['k'](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    try:",
            "        class Outer:",
            "            class Inner:",
            "                ev = evaluate_frontier_eligibility",
            "    except Exception:",
            "        pass",
            "    return Outer.Inner.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "class Outer:",
            "    class Inner:",
            "        table = {}",
            "        table['k'] = evaluate_frontier_eligibility",
            "def get():",
            "    return Outer.Inner.table['k']",
            "def f(raw, **kw):",
            "    return get()(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    for ev in [evaluate_frontier_eligibility]:",
            "        class C:",
            "            fn = ev",
            "        return C.fn(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    table = [x for x in [evaluate_frontier_eligibility]]",
            "    for fn in table:",
            "        return fn(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "A1 = evaluate_frontier_eligibility",
            "def outer():",
            "    def inner(raw, **kw):",
            "        if True:",
            "            class C:",
            "                ev = A1",
            "        return C.ev(..., operator_evidence=raw, **kw)",
            "    return inner",
        ),
        _src(
            "A1 = evaluate_frontier_eligibility",
            "def f(raw, flag, **kw):",
            "    if flag:",
            "        class Outer:",
            "            class Inner:",
            "                table = {}",
            "                table['k'] = A1",
            "    first = Outer.Inner.table['k']",
            "    second = first",
            "    return second(..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_cross_finding_compositions_reject(body: str) -> None:
    assert _b(_EV + body)


@pytest.mark.parametrize(
    "source",
    [
        _src(
            "A1 = len",
            "def f(values):",
            "    alias = A1",
            "    class C:",
            "        fn = alias",
            "    return C.fn(values)",
        ),
        _src(
            "def f(values, flag):",
            "    if flag:",
            "        class C:",
            "            fn = len",
            "    return C.fn(values)",
        ),
        _src(
            "class Outer:",
            "    class Inner:",
            "        fn = len",
            "def f(values):",
            "    return Outer.Inner.fn(values)",
        ),
        _src(
            "def f(values):",
            "    for fn in [len]:",
            "        class C:",
            "            member = fn",
            "        return C.member(values)",
        ),
    ],
)
def test_benign_cross_compositions_accept(source: str) -> None:
    assert _b(source) == ()


def test_preserved_returned_attribute_and_helper_local() -> None:
    attribute = _src(
        "def get(holder):",
        "    return holder.evaluate_frontier_eligibility",
        "def f(holder, raw, **kw):",
        "    fn = get(holder)",
        "    return fn(..., operator_evidence=raw, **kw)",
    )
    helper = _src(
        "def make():",
        "    return evaluate_frontier_eligibility",
        "def get():",
        "    x = make()",
        "    return x",
        "def f(raw, **kw):",
        "    y = get()",
        "    return y(..., operator_evidence=raw, **kw)",
    )
    assert _b(_EV + attribute)
    assert _b(_EV + helper)


def test_preserved_closure_ambiguity() -> None:
    ambiguous = _src(
        "def outer():",
        "    def inner():",
        "        return evaluate_frontier_eligibility",
        "    return inner",
        "def unrelated():",
        "    def inner():",
        "        return None",
        "    return inner",
        "def f(raw, **kw):",
        "    ev = outer()()",
        "    return ev(..., operator_evidence=raw, **kw)",
    )
    benign = _src(
        "def outer():",
        "    def inner():",
        "        return len",
        "    return inner",
        "def unrelated():",
        "    def inner():",
        "        return None",
        "    return inner",
        "def f(values):",
        "    return outer()()(values)",
    )
    assert _b(_EV + ambiguous)
    assert _b(benign) == ()


def test_frozen_repository_tripwires_stay_green() -> None:
    sources = {
        str(path): path.read_text(encoding="utf-8")
        for path in (_ROOT / "src").rglob("*.py")
    }
    assert scan_tripwire_a(sources) == ()
    assert scan_tripwire_b(sources) == ()
    assert scan_tripwire_c(sources) == ()
