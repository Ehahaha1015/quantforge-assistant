"""Daily-five chronological evidence and report-plan replay, with fixed candidates."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .chart_decision import GROUP_IDS
from .chart_evaluation import (
    LABELS,
    classification_metrics,
    label_return,
    label_threshold,
)
from .chart_suite import generate_chart_signals, rules
from .chart_synthesis import synthesize
from .daily_five import POLICIES, prediction, prepare_inputs
from .daily_replay import replay
from .moving_average import _single_symbol_bars

VERSION = "daily-five-validation-v1"


def build_history(
    daily: pl.DataFrame,
    weekly: pl.DataFrame | None,
    benchmark: pl.DataFrame | None,
    *,
    through: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    frame = _single_symbol_bars(daily)
    if through is not None:
        frame = frame.filter(pl.col("timestamp") < through)
    generated = {
        rule.id: generate_chart_signals(frame, rule)["value"].to_list()
        for rule in rules()
    }
    weekly_frame = _single_symbol_bars(weekly) if weekly is not None else None
    cache: dict[int, dict[str, Any]] = {}
    result = {}
    previous = None
    zone = ZoneInfo("Asia/Shanghai")
    for index in range(79, frame.height):
        prefix = frame.head(index + 1)
        at = frame["timestamp"][index]
        day = at.astimezone(zone).date()
        states = {
            key: (int(v[index]), int(v[index - 1])) for key, v in generated.items()
        }
        base = synthesize(prefix, "1d", states)
        inputs = prepare_inputs(prefix, benchmark, base=base, as_of=day)
        contexts: list[dict[str, Any]] = []
        if weekly_frame is not None:
            friday = day - timedelta(days=(day.weekday() - 4) % 7)
            cutoff = datetime.combine(friday, time(16), zone).astimezone(at.tzinfo)
            closed = weekly_frame.filter(pl.col("timestamp") <= cutoff)
            if closed.height:
                if closed.height not in cache:
                    cache[closed.height] = synthesize(closed, "1w")
                contexts = [
                    {
                        "timeframe": "1w",
                        "status": "AVAILABLE",
                        "synthesis": cache[closed.height],
                    }
                ]
        if not contexts:
            contexts = [{"timeframe": "1w", "status": "UNAVAILABLE", "synthesis": None}]
        decisions = {
            policy: prediction(inputs, contexts, policy, previous)
            for policy in POLICIES
        }
        diagnostics = {}
        for policy in POLICIES:
            diagnostics[policy] = {
                **{
                    key: prediction(inputs, contexts, policy, previous, (key,))[
                        "direction"
                    ]
                    for key in GROUP_IDS
                },
                **{
                    "WITHOUT_" + key: prediction(
                        inputs,
                        contexts,
                        policy,
                        previous,
                        tuple(x for x in GROUP_IDS if x != key),
                    )["direction"]
                    for key in GROUP_IDS
                },
            }
        result[at.isoformat()] = {
            "inputs": inputs,
            "contexts": contexts,
            "decisions": decisions,
            "diagnostics": diagnostics,
        }
        previous = inputs
    return result


def observations(
    frame: pl.DataFrame,
    history: dict[str, dict[str, Any]],
    start: datetime,
    end: datetime | None,
) -> list[dict[str, Any]]:
    rows = []
    for index in range(79, frame.height - 5):
        at = frame["timestamp"][index]
        label_end = frame["timestamp"][index + 5]
        if at < start or (end is not None and label_end >= end):
            continue
        sample = history.get(at.isoformat())
        if sample is None:
            continue
        values = sample["inputs"]["features"]
        if not values:
            continue
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            ref = Decimal(values["close"])
            atr = Decimal(values["atr14"])
            threshold = label_threshold(ref, atr, 5)
            change = Decimal(str(frame["close"][index + 5])) / ref - 1
            actual = label_return(change, threshold)
            momentum = label_return(
                ref / Decimal(str(frame["close"][index - 20])) - 1, threshold
            )
        rows.append(
            {
                "origin": at.isoformat(),
                "label_end": label_end.isoformat(),
                "actual": actual,
                "future_return": str(change),
                "threshold": str(threshold),
                "predictions": {
                    p: sample["decisions"][p]["direction"] for p in POLICIES
                },
                "entry_quality_eligible": sample["inputs"]["quality"]["eligible"],
                "simple_momentum": momentum,
                "diagnostics": sample["diagnostics"],
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]], policy: str, majority: str) -> dict[str, Any]:
    actual = [r["actual"] for r in rows]
    pred = [r["predictions"][policy] for r in rows]
    result = classification_metrics(actual, pred)
    common = [r for r in rows if r["predictions"][policy] != "UNCLEAR"]
    labels = [r["actual"] for r in common]
    result["majority_same_coverage"] = classification_metrics(
        labels, [majority] * len(common)
    )
    result["momentum_same_coverage"] = classification_metrics(
        labels, [r["simple_momentum"] for r in common]
    )
    result["data_coverage"] = (
        sum(r["entry_quality_eligible"] for r in rows) / len(rows) if rows else 0
    )
    result["diagnostics"] = {
        key: classification_metrics(
            actual, [r["diagnostics"][policy][key] for r in rows]
        )
        for key in (*GROUP_IDS, *("WITHOUT_" + x for x in GROUP_IDS))
    }
    return result


def phase_replay(
    frame: pl.DataFrame,
    history: dict[str, dict[str, Any]],
    policy: str,
    start: datetime,
    end: datetime | None,
    *,
    exit_policy: str = "ATR_TREND_TIME",
    friction_bps: int = 30,
    mode: str = "REPORT_PLAN",
    asset_class: str = "STOCK",
) -> dict[str, Any] | None:
    window = frame.filter(pl.col("timestamp") >= start)
    if end is not None:
        window = window.filter(pl.col("timestamp") < end)
    if window.height < 2:
        return None
    snapshots = {
        at: {"inputs": v["inputs"], "decision": v["decisions"][policy]}
        for at, v in history.items()
    }
    return replay(
        window,
        snapshots,
        exit_policy=exit_policy,
        friction_bps=friction_bps,
        tick_size="0.001" if asset_class == "ETF" else "0.01",
        mode=mode,
    )


def training_majority(rows: list[dict[str, Any]]) -> str:
    counts = Counter(r["actual"] for r in rows)
    return max(LABELS, key=lambda x: counts[x]) if rows else "UNCLEAR"
