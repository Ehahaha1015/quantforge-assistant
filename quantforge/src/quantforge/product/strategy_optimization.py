"""Bounded chronological parameter selection; descriptive research only."""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import polars as pl

from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.data import canonicalize_bars, logical_content_hash, validate_bars
from quantforge.strategies import MovingAverageCrossoverConfig
from quantforge.strategies.models import (
    StrategyConfig,
    generate_strategy_signals,
    required_observations,
)
from quantforge.strategies.time_series_momentum import TimeSeriesMomentumConfig
from quantforge.validation import WalkForwardConfig, run_walk_forward_validation
from quantforge.validation.metrics import oos_metrics


@dataclass(frozen=True)
class OptimizationProtocol:
    training_bars: int = 504
    inner_history_bars: int = 252
    inner_window_bars: int = 63
    evaluation_window_bars: int = 63

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.inner_history_bars < 61:
            raise ValueError("training history must cover every frozen candidate")
        if self.inner_window_bars < 10 or self.evaluation_window_bars < 10:
            raise ValueError("evaluation windows must contain at least ten bars")
        if self.training_bars < self.inner_history_bars + 2 * self.inner_window_bars:
            raise ValueError("at least two inner validation windows required")


def candidates() -> tuple[tuple[str, StrategyConfig], ...]:
    return (
        ("sma-5-20", MovingAverageCrossoverConfig(5, 20)),
        ("sma-10-40", MovingAverageCrossoverConfig(10, 40)),
        ("sma-20-60", MovingAverageCrossoverConfig(20, 60)),
        ("momentum-10", TimeSeriesMomentumConfig(10)),
        ("momentum-20", TimeSeriesMomentumConfig(20)),
        ("momentum-60", TimeSeriesMomentumConfig(60)),
    )


def _times(frame: pl.DataFrame) -> list[str]:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return [
        (epoch + timedelta(microseconds=int(value))).isoformat()
        for value in frame["timestamp"].cast(pl.Int64)
    ]


def _parameters(config: StrategyConfig) -> dict[str, Any]:
    result = asdict(config)
    return {
        key: str(value) if key == "threshold_pct" else value
        for key, value in result.items()
    }


def candidate_manifest() -> list[dict[str, Any]]:
    return [
        {
            "id": name,
            "family": "moving_average_crossover"
            if isinstance(config, MovingAverageCrossoverConfig)
            else "time_series_momentum",
            "parameters": _parameters(config),
        }
        for name, config in candidates()
    ]


def select_parameters(
    training: pl.DataFrame, protocol: OptimizationProtocol
) -> dict[str, Any]:
    """This selector accepts only the physically sliced past training interval."""
    if training.height != protocol.training_bars:
        raise ValueError("exact declared training interval required")
    rows = []
    for name, config in candidates():
        if required_observations(config) > protocol.inner_history_bars:
            raise ValueError("candidate exceeds historical context")
        scenarios = []
        for fee in (10, 30):
            result = run_walk_forward_validation(
                training,
                config,
                BacktestConfig(fee_bps=fee),
                WalkForwardConfig(
                    initial_history_bars=protocol.inner_history_bars,
                    oos_window_bars=protocol.inner_window_bars,
                ),
            )
            folds = [asdict(window.result.metrics) for window in result.windows]
            median_return = statistics.median(row["total_return"] for row in folds)
            median_loss = statistics.median(abs(row["max_drawdown"]) for row in folds)
            scenarios.append(
                {
                    "fee_bps": fee,
                    "windows": folds,
                    "median_return": median_return,
                    "score": median_return - 0.25 * median_loss,
                    "positive_window_fraction": sum(
                        row["total_return"] > 0 for row in folds
                    )
                    / len(folds),
                    "aggregate": asdict(result.aggregate.metrics),
                }
            )
        rows.append(
            {
                "candidate": name,
                "family": "moving_average_crossover"
                if name.startswith("sma")
                else "time_series_momentum",
                "parameters": _parameters(config),
                "scenarios": scenarios,
                "score": min(row["score"] for row in scenarios),
                "eligible": all(
                    row["aggregate"]["turnover"] >= 2
                    and row["aggregate"]["max_drawdown"] >= -0.20
                    and row["positive_window_fraction"] >= 0.5
                    for row in scenarios
                ),
            }
        )
    for row in rows:
        family_median = statistics.median(
            item["score"] for item in rows if item["family"] == row["family"]
        )
        row["stable_score"] = min(row["score"], family_median)
    eligible = sorted(
        (row for row in rows if row["eligible"] and row["stable_score"] > 0),
        key=lambda row: (-row["stable_score"], -row["score"], row["candidate"]),
    )
    winner = eligible[0]["candidate"] if eligible else "cash"
    return {
        "selected": winner,
        "candidate_count": len(rows),
        "candidates": rows,
        "rule": "positive cost-stressed median return minus 0.25 median drawdown; family-median cap; cash fallback",
        "selected_using": "PAST_TRAINING_INNER_VALIDATION_ONLY",
    }


