"""OPERATOR-001 Repair #8 container owner identity invariants.

Covers the single Review #8 mandatory finding
OP-001-REVIEW8-CONTAINER-ALIAS-OWNER-GATE and preserves the Review #7
class-owner closures.
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
# Module owner
# ---------------------------------------------------------------------------


def test_repair8_module_direct_and_alias_reject() -> None:
    base = _EV + _src(
        "table = {}",
        "table['k'] = evaluate_frontier_eligibility",
    )
    direct = base + _src(
        "def f(raw, **kw):",
        "    return table['k'](..., operator_evidence=raw, **kw)",
    )
    alias = base + _src(
        "def f(raw, **kw):",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(direct)
    assert _b(alias)


def test_repair8_module_second_alias_rejects() -> None:
    source = _EV + _src(
        "table = {}",
        "table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = table",
        "    y = x",
        "    return y['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_repair8_module_helper_composition_rejects() -> None:
    source = _EV + _src(
        "table = {}",
        "table['k'] = evaluate_frontier_eligibility",
        "def get():",
        "    return table",
        "def f(raw, **kw):",
        "    x = get()",
        "    y = x",
        "    return y['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_repair8_module_a_c_aliases_reject() -> None:
    a_source = _EC + _src(
        "table = {}",
        "table['k'] = OperatorExecutionEnableEvidence",
        "def f(values):",
        "    x = table",
        "    return x['k'](**values)",
    )
    c_source = _DS + _src(
        "table = {}",
        "table['k'] = SQLiteExecutionStateStore.mark_dispatching",
        "def f(store, dec):",
        "    x = table",
        "    return x['k'](store, dec)",
    )
    assert _a(a_source)
    assert _c(c_source)


def test_repair8_module_compound_population_alias_rejects() -> None:
    source = _EV + _src(
        "table = {}",
        "if True:",
        "    table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_repair8_module_benign_alias_acccepts() -> None:
    source = _src(
        "table = {}",
        "table['k'] = len",
        "def f(values):",
        "    x = table",
        "    return x['k'](values)",
    )
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# Function owner
# ---------------------------------------------------------------------------


def test_repair8_function_direct_and_alias_reject() -> None:
    direct = _EV + _src(
        "def f(raw, **kw):",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "    return table['k'](..., operator_evidence=raw, **kw)",
    )
    alias = _EV + _src(
        "def f(raw, **kw):",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(direct)
    assert _b(alias)


def test_repair8_function_second_alias_rejects() -> None:
    source = _EV + _src(
        "def f(raw, **kw):",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "    x = table",
        "    y = x",
        "    return y['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_repair8_function_a_c_aliases_reject() -> None:
    a_source = _EC + _src(
        "def f(values):",
        "    table = {}",
        "    table['k'] = OperatorExecutionEnableEvidence",
        "    x = table",
        "    return x['k'](**values)",
    )
    c_source = _DS + _src(
        "def f(store, dec):",
        "    table = {}",
        "    table['k'] = SQLiteExecutionStateStore.mark_dispatching",
        "    x = table",
        "    return x['k'](store, dec)",
    )
    assert _a(a_source)
    assert _c(c_source)


def test_repair8_function_compound_population_alias_rejects() -> None:
    source = _EV + _src(
        "def f(raw, flag, **kw):",
        "    table = {}",
        "    if flag:",
        "        table['k'] = evaluate_frontier_eligibility",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_repair8_function_benign_alias_acccepts() -> None:
    source = _src(
        "def f(values):",
        "    table = {}",
        "    table['k'] = len",
        "    x = table",
        "    return x['k'](values)",
    )
    assert _b(source) == ()


# ---------------------------------------------------------------------------
# Nested functions and owner isolation
# ---------------------------------------------------------------------------


def test_repair8_nested_function_authority_paths_reject() -> None:
    own = _EV + _src(
        "def outer():",
        "    def inner(raw, **kw):",
        "        table = {}",
        "        table['k'] = evaluate_frontier_eligibility",
        "        x = table",
        "        return x['k'](..., operator_evidence=raw, **kw)",
        "    return inner",
    )
    enclosing = _EV + _src(
        "def outer():",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "    def inner(raw, **kw):",
        "        x = table",
        "        return x['k'](..., operator_evidence=raw, **kw)",
        "    return inner",
    )
    assert _b(own)
    assert _b(enclosing)


def test_repair8_nested_shadow_isolation_acccepts() -> None:
    source = _EV + _src(
        "def outer():",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "    def inner(values):",
        "        table = {}",
        "        table['j'] = len",
        "        x = table",
        "        return x['k'](values)",
        "    return inner",
    )
    assert _b(source) == ()


def test_repair8_owner_isolation_acccepts() -> None:
    module_function = _EV + _src(
        "table = {}",
        "table['k'] = evaluate_frontier_eligibility",
        "def f(values):",
        "    table = {}",
        "    table['j'] = len",
        "    x = table",
        "    return x['k'](values)",
    )
    module_class = _EV + _src(
        "table = {}",
        "table['k'] = evaluate_frontier_eligibility",
        "class C:",
        "    table = {}",
        "    table['j'] = len",
        "def f(values):",
        "    x = C.table",
        "    return x['k'](values)",
    )
    assert _b(module_function) == ()
    assert _b(module_class) == ()


def test_repair8_sibling_function_authority_and_benign() -> None:
    source = _EV + _src(
        "def f(raw, **kw):",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
        "def g(values):",
        "    table = {}",
        "    table['j'] = len",
        "    x = table",
        "    return x['k'](values)",
    )
    assert _b(source)


# ---------------------------------------------------------------------------
# Class-owner regression and Review #7 closures
# ---------------------------------------------------------------------------


def test_repair8_class_owner_regression() -> None:
    direct = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    return C.table['k'](..., operator_evidence=raw, **kw)",
    )
    alias = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = C.table",
        "    y = x",
        "    return y['k'](..., operator_evidence=raw, **kw)",
    )
    helper = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def get():",
        "    return C.table",
        "def f(raw, **kw):",
        "    x = get()",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(direct)
    assert _b(alias)
    assert _b(helper)


def test_repair8_class_isolation_acccepts() -> None:
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
            "    return Outer.Alias.table['k'](",
            "        ..., operator_evidence=raw, **kw",
            "    )",
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
            "def f(raw, **kw):",
            "    return Outer.Inner.ev(",
            "        ..., operator_evidence=raw, **kw",
            "    )",
        ),
    ],
)
def test_repair8_review7_closures_preserved(source: str) -> None:
    assert _b(source)


# ---------------------------------------------------------------------------
# Out-of-scope freeze
# ---------------------------------------------------------------------------


def test_repair8_out_of_scope_forms_remain_unimplemented() -> None:
    dynamic_key = _EV + _src(
        "table = {}",
        "table['k'] = evaluate_frontier_eligibility",
        "def f(raw, key, **kw):",
        "    x = table",
        "    return x[key](..., operator_evidence=raw, **kw)",
    )
    iterator = _EV + _src(
        "table = {}",
        "table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    for ev in table.values():",
        "        return ev(..., operator_evidence=raw, **kw)",
    )
    augassign = _EV + _src(
        "table = {}",
        "table |= {'k': evaluate_frontier_eligibility}",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    instance = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = C().table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(dynamic_key) == ()
    assert _b(iterator) == ()
    assert _b(augassign) == ()
    assert _b(instance) == ()


def test_repair8_owner_identity_scan_terminates() -> None:
    lines = [_EV, "table = {}", "table['k'] = evaluate_frontier_eligibility"]
    for index in range(30):
        lines.extend(
            [
                f"def f{index}(values):",
                "    table = {}",
                "    table['j'] = len",
                f"    x{index} = table",
                f"    return x{index}['k'](values)",
            ]
        )
    lines.extend(
        [
            "def target(raw, **kw):",
            "    x = table",
            "    return x['k'](..., operator_evidence=raw, **kw)",
        ]
    )
    source = "\n".join(lines) + "\n"
    first = _b(source)
    second = _b(source)
    assert first == second
    assert first
