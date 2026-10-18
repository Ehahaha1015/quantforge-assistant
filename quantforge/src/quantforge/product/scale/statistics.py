"""Source-bound, deterministic diagnostics with no alpha/eligibility authority.

CSCV uses Bailey et al. Algorithm 2.3 (author manuscript 2015-02-27).
The sign tail and Bonferroni bound follow the retained NIST definitions.
Applicability floors and numerical tolerances are explicit local policy.
"""

from __future__ import annotations

import hashlib
import itertools
import math
from collections.abc import Sequence
from typing import Any

from quantforge.experiments import canonical_json

PBO_SOURCE = {
    "url": "https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf",
    "definition": "Algorithm 2.3, pp. 11-12; limitations pp. 23-24",
    "sha256": "d8bfbadaaedb430d9dec929646d92f3580fdf18238f34121ae7738fa1c69a63d",
}
SIGN_SOURCE = {
    "url": "https://www.itl.nist.gov/div898/software/dataplot/refman1/auxillar/signtest.htm",
    "sha256": "b4007869d21326c8c8175a9b3bf3d713cab8e3982975cc248c998912140cc653",
}
BONFERRONI_SOURCE = {
    "url": "https://www.itl.nist.gov/div898/handbook/prc/section4/prc473.htm",
    "sha256": "955637b57e2a30c9cab24397d706a3e277a1baa991a90b907b35a2c9b40412e4",
}
DSR_SOURCE = {
    "url": "https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf",
    "definition": "Equation (2), p. 8; Appendix 3, pp. 14-15",
    "sha256": "ca4a1e834a2954b84eb560776e903329cd9cef318d458aadf6d67bc8166d10b5",
}
_TIE_ABS = 1e-15
_TIE_REL = 1e-12
_CORRELATION_TOLERANCE = 1e-12


def _base(method: str, source: Any) -> dict[str, Any]:
    return {
        "schema_version": "research-scale-statistics-v1",
        "method": method,
        "source": source,
        "validated_alpha": False,
        "live_eligible": False,
    }


def _refuse(base: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {**base, "status": status, "reason": reason}


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _finite_values(values: Sequence[float]) -> list[float] | None:
    if isinstance(values, (str, bytes)):
        return None
    try:
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               for value in values):
            return None
        output = [float(value) for value in values]
        return output if all(math.isfinite(value) for value in output) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _mean(values: Sequence[float]) -> float:
    # Divide before summing to avoid overflowing a finite mean.
    return math.fsum(value / len(values) for value in values)


def _center_scaled(values: Sequence[float]) -> list[float] | None:
    scale = max(abs(value) for value in values)
    if scale == 0:
        return None
    scaled = [value / scale for value in values]
    mean = _mean(scaled)
    centered = [value - mean for value in scaled]
    return centered if math.fsum(value * value for value in centered) > 0 else None


