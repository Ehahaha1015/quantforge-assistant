"""OPERATOR-001 Repair #7 class identity preservation invariants.

Covers the three Review #7 mandatory residuals:

1. OP-001-REVIEW7-CLASS-CONTAINER-ALIAS
2. OP-001-REVIEW7-NESTED-CLASS-DUPLICATE-AMBIGUITY
3. OP-001-REVIEW7-CLASS-BODY-NESTED-ALIAS

The cases stay inside the frozen finite class-reference/composite-slot grammar.
"""

from __future__ import annotations

import pytest

from quantforge.operator.static_checks import (
    scan_tripwire_a,
    scan_tripwire_b,
    scan_tripwire_c,
)

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
# F1 - class container alias identity
# ---------------------------------------------------------------------------


def test_f1_container_alias_b_rejects() -> None:
    source = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = C.table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "    x = C.table",
            "    y = x",
            "    return y['k'](..., operator_evidence=raw, **kw)",
        ),
        _src(
            "    x = C.table",
            "    got = x['k']",
            "    return got(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "    x = C.table",
            "    Klass = C",
            "    y = Klass.table",
            "    return y['k'](..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_f1_container_alias_fresh_forms_reject(body: str) -> None:
    source = (
        _EV
        + _src(
            "class C:",
            "    table = {}",
            "    table['k'] = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
        )
        + body
    )
    assert _b(source)


def test_f1_container_alias_class_first_rejects() -> None:
    source = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    Klass = C",
        "    x = Klass.table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_f1_helper_returned_table_rejects() -> None:
    source = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def get():",
        "    return C.table",
        "def f(raw, **kw):",
        "    x = get()",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_f1_class_body_container_alias_rejects() -> None:
    source = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "    alias = table",
        "def f(raw, **kw):",
        "    return C.alias['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_f1_container_alias_a_c_reject() -> None:
    a_source = _EC + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = OperatorExecutionEnableEvidence",
        "def f(values):",
        "    x = C.table",
        "    return x['k'](**values)",
    )
    c_source = _DS + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = SQLiteExecutionStateStore.mark_dispatching",
        "def f(store, dec):",
        "    x = C.table",
        "    return x['k'](store, dec)",
    )
    assert _a(a_source)
    assert _c(c_source)


def test_f1_class_isolation_acccepts() -> None:
    source = _EV + _src(
        "class A:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "class B:",
        "    table = {}",
        "    table['k'] = len",
        "def f(values):",
        "    x = B.table",
        "    return x['k'](values)",
    )
    assert _b(source) == ()


def test_f1_benign_container_alias_acccepts() -> None:
    source = _src(
        "class C:",
        "    table = {}",
        "    table['k'] = len",
        "def f(values):",
        "    x = C.table",
        "    return x['k'](values)",
    )
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# F2 - duplicate nested class ambiguity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "class Outer:",
            "    if True:",
            "        class Inner:",
            "            ev = evaluate_frontier_eligibility",
            "    else:",
            "        class Inner:",
            "            ev = len",
        ),
        _src(
            "class Outer:",
            "    try:",
            "        class Inner:",
            "            ev = evaluate_frontier_eligibility",
            "    except Exception:",
            "        class Inner:",
            "            ev = len",
        ),
        _src(
            "class Outer:",
            "    class Inner:",
            "        ev = evaluate_frontier_eligibility",
            "    class Inner:",
            "        ev = len",
        ),
        _src(
            "class Outer:",
            "    if True:",
            "        class Inner:",
            "            ev = evaluate_frontier_eligibility",
            "    else:",
            "        class Inner:",
            "            ev = evaluate_frontier_eligibility",
        ),
    ],
)
def test_f2_duplicate_nested_class_rejects(body: str) -> None:
    source = (
        _EV
        + body
        + _src(
            "def f(raw, **kw):",
            "    return Outer.Inner.ev(",
            "        ..., operator_evidence=raw, **kw",
            "    )",
        )
    )
    assert _b(source)


