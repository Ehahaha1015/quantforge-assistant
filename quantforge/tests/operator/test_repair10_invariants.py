"""OPERATOR-001 Repair #10 constant-key normalization invariants.

Covers the single Review #10 mandatory finding
OP-001-REVIEW10-CONSTANT-SLOT-KEY-NORMALIZATION and preserves the Repair #9
root-provenance closure.
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
# Exact Review #10 reproduction and symmetric cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record,read",
    [("True", "1"), ("1", "True"), ("False", "0"), ("0", "False")],
)
def test_repair10_equivalent_keys_reject(
    record: str, read: str
) -> None:
    direct = _EV + _src(
        f"table = {{{record}: evaluate_frontier_eligibility}}",
        "def f(raw, **kw):",
        f"    return table[{read}](",
        "        ..., operator_evidence=raw, **kw",
        "    )",
    )
    alias = _EV + _src(
        f"table = {{{record}: evaluate_frontier_eligibility}}",
        "def f(raw, **kw):",
        "    x = table",
        f"    return x[{read}](",
        "        ..., operator_evidence=raw, **kw",
        "    )",
    )
    second = _EV + _src(
        f"table = {{{record}: evaluate_frontier_eligibility}}",
        "def f(raw, **kw):",
        "    x = table",
        "    y = x",
        f"    return y[{read}](",
        "        ..., operator_evidence=raw, **kw",
        "    )",
    )
    assert _b(direct)
    assert _b(alias)
    assert _b(second)


def test_repair10_function_and_class_owners_reject() -> None:
    function = _EV + _src(
        "def f(raw, **kw):",
        "    table = {True: evaluate_frontier_eligibility}",
        "    x = table",
        "    return x[1](..., operator_evidence=raw, **kw)",
    )
    class_owner = _EV + _src(
        "class C:",
        "    table = {0: evaluate_frontier_eligibility}",
        "def f(raw, **kw):",
        "    x = C.table",
        "    return x[False](..., operator_evidence=raw, **kw)",
    )
    assert _b(function)
    assert _b(class_owner)


def test_repair10_a_c_equivalent_keys_reject() -> None:
    a_true = _EC + _src(
        "table = {True: OperatorExecutionEnableEvidence}",
        "def f(values):",
        "    x = table",
        "    return x[1](**values)",
    )
    a_false = _EC + _src(
        "table = {False: OperatorExecutionEnableEvidence}",
        "def f(values):",
        "    x = table",
        "    return x[0](**values)",
    )
    c_true = _DS + _src(
        "table = {1: SQLiteExecutionStateStore.mark_dispatching}",
        "def f(store, dec):",
        "    x = table",
        "    return x[True](store, dec)",
    )
    c_false = _DS + _src(
        "table = {0: SQLiteExecutionStateStore.mark_dispatching}",
        "def f(store, dec):",
        "    x = table",
        "    return x[False](store, dec)",
    )
    assert _a(a_true)
    assert _a(a_false)
    assert _c(c_true)
    assert _c(c_false)


# ---------------------------------------------------------------------------
# Key distinctions and isolation
# ---------------------------------------------------------------------------


def test_repair10_string_and_int_keys_stay_distinct() -> None:
    string_to_int = _src(
        "table = {'1': evaluate_frontier_eligibility}",
        "def f(values):",
        "    x = table",
        "    return x[1](values)",
    )
    int_to_string = _src(
        "table = {1: evaluate_frontier_eligibility}",
        "def f(values):",
        "    x = table",
        "    return x['1'](values)",
    )
    string_zero = _src(
        "table = {'0': evaluate_frontier_eligibility}",
        "def f(values):",
        "    x = table",
        "    return x[0](values)",
    )
    zero_string = _src(
        "table = {0: evaluate_frontier_eligibility}",
        "def f(values):",
        "    x = table",
        "    return x['0'](values)",
    )
    assert _b(string_to_int) == ()
    assert _b(int_to_string) == ()
    assert _b(string_zero) == ()
    assert _b(zero_string) == ()


def test_repair10_ordinary_integer_slots_stay_distinct() -> None:
    one_to_two = _src(
        "table = {1: evaluate_frontier_eligibility}",
        "def f(values):",
        "    x = table",
        "    return x[2](values)",
    )
    zero_to_two = _src(
        "table = {0: evaluate_frontier_eligibility}",
        "def f(values):",
        "    x = table",
        "    return x[2](values)",
    )
    assert _b(one_to_two) == ()
    assert _b(zero_to_two) == ()


def test_repair10_bool_int_slot_isolation() -> None:
    true_authority = _EV + _src(
        "table = {True: evaluate_frontier_eligibility, 2: len}",
        "def f(raw, **kw):",
        "    x = table",
        "    return x[1](..., operator_evidence=raw, **kw)",
    )
    false_authority = _EV + _src(
        "table = {False: evaluate_frontier_eligibility, 2: len}",
        "def f(raw, **kw):",
        "    x = table",
        "    return x[0](..., operator_evidence=raw, **kw)",
    )
    benign_two = _src(
        "table = {True: evaluate_frontier_eligibility, 2: len}",
        "def f(values):",
        "    x = table",
        "    return x[2](values)",
    )
    assert _b(true_authority)
    assert _b(false_authority)
    assert _b(benign_two) == ()


def test_repair10_duplicate_equivalent_literal_keys_fail_closed() -> None:
    true_then_one = _EV + _src(
        "table = {True: evaluate_frontier_eligibility, 1: len}",
        "def f(raw, **kw):",
        "    x = table",
        "    return x[1](..., operator_evidence=raw, **kw)",
    )
    one_then_true = _EV + _src(
        "table = {1: len, True: evaluate_frontier_eligibility}",
        "def f(raw, **kw):",
        "    x = table",
        "    return x[1](..., operator_evidence=raw, **kw)",
    )
    assert _b(true_then_one)
    assert _b(one_then_true)


def test_repair10_equivalent_later_assignment_fails_closed() -> None:
    true_then_one = _EV + _src(
        "table = {True: evaluate_frontier_eligibility}",
        "table[1] = len",
        "def f(raw, **kw):",
        "    x = table",
        "    return x[1](..., operator_evidence=raw, **kw)",
    )
    one_then_true = _EV + _src(
        "table = {1: evaluate_frontier_eligibility}",
        "table[True] = len",
        "def f(raw, **kw):",
        "    x = table",
        "    return x[True](..., operator_evidence=raw, **kw)",
    )
    assert _b(true_then_one)
    assert _b(one_then_true)


# ---------------------------------------------------------------------------
# Owner isolation and preserved closures
# ---------------------------------------------------------------------------


def test_repair10_owner_isolation_acccepts() -> None:
    module_function = _EV + _src(
        "table = {True: evaluate_frontier_eligibility}",
        "def f(values):",
        "    table = {1: len}",
        "    x = table",
        "    return x[True](values)",
    )
    class_class = _EV + _src(
        "class A:",
        "    table = {False: evaluate_frontier_eligibility}",
        "class B:",
        "    table = {0: len}",
        "def f(values):",
        "    x = B.table",
        "    return x[False](values)",
    )
    assert _b(module_function) == ()
    assert _b(class_class) == ()


def test_repair10_repair9_root_clobber_preserved() -> None:
    canonical = _EV + _src(
        "table = {'k': evaluate_frontier_eligibility}",
        "table['x'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    normalized = _EV + _src(
        "table = {True: evaluate_frontier_eligibility}",
        "table[2] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = table",
        "    return x[1](..., operator_evidence=raw, **kw)",
    )
    assert _b(canonical)
    assert _b(normalized)


def test_repair10_repair8_owner_gate_preserved() -> None:
    module = _EV + _src(
        "table = {}",
        "table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    class_owner = _EV + _src(
        "class C:",
        "    table = {}",
        "    table['k'] = evaluate_frontier_eligibility",
        "def f(raw, **kw):",
        "    x = C.table",
        "    return x['k'](..., operator_evidence=raw, **kw)",
    )
    assert _b(module)
    assert _b(class_owner)


def test_repair10_benign_controls_accept() -> None:
    module = _src(
        "table = {True: len}",
        "def f(values):",
        "    x = table",
        "    return x[1](values)",
    )
    function = _src(
        "def f(values):",
        "    table = {False: len}",
        "    x = table",
        "    return x[0](values)",
    )
    class_owner = _src(
        "class C:",
        "    table = {True: len}",
        "def f(values):",
        "    x = C.table",
        "    return x[1](values)",
    )
    assert _b(module) == ()
    assert _b(function) == ()
    assert _b(class_owner) == ()


def test_repair10_out_of_scope_forms_remain_unimplemented() -> None:
    dynamic_key = _EV + _src(
        "table = {True: evaluate_frontier_eligibility}",
        "def f(raw, key, **kw):",
        "    x = table",
        "    return x[key](..., operator_evidence=raw, **kw)",
    )
    iterator = _EV + _src(
        "table = {True: evaluate_frontier_eligibility}",
        "def f(raw, **kw):",
        "    for ev in table.values():",
        "        return ev(..., operator_evidence=raw, **kw)",
    )
    augassign = _EV + _src(
        "table = {}",
        "table |= {True: evaluate_frontier_eligibility}",
        "def f(raw, **kw):",
        "    x = table",
        "    return x[1](..., operator_evidence=raw, **kw)",
    )
    instance = _EV + _src(
        "class C:",
        "    table = {True: evaluate_frontier_eligibility}",
        "def f(raw, **kw):",
        "    x = C().table",
        "    return x[1](..., operator_evidence=raw, **kw)",
    )
    assert _b(dynamic_key) == ()
    assert _b(iterator) == ()
    assert _b(augassign) == ()
    assert _b(instance) == ()


def test_repair10_normalization_scan_terminates() -> None:
    lines = [_EV]
    for index in range(40):
        lines.extend(
            [
                f"table{index} = {{True: evaluate_frontier_eligibility}}",
                f"table{index}[False] = {{0: len}}",
                f"alias{index} = table{index}",
                f"alias{index}2 = alias{index}",
            ]
        )
    lines.extend(
        [
            "def target(raw, **kw):",
            "    x = alias0",
            "    return x[1](..., operator_evidence=raw, **kw)",
        ]
    )
    source = "\n".join(lines) + "\n"
    first = _b(source)
    second = _b(source)
    assert first == second
    assert first
