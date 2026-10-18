from __future__ import annotations

import itertools
import json
import math
import random
import subprocess
import sys
from fractions import Fraction
from typing import Any

import pytest

from quantforge.product.scale.statistics import (
    block_sign_diagnostic,
    cscv_pbo,
    dsr_not_applicable,
    exact_sign_tail,
)


def matrix(*, planted: bool = False) -> list[list[float]]:
    rng = random.Random(104729)
    result = [[float(rng.randrange(-100000, 100001)) for _ in range(16)]
              for _ in range(256)]
    if planted:
        for row in result:
            row[0] += 300000
    return result


def exact_ranks(values: list[list[float]], blocks: int) -> list[tuple[int, int]]:
    """Independent exact Fraction arithmetic; no production helper calls."""
    size = len(values) // blocks
    output = []
    for subset in itertools.combinations(range(blocks), blocks // 2):
        inside = [i for i in range(len(values)) if i // size in subset]
        outside = [i for i in range(len(values)) if i // size not in subset]
        first = [sum((Fraction(values[i][j]) for i in inside), Fraction()) / len(inside)
                 for j in range(len(values[0]))]
        second = [sum((Fraction(values[i][j]) for i in outside), Fraction()) / len(outside)
                  for j in range(len(values[0]))]
        winner = first.index(max(first))
        rank = 1 + sum(value < second[winner] for value in second)
        output.append((winner, rank))
    return output


def test_cscv_independent_exact_reference_null_and_planted() -> None:
    for planted in (False, True):
        values = matrix(planted=planted)
        actual = cscv_pbo(values)
        expected = exact_ranks(values, 8)
        assert actual["status"] == "DESCRIPTIVE"
        assert actual["combinations"] == 70
        assert [(int(row["selected_trial"]), row["oos_rank"])
                for row in actual["splits"]] == expected
        assert actual["overfit_combinations"] == (0 if planted else 47)
        assert actual["empirical_pbo"] == float(0 if planted else Fraction(47, 70))
        assert actual["validated_alpha"] is False
        assert actual["live_eligible"] is False


def test_hand_reference_and_median_classification() -> None:
    # Mathematical kernel example; deliberately below deployment policy floors.
    rows = [[9., 1., 2.], [7., 2., 3.], [-8., 3., 1.], [-7., 4., 2.]]
    actual = cscv_pbo(rows, blocks=4, min_trials=3, min_rows_per_block=1)
    assert actual["status"] == "DESCRIPTIVE"
    assert [row["oos_rank"] for row in actual["splits"]] == [1, 3, 3, 3, 3, 1]
    assert actual["overfit_combinations"] == 2
    assert actual["empirical_pbo"] == float(Fraction(1, 3))
    assert abs(actual["splits"][0]["logit"] + math.log(3)) < 1e-14


def test_cscv_column_permutation_scaling_and_complement_symmetry() -> None:
    values = matrix()
    first = cscv_pbo(values)
    permuted = cscv_pbo([list(reversed(row)) for row in values])
    scaled = cscv_pbo([[value * .005 for value in row] for row in values])
    reversed_blocks = cscv_pbo(list(reversed(values)))
    for other in (permuted, scaled, reversed_blocks):
        assert other["empirical_pbo"] == first["empirical_pbo"]
        assert other["overfit_combinations"] == first["overfit_combinations"]
    for original, other in zip(first["splits"], permuted["splits"], strict=True):
        assert int(other["selected_trial"]) == 15 - int(original["selected_trial"])
        assert other["oos_rank"] == original["oos_rank"]
    splits = {tuple(row["selected_blocks"]) for row in first["splits"]}
    assert all(tuple(i for i in range(8) if i not in subset) in splits for subset in splits)


@pytest.mark.parametrize("change,status", [
    ({"selection_design": "adaptive_path"}, "NOT_APPLICABLE"),
    ({"history_complete": False}, "BLOCKED"),
    ({"blocks": 3}, "BLOCKED"),
    ({"blocks": 0}, "BLOCKED"),
    ({"blocks": float("nan")}, "BLOCKED"),
    ({"blocks": 18}, "BLOCKED"),
    ({"max_combinations": 69}, "BLOCKED"),
    ({"min_trials": 17}, "INSUFFICIENT_DATA"),
    ({"min_rows_per_block": 33}, "INSUFFICIENT_DATA"),
    ({"trial_ids": ["same"] * 16}, "BLOCKED"),
    ({"trial_ids": 4}, "BLOCKED"),
])
def test_cscv_policy_attacks_refuse_scalar(change: dict[str, Any], status: str) -> None:
    actual = cscv_pbo(matrix(), **change)
    assert actual["status"] == status
    assert "empirical_pbo" not in actual
    json.dumps(actual, allow_nan=False)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, "1"])
def test_cscv_invalid_values_fail_closed(bad: Any) -> None:
    values: Any = matrix()
    values[0][0] = bad
    actual = cscv_pbo(values)
    assert actual["status"] == "BLOCKED"
    assert "empirical_pbo" not in actual


def test_cscv_empty_ragged_tiny_unequal_and_constant_inputs() -> None:
    assert cscv_pbo([])["status"] == "INSUFFICIENT_DATA"
    assert cscv_pbo([[1], [1, 2]])["status"] == "BLOCKED"
    assert cscv_pbo(matrix()[:160])["status"] == "DESCRIPTIVE"
    assert cscv_pbo(matrix()[:159])["status"] == "INSUFFICIENT_DATA"
    assert cscv_pbo(matrix()[:255])["status"] == "BLOCKED"
    values = matrix()
    for row in values:
        row[0] = 0
    assert cscv_pbo(values)["status"] == "UNDEFINED"


@pytest.mark.parametrize("affine", [1, -1, 2])
def test_cscv_identical_and_perfect_correlations_refuse(affine: int) -> None:
    values = matrix()
    for row in values:
        row[1] = affine * row[0] + 100
    actual = cscv_pbo(values)
    assert actual["status"] == "UNDEFINED"
    assert "correlated" in actual["reason"]


def test_cscv_rank_ties_are_not_broken_by_trial_id() -> None:
    values = matrix()
    for row in values[:128]:
        row[0] = row[1] + 10000000
    for row in values[128:]:
        row[0] = row[1]
    actual = cscv_pbo(values)
    assert actual["status"] == "UNDEFINED"
    assert "rank" in actual["reason"]
    assert "empirical_pbo" not in actual


def test_sign_tails_match_exhaustive_reference_and_are_monotone() -> None:
    for blocks in range(1, 9):
        patterns = list(itertools.product((0, 1), repeat=blocks))
        tails = []
        for k in range(blocks + 1):
            expected = sum(sum(pattern) >= k for pattern in patterns) / len(patterns)
            tails.append(exact_sign_tail(k, blocks))
            assert tails[-1] == expected
        assert tails == sorted(tails, reverse=True)
    assert exact_sign_tail(5, 6) == Fraction(7, 64)
    assert exact_sign_tail(12, 12) == Fraction(1, 4096)


def test_exact_null_control_familywise_bound_and_positive_control_power() -> None:
    # Frozen M=16, B=16. All fair-sign patterns evaluated analytically.
    rejected = sum(math.comb(16, k) for k in range(17)
                   if 16 * exact_sign_tail(k, 16) <= .05)
    assert rejected == 137
    per_test = Fraction(rejected, 2 ** 16)
    assert 16 * per_test <= Fraction(1, 20)  # no test-independence needed
    assert 1 - (1 - per_test) ** 16 == pytest.approx(.03292794699763546)
    power = sum(Fraction(math.comb(16, k)) * Fraction(19, 20) ** k *
                Fraction(1, 20) ** (16 - k) for k in range(14, 17))
    assert power == pytest.approx(.9570621465142274)
    assert power > Fraction(9, 10)
    values = [value for _ in range(16) for value in ([.01, -.002] * 10)]
    planted = block_sign_diagnostic(values, block_size=20, frozen_trial_budget=16)
    assert planted["familywise_adjusted_p"] == Fraction(1, 4096)
    assert planted["conditional_threshold_crossed"] is True
    assert planted["validated_alpha"] is False


def test_sign_zero_blocks_count_failure_tail_and_budget_are_declared() -> None:
    values = [1., -1.] * 10 + [value for _ in range(12) for value in ([.01, -.002] * 10)]
    one = block_sign_diagnostic(values + [100.], block_size=20, frozen_trial_budget=1)
    many = block_sign_diagnostic(values + [100.], block_size=20, frozen_trial_budget=100)
    assert one["zero_blocks"] == 1
    assert one["positive_blocks"] == 12
    assert one["blocks"] == 13
    assert one["unscored_tail_rows"] == 1
    assert many["familywise_adjusted_p"] >= one["familywise_adjusted_p"]
    assert one["status"] == "EXPLORATORY_CONDITIONAL"


@pytest.mark.parametrize("values,change,status", [
    ([], {}, "INSUFFICIENT_DATA"),
    ([0.] * 320, {}, "UNDEFINED"),
    ([.1] * 320, {}, "UNDEFINED"),
    ([.1, -.1] * 160, {"block_size": 19}, "INSUFFICIENT_DATA"),
    ([.1, -.1] * 100, {}, "INSUFFICIENT_DATA"),
    ([.1, -.1] * 160, {"history_complete": False}, "BLOCKED"),
    ([.1, -.1] * 160, {"frozen_trial_budget": 0}, "BLOCKED"),
    ([.1, -.1] * 160, {"frozen_trial_budget": True}, "BLOCKED"),
    ([.1, -.1] * 160, {"frozen_trial_budget": 1000001}, "BLOCKED"),
    ([.1, -.1] * 160, {"alpha": float("nan")}, "BLOCKED"),
    ([.1, -.1] * 160, {"selection_design": "posthoc"}, "BLOCKED"),
    ([float("inf")] + [.1, -.1] * 160, {}, "BLOCKED"),
    ([float("nan")] + [.1, -.1] * 160, {}, "BLOCKED"),
    ([1e308, 9e307] * 160, {}, "UNDEFINED"),
])
def test_sign_degenerate_and_invalid_inputs_refuse(values: list[float], change: dict[str, Any],
                                                   status: str) -> None:
    kwargs = {"block_size": 20, "frozen_trial_budget": 16, **change}
    actual = block_sign_diagnostic(values, **kwargs)
    assert actual["status"] == status
    assert "familywise_adjusted_p" not in actual
    json.dumps(actual, allow_nan=False)


def test_extreme_finite_outliers_and_dsr_never_claim_alpha() -> None:
    values = [1e100, -1e100] * 159 + [1., 2.]
    actual = block_sign_diagnostic(values, block_size=20, frozen_trial_budget=16)
    assert actual["status"] == "EXPLORATORY_CONDITIONAL"
    assert actual["validated_alpha"] is False
    json.dumps(actual, allow_nan=False)
    dsr = dsr_not_applicable()
    assert dsr["status"] == "NOT_APPLICABLE"
    assert "dsr" not in dsr


@pytest.mark.parametrize("positives,blocks", [(-1, 12), (13, 12), (True, 12), (1, 0), (1, 513)])
def test_invalid_exact_tail_counts_raise(positives: int, blocks: int) -> None:
    with pytest.raises(ValueError, match="sign counts"):
        exact_sign_tail(positives, blocks)


def test_statistics_reproducible_canonical_payloads() -> None:
    values = matrix()
    assert json.dumps(cscv_pbo(values), sort_keys=True) == json.dumps(cscv_pbo(values), sort_keys=True)
    stream = [item for _ in range(16) for item in ([.01, -.002] * 10)]
    kwargs = {"block_size": 20, "frozen_trial_budget": 16}
    assert block_sign_diagnostic(stream, **kwargs) == block_sign_diagnostic(stream, **kwargs)


def test_statistics_fresh_process_reproduction_is_byte_identical() -> None:
    code = (
        "import json,sys; from quantforge.product.scale.statistics import cscv_pbo; "
        "from quantforge.experiments import canonical_json; "
        "print(canonical_json(cscv_pbo(json.load(sys.stdin))))"
    )
    values = matrix()
    expected = json.dumps(cscv_pbo(values), sort_keys=True, separators=(",", ":"))
    output = subprocess.run([sys.executable, "-c", code], input=json.dumps(values),
                            capture_output=True, text=True, check=True, timeout=30)
    assert output.stdout.strip() == expected


def test_cscv_full_s16_enumeration_and_arithmetic_work_bound() -> None:
    rng = random.Random(99991)
    values = [[rng.uniform(-1, 1) for _ in range(12)] for _ in range(320)]
    actual = cscv_pbo(values, blocks=16)
    assert actual["status"] == "DESCRIPTIVE"
    assert len(actual["splits"]) == actual["combinations"] == 12870
    # No expensive scoring is entered when the declared work bound fails.
    large = [[0.] * 512 for _ in range(160)]
    blocked = cscv_pbo(large)
    assert blocked["status"] == "BLOCKED"
    assert "work" in blocked["reason"]