def test_f2_duplicate_nested_class_a_c_reject() -> None:
    a_source = _EC + _src(
        "class Outer:",
        "    if True:",
        "        class Inner:",
        "            E = OperatorExecutionEnableEvidence",
        "    else:",
        "        class Inner:",
        "            E = len",
        "def f(values):",
        "    return Outer.Inner.E(**values)",
    )
    c_source = _DS + _src(
        "class Outer:",
        "    try:",
        "        class Inner:",
        "            md = SQLiteExecutionStateStore.mark_dispatching",
        "    except Exception:",
        "        class Inner:",
        "            md = len",
        "def f(store, dec):",
        "    return Outer.Inner.md(store, dec)",
    )
    assert _a(a_source)
    assert _c(c_source)


@pytest.mark.parametrize(
    "body",
    [
        _src(
            "def f(raw, **kw):",
            "    x = Outer.Inner",
            "    return x.ev(..., operator_evidence=raw, **kw)",
        ),
        _src(
            "def f(raw, **kw):",
            "    OuterAlias = Outer",
            "    return OuterAlias.Inner.ev(",
            "        ..., operator_evidence=raw, **kw",
            "    )",
        ),
    ],
)
def test_f2_duplicate_ambiguity_through_alias_rejects(body: str) -> None:
    source = (
        _EV
        + _src(
            "class Outer:",
            "    if True:",
            "        class Inner:",
            "            ev = evaluate_frontier_eligibility",
            "    else:",
            "        class Inner:",
            "            ev = len",
        )
        + body
    )
    assert _b(source)


def test_f2_duplicate_ambiguity_through_slot_rejects() -> None:
    source = _EV + _src(
        "class Outer:",
        "    if True:",
        "        class Inner:",
        "            table = {}",
        "            table['k'] = evaluate_frontier_eligibility",
        "    else:",
        "        class Inner:",
        "            table = {}",
        "            table['k'] = len",
        "def f(raw, **kw):",
        "    return Outer.Inner.table['k'](",
        "        ..., operator_evidence=raw, **kw",
        "    )",
    )
    assert _b(source)


def test_f2_unique_nested_controls() -> None:
    authority = _EV + _src(
        "class Outer:",
        "    class Inner:",
        "        ev = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    return Outer.Inner.ev(..., operator_evidence=raw, **kw)",
    )
    benign = _src(
        "class Outer:",
        "    class Inner:",
        "        fn = len",
        "def f(values):",
        "    return Outer.Inner.fn(values)",
    )
    assert _b(authority)
    assert _b(benign) == ()


# ---------------------------------------------------------------------------
# F3 - class-body nested-class alias identity
# ---------------------------------------------------------------------------


