"""OPERATOR-001 Repair #9 container root provenance invariants.

Covers the single Review #9 mandatory finding
OP-001-REVIEW9-CONTAINER-ROOT-ALIAS-CLOBBER and preserves the Repair #8
owner-gate closure.
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
# Exact historical regression: module, function and class owners
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        _EV
        + _src(
            "table = {'k': evaluate_frontier_eligibility}",
            "table['x'] = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    return table['k'](..., operator_evidence=raw, **kw)",
        ),
        _EV
        + _src(
            "table = {'k': evaluate_frontier_eligibility}",
            "table['x'] = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    x = table",
            "    return x['k'](..., operator_evidence=raw, **kw)",
        ),
        _EV
        + _src(
            "table = {'k': evaluate_frontier_eligibility}",
            "table['x'] = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    x = table",
            "    y = x",
            "    return y['k'](..., operator_evidence=raw, **kw)",
        ),
        _EV
        + _src(
            "def f(raw, **kw):",
            "    table = {'k': evaluate_frontier_eligibility}",
            "    table['x'] = evaluate_frontier_eligibility",
            "    x = table",
            "    return x['k'](..., operator_evidence=raw, **kw)",
        ),
        _EV
        + _src(
            "class C:",
            "    table = {'k': evaluate_frontier_eligibility}",
            "    table['x'] = evaluate_frontier_eligibility",
            "def f(raw, **kw):",
            "    x = C.table",
            "    return x['k'](..., operator_evidence=raw, **kw)",
        ),
    ],
)
def test_repair9_b_root_clobber_rejects(source: str) -> None:
    assert _b(source)


def test_repair9_a_c_root_clobber_rejects() -> None:
    a_source = _EC + _src(
        "table = {'k': OperatorExecutionEnableEvidence}",
        "table['x'] = OperatorExecutionEnableEvidence",
        "def f(values):",
        "    x = table",
        "    return x['k'](**values)",
    )
    c_source = _DS + _src(
        "table = {'k': SQLiteExecutionStateStore.mark_dispatching}",
        "table['x'] = SQLiteExecutionStateStore.mark_dispatching",
        "def f(store, dec):",
        "    x = table",
        "    return x['k'](store, dec)",
    )
    assert _a(a_source)
    assert _c(c_source)


# ---------------------------------------------------------------------------
# Root preservation, slot isolation and later populations
# ---------------------------------------------------------------------------


def test_repair9_authority_then_benign_preserves_original_key() -> None:
    source = _EV + _src(
        "table = {'k': evaluate_frontier_eligibility}",
        "table['x'] = len",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_repair9_authority_then_authority_preserves_original_key() -> None:
    source = _EV + _src(
        "table = {'k': evaluate_frontier_eligibility}",
        "table['x'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_repair9_benign_then_authority_keeps_slot_isolation() -> None:
    benign_key = _EV + _src(
        "table = {'k': len}",
        "table['x'] = evaluate_frontier_eligibility",
        "def f(values):",
        "    x = table",
        "    return x['k'](values)",
    )
    authority_key = _EV + _src(
        "table = {'k': len}",
        "table['x'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['x'](..., operator_evidence=raw, **kw)",
    )
    assert _b(benign_key) == ()
    assert _b(authority_key)


def test_repair9_multiple_later_slots_preserve_original_key() -> None:
    source = _EV + _src(
        "table = {'k': evaluate_frontier_eligibility}",
        "table['x'] = len",
        "table['y'] = str",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(source)


def test_repair9_slot_isolation_safe_and_danger() -> None:
    safe = _src(
        "table = {'safe': len, 'danger': evaluate_frontier_eligibility}",
        "def f(values):",
        "    x = table",
        "    return x['safe'](values)",
    )
    danger = _EV + _src(
        "table = {'safe': len, 'danger': evaluate_frontier_eligibility}",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['danger'](..., operator_evidence=raw, **kw)",
    )
    safe_after = _src(
        "table = {'safe': len, 'danger': evaluate_frontier_eligibility}",
        "table['other'] = str",
        "def f(values):",
        "    x = table",
        "    return x['safe'](values)",
    )
    assert _b(safe) == ()
    assert _b(danger)
    assert _b(safe_after) == ()


def test_repair9_all_benign_literals_accept() -> None:
    module = _src(
        "table = {'k': len}",
        "table['x'] = str",
        "def f(values):",
        "    x = table",
        "    y = x",
        "    return y['k'](values)",
    )
    function = _src(
        "def f(values):",
        "    table = {'k': len}",
        "    table['x'] = str",
        "    x = table",
        "    return x['k'](values)",
    )
    class_owner = _src(
        "class C:",
        "    table = {'k': len}",
        "    table['x'] = str",
        "def f(values):",
        "    x = C.table",
        "    return x['k'](values)",
    )
    assert _b(module) == ()
    assert _b(function) == ()
    assert _b(class_owner) == ()


# ---------------------------------------------------------------------------
# Owner isolation and Repair #8 regression
# ---------------------------------------------------------------------------


def test_repair9_owner_isolation_acccepts() -> None:
    module_function = _EV + _src(
        "table = {'k': evaluate_frontier_eligibility}",
        "def f(values):",
        "    table = {'k': len}",
        "    x = table",
        "    return x['k'](values)",
    )
    class_class = _EV + _src(
        "class A:",
        "    table = {'k': evaluate_frontier_eligibility}",
        "class B:",
        "    table = {'k': len}",
        "def f(values):",
        "    x = B.table",
        "    return x['k'](values)",
    )
    assert _b(module_function) == ()
    assert _b(class_class) == ()


def test_repair9_repair8_owner_gate_preserved() -> None:
    module = _EV + _src(
        "table = {}",
        "table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    function = _EV + _src(
        "def f(raw, **kw):",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "    x = table",
        "    y = x",
        "    return y['k'](..., operator_evidence=raw, **kw)",
    )
    class_owner = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def get():",
        "    return C.table",
        "def f(raw, **kw):",
        "    x = get()",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(module)
    assert _b(function)
    assert _b(class_owner)


def test_repair9_helper_and_compound_clobber_reject() -> None:
    helper = _EV + _src(
        "table = {'k': evaluate_frontier_eligibility}",
        "table['x'] = evaluate_frontier_eligibility",
        "def get():",
        "    return table",
        "def f(raw, **kw):",
        "    x = get()",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    compound = _EV + _src(
        "table = {'k': evaluate_frontier_eligibility}",
        "if True:",
        "    table['x'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(helper)
    assert _b(compound)


# ---------------------------------------------------------------------------
# Out-of-scope fence
# ---------------------------------------------------------------------------


def test_repair9_out_of_scope_forms_remain_unimplemented() -> None:
    dynamic_key = _EV + _src(
        "table = {'k': evaluate_frontier_eligibility}",
        "table['x'] = evaluate_frontier_eligibility",
        "def f(raw, key, **kw):",
        "    x = table",
        "    return x[key](..., operator_evidence=raw, **kw)",
    )
    iterator = _EV + _src(
        "table = {'k': evaluate_frontier_eligibility}",
        "table['x'] = evaluate_frontier_eligibility",
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
        "    table = {'k': evaluate_frontier_eligibility}",
        "    table['x'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = C().table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(dynamic_key) == ()
    assert _b(iterator) == ()
    assert _b(augassign) == ()
    assert _b(instance) == ()


def test_repair9_root_identity_scan_terminates() -> None:
    lines = [_EV]
    for index in range(40):
        lines.extend(
            [
                f"table{index} = {{'k': evaluate_frontier_eligibility}}",
                f"table{index}['x'] = len",
                f"alias{index} = table{index}",
                f"alias{index}2 = alias{index}",
            ]
        )
    lines.extend(
        [
            "def target(raw, **kw):",
            "    x = alias0",
            "    return x['k'](..., operator_evidence=raw, **kw)",
        ]
    )
    source = "\n".join(lines) + "\n"
    first = _b(source)
    second = _b(source)
    assert first == second
    assert first
