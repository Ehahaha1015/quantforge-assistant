"""Chronological, purged evaluation of frozen chart research rules; no fitting."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.data.bars import logical_content_hash

from .chart_decision import GROUP_IDS, decide
from .chart_suite import generate_chart_signals, legacy_conditional_plan, rules
from .chart_synthesis import VERSION as SYNTHESIS_VERSION
from .chart_synthesis import synthesize
from .moving_average import _single_symbol_bars

VERSION = "chart-evaluation-v3"
LABELS = ("BULLISH", "RANGE", "BEARISH")
HORIZONS = {"60m": (1, 4), "1d": (1, 5, 20), "1w": (1, 4)}


def label_threshold(reference: Decimal, atr: Decimal, horizon: int) -> Decimal:
    if (
        not reference.is_finite()
        or not atr.is_finite()
        or reference <= 0
        or atr < 0
        or type(horizon) is not int
        or not 1 <= horizon <= 20
    ):
        raise ValueError(
            "positive reference, nonnegative ATR and bounded horizon required"
        )
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        return Decimal("0.5") * Decimal(horizon).sqrt() * atr / reference


def label_return(value: Decimal, threshold: Decimal) -> str:
    if not value.is_finite() or not threshold.is_finite() or threshold < 0:
        raise ValueError("finite return and nonnegative threshold required")
    return (
        "BULLISH" if value > threshold else "BEARISH" if value < -threshold else "RANGE"
    )


def purged_phase(
    origin: datetime, end: datetime, first: datetime, second: datetime
) -> str | None:
    if (
        any(
            x.tzinfo is None or x.utcoffset() is None
            for x in (origin, end, first, second)
        )
        or not origin < end
        or not first < second
    ):
        raise ValueError("ordered aware evaluation timestamps required")
    if origin < first:
        return "TRAIN" if end < first else None
    if origin < second:
        return "VALIDATION" if end < second else None
    return "TEST"


def _statistics(rows: list[dict[str, Any]], method: str) -> dict[str, Any]:
    matrix = {pred: {actual: 0 for actual in LABELS} for pred in (*LABELS, "UNCLEAR")}
    for row in rows:
        matrix[row["predictions"][method]][row["actual"]] += 1
    covered = sum(sum(matrix[x].values()) for x in LABELS)
    correct = sum(matrix[x][x] for x in LABELS)
    per_class = {}
    for label in LABELS:
        predicted = sum(matrix[label].values())
        actual = sum(matrix[x][label] for x in matrix)
        per_class[label] = {
            "predicted": predicted,
            "actual": actual,
            "precision": matrix[label][label] / predicted if predicted else None,
            "recall": matrix[label][label] / actual if actual else None,
        }
    recalls = [x["recall"] for x in per_class.values() if x["recall"] is not None]
    f1 = []
    covered_recalls = []
    covered_f1 = []
    for label in LABELS:
        tp = matrix[label][label]
        fp = sum(matrix[label].values()) - tp
        fn = sum(matrix[x][label] for x in matrix) - tp
        f1.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0)
        covered_actual = sum(matrix[x][label] for x in LABELS)
        covered_fn = covered_actual - tp
        if covered_actual:
            covered_recalls.append(tp / covered_actual)
        covered_f1.append(
            2 * tp / (2 * tp + fp + covered_fn) if 2 * tp + fp + covered_fn else 0.0
        )
    return {
        "balanced_accuracy_all": sum(recalls) / len(recalls) if recalls else None,
        "macro_f1_all": sum(f1) / 3 if rows else None,
        "balanced_accuracy_covered": sum(covered_recalls) / len(covered_recalls)
        if covered_recalls
        else None,
        "macro_f1_covered": sum(covered_f1) / 3 if covered else None,
        "metric_scope": "ABSTENTIONS_ARE_MISSES;MACRO_F1_FIXED_THREE_LABELS",
        "samples": len(rows),
        "covered": covered,
        "abstained": len(rows) - covered,
        "coverage": covered / len(rows) if rows else 0,
        "accuracy_when_covered": correct / covered if covered else None,
        "correct_over_all": correct / len(rows) if rows else None,
        "confusion": matrix,
        "per_class": per_class,
    }


def classification_metrics(actual: list[str], predictions: list[str]) -> dict[str, Any]:
    if (
        len(actual) != len(predictions)
        or any(x not in LABELS for x in actual)
        or any(x not in {*LABELS, "UNCLEAR"} for x in predictions)
    ):
        raise ValueError("aligned declared labels and predictions required")
    return _statistics(
        [
            {"actual": a, "predictions": {"model": p}}
            for a, p in zip(actual, predictions, strict=True)
        ],
        "model",
    )


def _without_group(
    groups: list[dict[str, Any]], removed: str, risk: bool, market_state: str
) -> str:
    return str(
        decide(
            groups,
            market_state,
            ["RISK_FILTER"] if risk else [],
            tuple(x for x in GROUP_IDS if x != removed),
        )["direction"]
    )


def evaluate_history(
    datasets: dict[str, pl.DataFrame], timeframe: str
) -> dict[str, Any]:
    """All assets share calendar split boundaries; labels crossing them are purged."""
    if timeframe not in HORIZONS or not datasets:
        raise ValueError("declared timeframe and datasets required")
    frames = {name: _single_symbol_bars(frame) for name, frame in datasets.items()}
    timestamps = sorted({at for frame in frames.values() for at in frame["timestamp"]})
    if len(timestamps) < 100:
        raise ValueError("at least 100 unique closed-bar timestamps required")
    first, second = (
        timestamps[len(timestamps) * 60 // 100],
        timestamps[len(timestamps) * 80 // 100],
    )
    samples: list[dict[str, Any]] = []
    purged = {str(h): 0 for h in HORIZONS[timeframe]}
    forecasts: dict[str, dict[int, dict[str, str]]] = {}
    for name, frame in frames.items():
        generated = {
            rule.id: generate_chart_signals(frame, rule)["value"].to_list()
            for rule in rules()
        }
        forecasts[name] = {}
        for index in range(79, frame.height):
            prefix = frame.head(index + 1)
            states = {
                key: (int(values[index]), int(values[index - 1]))
                for key, values in generated.items()
            }
            combined = synthesize(prefix, timeframe, states)
            old = legacy_conditional_plan(
                prefix, timeframe=timeframe, horizon_bars=HORIZONS[timeframe][0]
            )
            predictions = {
                "COMBINED": combined["direction"],
                "LEGACY_ENTRY_ONLY": "BULLISH"
                if old["action"] == "CONSIDER_ENTRY"
                else "UNCLEAR",
            }
            for group in combined["groups"]:
                predictions[group["id"]] = decide(
                    combined["groups"],
                    combined["market_state"],
                    combined["risk_flags"],
                    (group["id"],),
                )["direction"]
                predictions["WITHOUT_" + group["id"]] = _without_group(
                    combined["groups"],
                    group["id"],
                    bool(combined["risk_flags"]),
                    combined["market_state"],
                )
            indicators = combined["indicators"]
            reference = Decimal(indicators["reference_close"])
            atr = Decimal(indicators["atr14"])
            with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                momentum = reference / Decimal(str(frame["close"][index - 20])) - 1
                predictions["SIMPLE_MOMENTUM"] = label_return(
                    momentum, label_threshold(reference, atr, 1)
                )
            forecasts[name][index] = predictions
            for horizon in HORIZONS[timeframe]:
                if index + horizon >= frame.height:
                    continue
                origin, end = (
                    frame["timestamp"][index],
                    frame["timestamp"][index + horizon],
                )
                phase = purged_phase(origin, end, first, second)
                if phase is None:
                    purged[str(horizon)] += 1
                    continue
                with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                    future_return = (
                        Decimal(str(frame["close"][index + horizon])) / reference - 1
                    )
                    threshold = label_threshold(reference, atr, horizon)
                    label = label_return(future_return, threshold)
                samples.append(
                    {
                        "symbol": name,
                        "horizon_bars": horizon,
                        "origin": origin.isoformat(),
                        "label_end": end.isoformat(),
                        "phase": phase,
                        "actual": label,
                        "future_return": str(future_return),
                        "threshold": str(threshold),
                        "predictions": dict(predictions),
                    }
                )
    methods = [
        "COMBINED",
        "TREND",
        "BREAKOUT",
        "REVERSION",
        "SIMPLE_MOMENTUM",
        "LEGACY_ENTRY_ONLY",
        "WITHOUT_TREND",
        "WITHOUT_BREAKOUT",
        "WITHOUT_REVERSION",
        "MAJORITY_TRAIN",
    ]
    summaries = {}
    for horizon in HORIZONS[timeframe]:
        selected = [x for x in samples if x["horizon_bars"] == horizon]
        train = [x for x in selected if x["phase"] == "TRAIN"]
        counts = Counter(x["actual"] for x in train)
        majority = max(LABELS, key=lambda x: counts[x]) if train else "UNCLEAR"
        for row in selected:
            row["predictions"]["MAJORITY_TRAIN"] = majority
        summaries[str(horizon)] = {
            phase: {
                method: _statistics(
                    [x for x in selected if x["phase"] == phase], method
                )
                for method in methods
            }
            for phase in ("TRAIN", "VALIDATION", "TEST")
        }
    replays = []
    for name, frame in frames.items():
        test_indices = [i for i, at in enumerate(frame["timestamp"]) if at >= second]
        if len(test_indices) < 2:
            continue
        start = test_indices[0]
        window = frame.slice(start)
        for method in ("COMBINED", "LEGACY_ENTRY_ONLY", "BUY_AND_HOLD", "CASH"):
            desired = [
                1.0
                if method == "BUY_AND_HOLD"
                else 0.0
                if method == "CASH"
                else float(forecasts[name].get(i, {}).get(method) == "BULLISH")
                for i in range(start, frame.height)
            ]
            signals = window.select("symbol", "timestamp").with_columns(
                pl.Series("value", desired, dtype=pl.Float64)
            )
            for fee in (10, 30):
                replay = run_vectorized_backtest(
                    window,
                    signals,
                    BacktestConfig(
                        fee_bps=fee,
                        periods_per_year={"60m": 1008, "1d": 252, "1w": 52}[timeframe],
                    ),
                )
                replays.append(
                    {
                        "symbol": name,
                        "method": method,
                        "fee_bps": fee,
                        "price_return": replay.metrics.total_return,
                        "max_drawdown": replay.metrics.max_drawdown,
                        "turnover": replay.metrics.turnover,
                    }
                )
    return {
        "schema_version": VERSION,
        "timeframe": timeframe,
        "split_boundaries": [first.isoformat(), second.isoformat()],
        "assets": {
            name: {
                "bars": frame.height,
                "bars_sha256": logical_content_hash(frame),
                "first": frame["timestamp"][0].isoformat(),
                "last": frame["timestamp"][-1].isoformat(),
            }
            for name, frame in frames.items()
        },
        "selection": "FROZEN_RULES_NO_PARAMETER_SEARCH",
        "attempts": [SYNTHESIS_VERSION],
        "summaries": summaries,
        "purged_boundary_samples": purged,
        "replays": replays,
        "samples": samples,
        "forecast_validated": False,
        "execution_qualified": False,
        "limitations": [
            "Small selected universe; retrospective validation is not forward evidence.",
            "Overlapping horizons are correlated; no independent-sample confidence claim.",
            "Raw unadjusted prices; corporate actions/trading calendar remain unverified.",
            "Price-series replay excludes T+1, limit/halts, lots and fill availability.",
            "Replay is binary long/flat direction exposure, not a validated stop/target execution strategy.",
            "Hourly rolling history and per-horizon sample counts limit representativeness.",
        ],
    }


def freeze_target(
    *,
    direction: str,
    timeframe: str,
    horizon: int,
    origin: datetime,
    reference: Decimal,
    atr: Decimal,
    frozen_at: datetime,
) -> dict[str, Any]:
    if (
        timeframe not in HORIZONS
        or horizon not in HORIZONS[timeframe]
        or direction not in {*LABELS, "UNCLEAR"}
    ):
        raise ValueError("declared observation target required")
    if (
        any(x.tzinfo is None or x.utcoffset() is None for x in (origin, frozen_at))
        or frozen_at < origin
    ):
        raise ValueError("frozen observation needs ordered aware timestamps")
    return {
        "schema_version": "chart-observation-target-v1",
        "direction": direction,
        "timeframe": timeframe,
        "horizon_bars": horizon,
        "origin": origin.isoformat(),
        "reference_close": str(reference),
        "atr14": str(atr),
        "label_threshold": str(label_threshold(reference, atr, horizon)),
        "frozen_at": frozen_at.isoformat(),
        "forecast_validated": False,
    }


def assess_observation(
    target: dict[str, Any],
    bars: pl.DataFrame,
    as_of: datetime,
    levels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (
        target.get("schema_version") != "chart-observation-target-v1"
        or as_of.tzinfo is None
        or as_of.utcoffset() is None
    ):
        raise ValueError("versioned target and aware observation time required")
    origin, frozen_at = (
        datetime.fromisoformat(target["origin"]),
        datetime.fromisoformat(target["frozen_at"]),
    )
    horizon = target["horizon_bars"]
    checked = freeze_target(
        direction=target["direction"],
        timeframe=target["timeframe"],
        horizon=horizon,
        origin=origin,
        reference=Decimal(target["reference_close"]),
        atr=Decimal(target["atr14"]),
        frozen_at=frozen_at,
    )
    if checked != target:
        raise ValueError("frozen target fields do not match")
    frame = _single_symbol_bars(bars).filter(
        pl.col("timestamp") <= as_of.astimezone(UTC)
    )
    at_origin = frame.filter(pl.col("timestamp") == origin.astimezone(UTC))
    future = frame.filter(pl.col("timestamp") > origin.astimezone(UTC))
    result: dict[str, Any] = {
        "schema_version": "chart-observation-v1",
        "status": "PENDING",
        "horizon_bars": horizon,
        "observed_bars": min(future.height, horizon),
        "kind": "PENDING_CLASSIFICATION",
        "actual": None,
        "correct": None,
        "future_return": None,
        "label_end": None,
        "first_reference_touch": "NONE",
        "execution_qualified": False,
        "forecast_validated": False,
    }
    if at_origin.is_empty():
        result["status"] = "ORIGIN_MISSING"
        return result
    if Decimal(str(at_origin["close"][0])) != Decimal(target["reference_close"]):
        result["status"] = "ORIGIN_REVISED"
        return result
    if future.is_empty():
        return result
    zone = ZoneInfo("Asia/Shanghai")
    first_bar = future["timestamp"][0].astimezone(zone)
    if target["timeframe"] == "60m":
        opened = first_bar - timedelta(hours=1)
    else:
        opened = datetime.combine(first_bar.date(), time(9, 30), zone)
        if target["timeframe"] == "1w":
            opened -= timedelta(days=first_bar.weekday())
    result["kind"] = "FORWARD_FROZEN" if frozen_at <= opened else "RETROSPECTIVE"
    if future.height < horizon:
        return result
    observed = future.head(horizon)
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        value = (
            Decimal(str(observed["close"][-1])) / Decimal(target["reference_close"]) - 1
        )
        actual = label_return(value, Decimal(target["label_threshold"]))
    result.update(
        status="MATURED",
        actual=actual,
        future_return=str(value),
        correct=actual == target["direction"]
        if target["direction"] in LABELS
        else None,
        label_end=observed["timestamp"][-1].isoformat(),
    )
    if levels:
        stop, take = (
            Decimal(levels["stop_reference"]),
            Decimal(levels["take_profit_reference"]),
        )
        for high, low in observed.select("high", "low").iter_rows():
            stop_touch, take_touch = (
                Decimal(str(low)) <= stop,
                Decimal(str(high)) >= take,
            )
            if stop_touch or take_touch:
                result["first_reference_touch"] = (
                    "BOTH_AMBIGUOUS_STOP_FIRST"
                    if stop_touch and take_touch
                    else "STOP_REFERENCE"
                    if stop_touch
                    else "TAKE_PROFIT_REFERENCE"
                )
                break
    return result
