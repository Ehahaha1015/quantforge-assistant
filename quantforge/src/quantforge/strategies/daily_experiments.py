"""Research-only daily candidates and dependence-aware exploratory diagnostics."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from random import Random
from typing import Any

from .chart_decision import context_relation
from .chart_evaluation import classification_metrics

METHODS = ("BASELINE_V2", "REGIME_ROUTER", "RELATIVE_CONFIRMATION", "CHANDELIER")


def relative_confirmation(
    decision: dict[str, Any],
    features: dict[str, Any],
    contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Filter an existing router decision; never create evidence or alter its parent."""
    if decision["policy"] != "REGIME_ROUTER":
        raise ValueError("relative experiment requires the frozen router parent")
    result = deepcopy(decision)
    result["experiment"] = "RELATIVE_CONFIRMATION"
    direction = result["direction"]
    if direction in {"BULLISH", "BEARISH"}:
        values = [features.get(f"relative_return{h}") for h in (5, 20)]
        missing = any(value is None for value in values)
        numbers = [Decimal(str(value)) for value in values if value is not None]
        if any(not number.is_finite() for number in numbers):
            raise ValueError("finite relative returns required")
        sign = 1 if direction == "BULLISH" else -1
        if missing or any(number * sign <= 0 for number in numbers):
            result["direction"] = "UNCLEAR"
            result["abstention_reason"] = (
                "RELATIVE_REFERENCE_MISSING" if missing else "RELATIVE_NOT_CONFIRMED"
            )
            result["context_relation"] = context_relation("UNCLEAR", contexts)
    return result


def diagnostic_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts explain filtering stages; they do not assign causal blame to ADX."""
    strata: dict[str, Any] = {}
    for field in ("market_state", "context_relation", "primary_group", "actual"):
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            buckets[row[field]].append(row)
        strata[field] = {
            key: classification_metrics(
                [r["actual"] for r in group], [r["prediction"] for r in group]
            )
            for key, group in sorted(buckets.items())
        }
    bullish = [row for row in rows if row["actual"] == "BULLISH"]
    return {
        "strata": strata,
        "abstention_reasons": dict(Counter(r["abstention_reason"] for r in rows)),
        "risk_flags": dict(Counter(flag for r in rows for flag in r["risk_flags"])),
        "entry_first_reason": dict(Counter(r["entry_reason"] for r in rows)),
        "bullish_funnel": {
            "actual_bullish": len(bullish),
            "predicted_bullish": sum(r["prediction"] == "BULLISH" for r in bullish),
            "entry_eligible": sum(
                r["entry_action"] == "CONSIDER_ENTRY" for r in bullish
            ),
            "not_bullish_direction": dict(
                Counter(
                    r["prediction"] for r in bullish if r["prediction"] != "BULLISH"
                )
            ),
            "bullish_but_entry_blocked": dict(
                Counter(
                    r["entry_reason"]
                    for r in bullish
                    if r["prediction"] == "BULLISH"
                    and r["entry_action"] != "CONSIDER_ENTRY"
                )
            ),
        },
    }


def _gains(
    rows: list[dict[str, Any]], method: str, majority: str
) -> dict[str, float | None]:
    actual = [r["actual"] for r in rows]
    predictions = [r["predictions"][method] for r in rows]
    current = classification_metrics(actual, predictions)
    parent = classification_metrics(
        actual, [r["predictions"]["REGIME_ROUTER"] for r in rows]
    )
    covered = [r for r in rows if r["predictions"][method] != "UNCLEAR"]
    accuracy = current["accuracy_when_covered"]
    majority_accuracy = (
        sum(r["actual"] == majority for r in covered) / len(covered)
        if covered
        else None
    )
    balance, baseline = (
        current["balanced_accuracy_all"],
        parent["balanced_accuracy_all"],
    )
    return {
        "accuracy_minus_same_coverage_majority": (
            accuracy - majority_accuracy
            if accuracy is not None and majority_accuracy is not None
            else None
        ),
        "balanced_accuracy_minus_router": balance - baseline
        if balance is not None and baseline is not None
        else None,
    }


def block_uncertainty(
    rows: list[dict[str, Any]],
    method: str,
    majority: str,
    *,
    repetitions: int = 500,
    block_days: int = 20,
    seed: int = 20261018,
) -> dict[str, Any]:
    """Paired moving date blocks keep every symbol for a sampled date together."""
    if repetitions < 1 or block_days < 1:
        raise ValueError("positive repetitions and block size required")
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[row["origin"]].append(row)
    dates = sorted(by_date)
    observed = _gains(rows, method, majority)
    samples: dict[str, list[float]] = {key: [] for key in observed}
    rng = Random(seed)
    if len(dates) >= block_days * 2:
        for _ in range(repetitions):
            selected: list[str] = []
            while len(selected) < len(dates):
                start = rng.randrange(len(dates) - block_days + 1)
                selected.extend(dates[start : start + block_days])
            resampled = [row for day in selected[: len(dates)] for row in by_date[day]]
            for key, value in _gains(resampled, method, majority).items():
                if value is not None:
                    samples[key].append(value)
    intervals = {}
    for key, values in samples.items():
        values.sort()
        intervals[key] = {
            "observed": observed[key],
            "valid_resamples": len(values),
            "percentile_95": [
                values[int((len(values) - 1) * p)] for p in (0.025, 0.975)
            ]
            if values
            else None,
        }
    return {
        "method": "PAIRED_COMMON_DATE_MOVING_BLOCK_EXPLORATORY",
        "unique_origin_dates": len(dates),
        "observations": len(rows),
        "block_days": block_days,
        "repetitions": repetitions,
        "seed": seed,
        "status": "EXPLORATORY"
        if len(dates) >= block_days * 2
        else "INSUFFICIENT_DATE_BLOCKS",
        "intervals": intervals,
        "leave_one_symbol_out": {
            symbol: _gains([r for r in rows if r["symbol"] != symbol], method, majority)
            for symbol in sorted({r["symbol"] for r in rows})
        },
        "limitations": "Not independent trials; block length fixed, small windows and selection bias remain.",
    }


def forward_eligible(
    origin: datetime, label_end: datetime, frozen_at: datetime, seen_through: datetime
) -> bool:
    if any(
        value.tzinfo is None or value.utcoffset() is None
        for value in (origin, label_end, frozen_at, seen_through)
    ):
        raise ValueError("aware forward evidence timestamps required")
    if label_end <= origin:
        raise ValueError("label must follow prediction origin")
    return origin > frozen_at and origin > seen_through