def test_f3_class_body_nested_alias_rejects() -> None:
    source = _EV + _src(
        "class Outer:",
        "    class Inner:",
        "        ev = evaluate_frontier_eligibility",
        "    Alias = Inner",
        "def f(raw, **kw):",
        "    return Outer.Alias.ev(..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_f3_class_body_nested_alias_a_c_reject() -> None:
    a_source = _EC + _src(
        "class Outer:",
        "    class Inner:",
        "        E = OperatorExecutionEnableEvidence",
        "    Alias = Inner",
        "def f(values):",
        "    return Outer.Alias.E(**values)",
    )
    c_source = _DS + _src(
        "class Outer:",
        "    class Inner:",
        "        md = SQLiteExecutionStateStore.mark_dispatching",
        "    Alias = Inner",
        "def f(store, dec):",
        "    return Outer.Alias.md(store, dec)",
    )
    assert _a(a_source)
    assert _c(c_source)


def test_f3_second_class_body_alias_rejects() -> None:
    source = _EV + _src(
        "class Outer:",
        "    class Inner:",
        "        ev = evaluate_frontier_eligibility",
        "    Alias = Inner",
        "    Alias2 = Alias",
        "def f(raw, **kw):",
        "    return Outer.Alias2.ev(..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_f3_deep_class_body_alias_rejects() -> None:
    source = _EV + _src(
        "class Outer:",
        "    class Middle:",
        "        class Inner:",
        "            ev = evaluate_frontier_eligibility",
        "        Alias = Inner",
        "def f(raw, **kw):",
        "    return Outer.Middle.Alias.ev(",
        "        ..., operator_evidence=raw, **kw",
        "    )",
    )
    assert _b(source)


def test_f3_class_body_alias_slot_rejects() -> None:
    source = _EV + _src(
        "class Outer:",
        "    class Inner:",
        "        table = {}",
        "        table['k'] = evaluate_frontier_eligibility",
        "    Alias = Inner",
        "def f(raw, **kw):",
        "    return Outer.Alias.table['k'](",
        "        ..., operator_evidence=raw, **kw",
        "    )",
    )
    assert _b(source)


def test_f3_benign_class_body_alias_acccepts() -> None:
    source = _src(
        "class Outer:",
        "    class Inner:",
        "        fn = len",
        "    Alias = Inner",
        "def f(values):",
        "    return Outer.Alias.fn(values)",
    )
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# Cross-finding and three-edge compositions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        _EV
        + _src(
            "class Outer:",
            "    class Inner:",
            "        table = {}",
            "        table['k'] = evaluate_frontier_eligibility",
            "    Alias = Inner",
            "def f(raw, **kw):",
            "    x = Outer.Alias.table",
            "    return x['k'](..., operator_evidence=raw, **kw)",
        ),
        _EV
        + _src(
            "class Outer:",
            "    class Inner:",
            "        ev = evaluate_frontier_eligibility",
            "    Alias = Inner",
            "def f(raw, **kw):",
            "    x = Outer.Alias",
            "    y = x",
            "    return y.ev(..., operator_evidence=raw, **kw)",
        ),
        _EV
        + _src(
            "class C:",
            "    table = {}",
            "    table['k'] = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    x = C.table",
            "    y = x",
            "    return y['k'](..., operator_evidence=raw, **kw)",
        ),
        _EV
        + _src(
            "class C:",
            "    table = {}",
            "    table['k'] = evaluate_frontier_eligibility",
            "def get():",
            "    return C.table",
            "def f(raw, **kw):",
            "    Klass = C",
            "    x = Klass.table",
            "    y = get()",
            "    return y['k'](..., operator_evidence=raw, **kw)",
        ),
        _EV
        + _src(
            "class Outer:",
            "    if True:",
            "        class Inner:",
            "            ev = evaluate_frontier_eligibility",
            "    else:",
            "        class Inner:",
            "            ev = len",
            "    Alias = Inner",
            "def f(raw, **kw):",
            "    return Outer.Alias.ev(..., operator_evidence=raw, **kw)",
        ),
        _EV
        + _src(
            "class Outer:",
            "    class Middle:",
            "        class Inner:",
            "            table = {}",
            "            table['k'] = evaluate_frontier_eligibility",
            "        Alias = Inner",
            "def f(raw, **kw):",
            "    x = Outer.Middle.Alias.table",
            "    return x['k'](..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_repair7_cross_finding_rejects(source: str) -> None:
    assert _b(source)


def test_repair7_benign_cross_composition_acccepts() -> None:
    source = _src(
        "class Outer:",
        "    class Inner:",
        "        table = {}",
        "        table['k'] = len",
        "    Alias = Inner",
        "def f(values):",
        "    x = Outer.Alias.table",
        "    y = x",
        "    return y['k'](values)",
    )
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# Review #7 out-of-scope freeze and termination-sensitive behavior
# ---------------------------------------------------------------------------


def test_review7_out_of_scope_forms_stay_out_of_scope() -> None:
    dynamic_key = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def f(raw, key, **kw):",
        "    return C.table[key](..., operator_evidence=raw, **kw)",
    )
    iterator = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    for ev in C.table.values():",
        "        return ev(..., operator_evidence=raw, **kw)",
    )
    assert _b(dynamic_key) == ()
    assert _b(iterator) == ()


def test_repair7_bounded_identity_scan_terminates() -> None:
    lines = [_EV, "class C:", "    table = {}", "    alias = table"]
    for index in range(20):
        lines.extend(
            [
                "    if True:",
                f"        table['k{index}'] = evaluate_frontier_eligibility",
                f"        alias{index} = table",
            ]
        )
    lines.extend(
        [
            "def f(raw, **kw):",
            "    x = C.alias19",
            "    return x['k19'](..., operator_evidence=raw, **kw)",
        ]
    )
    source = "\n".join(lines) + "\n"
    first = _b(source)
    second = _b(source)
    assert first == second
    assert first