def evaluate_optimization(
    bars: pl.DataFrame, protocol: OptimizationProtocol | None = None
) -> dict[str, Any]:
    selected_protocol = protocol or OptimizationProtocol()
    if not isinstance(selected_protocol, OptimizationProtocol):
        raise TypeError("OptimizationProtocol required")
    frame = canonicalize_bars(bars)
    if frame.is_empty() or frame["symbol"].n_unique() != 1:
        raise ValueError("nonempty single-symbol bars required")
    times = _times(frame)
    validate_bars(frame, as_of=datetime.fromisoformat(times[-1]))
    if frame.height < selected_protocol.training_bars + 20 or frame.height > 20000:
        raise ValueError("bounded data must leave at least twenty evaluation bars")
    history = selected_protocol.training_bars
    values = [0.0] * frame.height
    lookup = dict(candidates())
    decisions: list[dict[str, Any]] = []
    start = history
    while start < frame.height:
        end = min(start + selected_protocol.evaluation_window_bars, frame.height)
        training = frame.slice(start - history, history)
        selection = select_parameters(training, selected_protocol)
        chosen = selection["selected"]
        if chosen != "cash":
            signals = generate_strategy_signals(frame.head(end), lookup[chosen])
            # Prior-close targets, including the decision before the first open.
            values[start - 1 : end - 1] = (
                signals["value"].slice(start - 1, end - start).to_list()
            )
        decisions.append(
            {
                "window": len(decisions),
                "train_start": times[start - history],
                "train_end": times[start - 1],
                "evaluation_start": times[start],
                "evaluation_end": times[end - 1],
                "start_index": start,
                "end_index": end,
                "training_hash": logical_content_hash(training),
                **selection,
            }
        )
        start = end
    target_frames = {
        "optimized": frame.select("symbol", "timestamp").with_columns(
            pl.Series("value", values, dtype=pl.Float64)
        ),
    }
    for name, baseline_config in (
        ("baseline_sma_5_20", MovingAverageCrossoverConfig(5, 20)),
        ("baseline_momentum_20", TimeSeriesMomentumConfig(20)),
    ):
        signals = generate_strategy_signals(frame, baseline_config)
        targets = [0.0] * (history - 1) + signals["value"].slice(history - 1).to_list()
        target_frames[name] = signals.with_columns(
            pl.Series("value", targets, dtype=pl.Float64)
        )
    for name, target in (("buy_and_hold", 1.0), ("cash", 0.0)):
        target_frames[name] = frame.select("symbol", "timestamp").with_columns(
            pl.Series(
                "value",
                [0.0] * (history - 1) + [target] * (frame.height - history + 1),
                dtype=pl.Float64,
            )
        )
    scenarios: list[dict[str, Any]] = []
    for fee in (0, 10, 30):
        fee_config = BacktestConfig(initial_cash=100000.0, fee_bps=fee)
        rows: list[dict[str, Any]] = []
        for name, signals in target_frames.items():
            periods = run_vectorized_backtest(frame, signals, fee_config).periods.slice(
                history
            )
            metrics = oos_metrics(
                periods["strategy_return"].to_list(),
                periods["turnover"].to_list(),
                backtest_config=fee_config,
            )
            rows.append(
                {
                    "strategy": name,
                    "metrics": asdict(metrics),
                    "windows": [
                        {
                            "window": decision["window"],
                            "metrics": asdict(
                                oos_metrics(
                                    periods["strategy_return"]
                                    .slice(
                                        decision["start_index"] - history,
                                        decision["end_index"] - decision["start_index"],
                                    )
                                    .to_list(),
                                    periods["turnover"]
                                    .slice(
                                        decision["start_index"] - history,
                                        decision["end_index"] - decision["start_index"],
                                    )
                                    .to_list(),
                                    backtest_config=fee_config,
                                )
                            ),
                        }
                        for decision in decisions
                    ],
                    "periods": [
                        {
                            "timestamp": times[index + history],
                            "strategy_return": float(row["strategy_return"]),
                            "turnover": float(row["turnover"]),
                            "equity": float(row["equity"]),
                        }
                        for index, row in enumerate(periods.iter_rows(named=True))
                    ],
                }
            )
        scenarios.append({"fee_bps": fee, "strategies": rows})
    for scenario in scenarios:
        for row in scenario["strategies"]:
            if not all(math.isfinite(value) for value in row["metrics"].values()):
                raise ValueError("nonfinite optimization metrics")
    return {
        "schema_version": "bounded-strategy-optimization-v1",
        "symbol": str(frame["symbol"][0]),
        "dataset_hash": logical_content_hash(frame),
        "observations": frame.height,
        "evaluation_bars": frame.height - history,
        "evaluation_start": times[history],
        "evaluation_end": times[-1],
        "protocol": asdict(selected_protocol),
        "frozen_candidates": candidate_manifest(),
        "decisions": decisions,
        "scenarios": scenarios,
        "candidate_evaluations": len(decisions) * len(candidates()),
        "parameter_switches": sum(
            left["selected"] != right["selected"] for left, right in pairwise(decisions)
        ),
        "validated_alpha": False,
        "live_eligible": False,
        "limitations": [
            "Chronological research evaluation is not an untouched final lockbox.",
            "Selection is adaptive; multiple testing remains, no PBO/DSR qualification.",
            "Fixed six-candidate grid and fixed risk rule, not a universal best strategy.",
            "Position-switch costs omit calibrated commission, lots, liquidity and dividends.",
            "Targets change at actual next opens; switching costs retain the prior position.",
            "Prices must have a declared adjustment basis; data authenticity is caller-provided.",
            "Terminal holdings are marked to close without forced liquidation.",
        ],
    }