def cscv_pbo(
    matrix: Sequence[Sequence[float]],
    *,
    blocks: int = 8,
    trial_ids: Sequence[str] | None = None,
    selection_design: str = "fixed_grid",
    history_complete: bool = True,
    min_trials: int = 12,
    min_rows_per_block: int = 20,
    max_combinations: int = 12870,
) -> dict[str, Any]:
    """Empirical CSCV rank frequency for a predeclared fixed mean-return grid.

    This reranks synchronous, already causal net-return streams. It does not
    fit on shuffled history, estimate future profits, or authorize alpha.
    Adaptive intermediate proposals are outside the source's applicability.
    """
    base = _base("cscv_mean_net_return_pbo", PBO_SOURCE)
    if history_complete is not True:
        return _refuse(base, "BLOCKED", "incomplete authoritative trial history")
    if selection_design != "fixed_grid":
        return _refuse(base, "NOT_APPLICABLE", "adaptive search steps are not fixed-grid trials")
    if (not all(_positive_integer(v) for v in
                (blocks, min_trials, min_rows_per_block, max_combinations))
            or blocks < 4 or blocks % 2 or min_trials < 2
            or blocks > 24 or max_combinations > 12870):
        return _refuse(base, "BLOCKED", "invalid or unbounded CSCV policy")
    combinations = math.comb(blocks, blocks // 2)
    if combinations > max_combinations:
        return _refuse(base, "BLOCKED", "CSCV combinations exceed declared resource bound")
    base["policy"] = {
        "blocks": blocks, "min_trials": min_trials,
        "min_rows_per_block": min_rows_per_block,
        "max_combinations": max_combinations,
        "tie_absolute_tolerance": _TIE_ABS,
        "tie_relative_tolerance": _TIE_REL,
        "perfect_correlation_tolerance": _CORRELATION_TOLERANCE,
        "metric": "arithmetic_mean_net_return",
    }
    try:
        rows = len(matrix)
        width = len(matrix[0]) if rows else 0
        if any(len(row) != width for row in matrix):
            return _refuse(base, "BLOCKED", "matrix is not rectangular and synchronous")
        if rows > 100000 or width > 512 or rows * width > 2000000:
            return _refuse(base, "BLOCKED", "matrix exceeds deterministic resource bound")
        values = [_finite_values(row) for row in matrix]
    except (TypeError, IndexError):
        return _refuse(base, "BLOCKED", "invalid matrix")
    if any(row is None for row in values):
        return _refuse(base, "BLOCKED", "non-finite or nonnumeric return")
    if width < min_trials or rows < blocks * min_rows_per_block:
        return _refuse(base, "INSUFFICIENT_DATA", "local trial/row floors not met")
    if combinations * blocks * width > 20000000 or rows * width * width > 20000000:
        return _refuse(base, "BLOCKED", "CSCV arithmetic work exceeds deterministic resource bound")
    if rows % blocks:
        return _refuse(base, "BLOCKED", "equal blocks required; no silent row truncation")
    try:
        ids = list(trial_ids) if trial_ids is not None else [str(i) for i in range(width)]
    except TypeError:
        return _refuse(base, "BLOCKED", "invalid trial identities")
    if (len(ids) != width or any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != width):
        return _refuse(base, "BLOCKED", "trial identities must be unique and complete")
    clean = [row for row in values if row is not None]
    columns = [[row[j] for row in clean] for j in range(width)]
    centered = [_center_scaled(column) for column in columns]
    if any(column is None for column in centered):
        return _refuse(base, "UNDEFINED", "constant/zero-volatility trial")
    nonconstant = [column for column in centered if column is not None]
    lengths = [math.sqrt(math.fsum(v * v for v in column)) for column in nonconstant]
    for first in range(width):
        for second in range(first):
            correlation = math.fsum(a * b for a, b in
                                    zip(nonconstant[first], nonconstant[second], strict=True))
            correlation /= lengths[first] * lengths[second]
            if abs(correlation) >= 1 - _CORRELATION_TOLERANCE:
                return _refuse(base, "UNDEFINED", "identical or perfectly correlated trials")
    block_size = rows // blocks
    # Equal block lengths let us reuse block means; no approximate/subsampled
    # combinations and no order-dependent parallel reduction are introduced.
    block_means = [[_mean(column[i * block_size:(i + 1) * block_size])
                    for column in columns] for i in range(blocks)]
    split_results: list[dict[str, Any]] = []
    for chosen in itertools.combinations(range(blocks), blocks // 2):
        selected = set(chosen)
        complement = [i for i in range(blocks) if i not in selected]
        inside = [_mean([block_means[i][trial] for i in chosen]) for trial in range(width)]
        outside = [_mean([block_means[i][trial] for i in complement]) for trial in range(width)]
        best = max(inside)
        winners = [i for i, score in enumerate(inside) if
                   math.isclose(score, best, rel_tol=_TIE_REL, abs_tol=_TIE_ABS)]
        if len(winners) != 1:
            return _refuse(base, "UNDEFINED", "in-sample winner has ambiguous tied rank")
        winner = winners[0]
        score = outside[winner]
        if sum(math.isclose(other, score, rel_tol=_TIE_REL, abs_tol=_TIE_ABS)
               for other in outside) != 1:
            return _refuse(base, "UNDEFINED", "selected out-of-sample rank is ambiguous")
        rank = 1 + sum(other < score for other in outside)
        split_results.append({
            "selected_blocks": list(chosen), "selected_trial": ids[winner],
            "oos_rank": rank, "relative_rank": rank / (width + 1),
            "logit": math.log(rank / (width + 1 - rank)),
            "overfit": 2 * rank <= width + 1,
        })
    overfit = sum(item["overfit"] for item in split_results)
    identity = {"matrix": clean, "trial_ids": ids, "selection_design": selection_design}
    return {
        **base, "status": "DESCRIPTIVE", "rows": rows, "trials": width,
        "block_size": block_size, "combinations": combinations,
        "overfit_combinations": overfit, "empirical_pbo": overfit / combinations,
        "rank_frequency_resolution": 1 / combinations, "splits": split_results,
        "input_sha256": hashlib.sha256(canonical_json(identity).encode()).hexdigest(),
        "assumptions": ["fixed predeclared complete synchronous candidate grid",
                        "historical partitions represent selection behavior"],
        "limitations": ["not a p-value or future-profit probability",
                        "overlapping combinations are not independent Bernoulli observations",
                        "local sample floors do not guarantee statistical calibration",
                        "serial dependence and changing regimes can limit representativeness"],
    }


def exact_sign_tail(positives: int, blocks: int) -> float:
    """Exact one-sided binomial tail; integer arithmetic before final division."""
    if (not _positive_integer(blocks) or blocks > 512
            or not isinstance(positives, int) or isinstance(positives, bool)
            or not 0 <= positives <= blocks):
        raise ValueError("invalid bounded sign counts")
    return sum(math.comb(blocks, k) for k in range(positives, blocks + 1)) / 2 ** blocks


def block_sign_diagnostic(
    returns: Sequence[float],
    *,
    block_size: int,
    frozen_trial_budget: int,
    selection_design: str = "adaptive_path",
    history_complete: bool = True,
    min_blocks: int = 12,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Exploratory positive-block probability diagnostic, never an alpha gate.

    H0: independent block signs with P(net block sum > 0) <= 1/2.
    Zero blocks count as failures, a conservative variant of excluded ties.
    Bonferroni cannot fix dependent blocks or adaptively defined hypotheses.
    """
    base = _base("exact_block_sign_frozen_budget_bonferroni", [SIGN_SOURCE, BONFERRONI_SOURCE])
    if history_complete is not True:
        return _refuse(base, "BLOCKED", "incomplete authoritative trial history")
    if (not all(_positive_integer(v) for v in (block_size, frozen_trial_budget, min_blocks))
            or min_blocks < 2 or frozen_trial_budget > 1000000
            or not isinstance(alpha, (int, float))
            or isinstance(alpha, bool) or not math.isfinite(alpha) or not 0 < alpha < 1
            or selection_design not in {"fixed_grid", "adaptive_path"}):
        return _refuse(base, "BLOCKED", "invalid sign-test policy or frozen budget")
    values = _finite_values(returns)
    if values is None:
        return _refuse(base, "BLOCKED", "non-finite or nonnumeric return")
    blocks = len(values) // block_size
    if block_size < 20 or blocks < min_blocks:
        return _refuse(base, "INSUFFICIENT_DATA", "local block sample floors not met")
    if blocks > 512 or len(values) > 100000:
        return _refuse(base, "BLOCKED", "sign-test resource bound exceeded")
    if _center_scaled(values) is None:
        return _refuse(base, "UNDEFINED", "constant/zero-volatility return stream")
    try:
        totals = [math.fsum(values[i * block_size:(i + 1) * block_size])
                  for i in range(blocks)]
    except OverflowError:
        return _refuse(base, "UNDEFINED", "block arithmetic overflow")
    if not all(math.isfinite(total) for total in totals):
        return _refuse(base, "UNDEFINED", "non-finite block aggregate")
    positives = sum(total > 0 for total in totals)
    raw = exact_sign_tail(positives, blocks)
    numerator = sum(math.comb(blocks, k) for k in range(positives, blocks + 1))
    adjusted = min(1.0, numerator * frozen_trial_budget / 2 ** blocks)
    return {
        **base, "status": "EXPLORATORY_CONDITIONAL",
        "positive_block_quantity": "P(predeclared block net simple-return sum > 0)",
        "null": "independent block signs; every positive probability <= 1/2",
        "selection_design": selection_design,
        "block_size": block_size, "blocks": blocks,
        "positive_blocks": positives, "zero_blocks": sum(total == 0 for total in totals),
        "unscored_tail_rows": len(values) % block_size,
        "tail_rule": "exclude final incomplete block as predeclared policy",
        "frozen_trial_budget": frozen_trial_budget,
        "uncorrected_p": raw, "familywise_adjusted_p": adjusted,
        "exact_tail_numerator": numerator, "exact_tail_denominator": 2 ** blocks,
        "alpha": alpha, "conditional_threshold_crossed": adjusted <= alpha,
        "input_sha256": hashlib.sha256(canonical_json(values).encode()).hexdigest(),
        "assumptions": ["independent predeclared block signs under the null",
                        "zero-centered symmetry is sufficient for fair nonzero signs",
                        "frozen budget covers all hypotheses including failed/exposed attempts"],
        "limitations": ["block independence is unverified for market returns",
                        "adaptive hypothesis construction can invalidate each base p-value",
                        "Bonferroni does not repair invalid base p-values",
                        "sign statistic discards return magnitude and does not test mean Sharpe",
                        "conditional threshold never grants validated-alpha or eligibility"],
    }


def dsr_not_applicable() -> dict[str, Any]:
    """The current adaptive correlated trials do not justify independent N."""
    return _refuse(_base("deflated_sharpe_ratio", DSR_SOURCE), "NOT_APPLICABLE",
                   "no justified independent-trial count or complete comparable Sharpe distribution")
