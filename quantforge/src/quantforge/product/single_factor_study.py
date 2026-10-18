"""Frozen buffer-only momentum comparison; attribution is diagnostic, not alpha."""

from __future__ import annotations

import math
from dataclasses import asdict
from decimal import Decimal
from typing import Any

import polars as pl

from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.data import canonicalize_bars, logical_content_hash
from quantforge.strategies.models import generate_strategy_signals
from quantforge.strategies.time_series_momentum import TimeSeriesMomentumConfig
from quantforge.validation.metrics import oos_metrics

from .trend_refinement import BufferedTrendConfig, _frame, buffered_signals

BUFFERS = ("0", "0.5", "1", "2")
SCENARIOS = ((0, 0), (30, 0), (30, 1))


def study_manifest() -> dict[str, Any]:
    return {
        "schema_version": "buffer-only-study-policy-v1",
        "question": "Does buffering change gross timing or reduce cost drag?",
        "lookback": 60,
        "buffers_pct": list(BUFFERS),
        "primary_fees_bps": [0, 30],
        "extra_delay_stress": [30, 1],
        "assets": ["510300", "510500", "159915"],
        "primary_configurations": 24,
        "latency_diagnostics": 12,
        "new_buffer_variants": 3,
        "selection": "NONE; no extra fitting or promotion after outputs",
        "attribution": "Net log gap = zero-fee timing gap + relative cost drag",
        "limitations": [
            "All intervals already exposed; not untouched final confirmation.",
            "Zero fees are diagnostics, not executable cost assumptions.",
            "Correlated ETFs and one historical study do not establish alpha.",
        ],
    }


def buffer_targets(
    bars: pl.DataFrame, buffer: str, *, history_bars: int
) -> pl.DataFrame:
    if buffer not in BUFFERS or type(history_bars) is not int or history_bars < 120:
        raise ValueError("frozen buffer and bounded warmup required")
    frame = _frame(bars)
    if frame.height <= history_bars or frame.height > 20000:
        raise ValueError("bounded evaluation data required")
    signals = (
        generate_strategy_signals(frame, TimeSeriesMomentumConfig(60))
        if buffer == "0"
        else buffered_signals(
            frame, BufferedTrendConfig("time_series_momentum", Decimal(buffer))
        )
    )
    return signals.with_columns(
        pl.Series(
            "value",
            [0.0] * (history_bars - 1)
            + signals["value"].slice(history_bars - 1).to_list(),
            dtype=pl.Float64,
        )
    )


def evaluate_buffer_study(
    bars: pl.DataFrame, *, history_bars: int = 504
) -> dict[str, Any]:
    frame = canonicalize_bars(bars)
    if frame.is_empty() or frame["symbol"].n_unique() != 1:
        raise ValueError("single asset required")
    all_targets = {
        buffer: buffer_targets(frame, buffer, history_bars=history_bars)
        for buffer in BUFFERS
    }
    scenarios = []
    outcomes: dict[tuple[str, int, int], dict[str, Any]] = {}
    for fee, delay in SCENARIOS:
        config = BacktestConfig(initial_cash=100000.0, fee_bps=fee)
        variants = []
        for buffer, signals in all_targets.items():
            delayed = signals.with_columns(pl.col("value").shift(delay).fill_null(0.0))
            periods = run_vectorized_backtest(frame, delayed, config).periods.slice(
                history_bars
            )
            metrics = oos_metrics(
                periods["strategy_return"].to_list(),
                periods["turnover"].to_list(),
                backtest_config=config,
            )
            wealth = 1.0 + metrics.total_return
            if not wealth > 0 or not math.isfinite(wealth):
                raise ValueError("positive finite wealth required for log attribution")
            row = {
                "buffer_pct": buffer,
                "metrics": asdict(metrics),
                "log_wealth": math.log(wealth),
                "average_exposure": sum(periods["position_after_open"].to_list())
                / periods.height,
                "switches": int(periods["turnover"].sum()),
                "periods": [
                    {
                        "timestamp": value["timestamp"].isoformat(),
                        "net_return": value["strategy_return"],
                        "turnover": value["turnover"],
                        "before": value["position_before_open"],
                        "after": value["position_after_open"],
                    }
                    for value in periods.iter_rows(named=True)
                ],
            }
            outcomes[(buffer, fee, delay)] = row
            variants.append(row)
        scenarios.append(
            {"fee_bps": fee, "extra_delay_bars": delay, "variants": variants}
        )
    attribution = []
    baseline_gross = outcomes[("0", 0, 0)]["log_wealth"]
    baseline_net = outcomes[("0", 30, 0)]["log_wealth"]
    for buffer in BUFFERS[1:]:
        gross, net = outcomes[(buffer, 0, 0)], outcomes[(buffer, 30, 0)]
        timing = gross["log_wealth"] - baseline_gross
        relative_cost = (net["log_wealth"] - gross["log_wealth"]) - (
            baseline_net - baseline_gross
        )
        net_gap = net["log_wealth"] - baseline_net
        residual = net_gap - timing - relative_cost
        if abs(residual) > 1e-12:
            raise ValueError("log attribution did not reconcile")
        attribution.append(
            {
                "buffer_pct": buffer,
                "zero_fee_timing_log_gap": timing,
                "relative_cost_drag_log_gap": relative_cost,
                "net_log_gap": net_gap,
                "residual": residual,
                "exposure_gap": net["average_exposure"]
                - outcomes[("0", 30, 0)]["average_exposure"],
            }
        )
    return {
        "schema_version": "buffer-only-study-result-v1",
        "symbol": str(frame["symbol"][0]),
        "dataset_hash": logical_content_hash(frame),
        "history_bars": history_bars,
        "evaluation_start": frame["timestamp"][history_bars].isoformat(),
        "evaluation_end": frame["timestamp"][-1].isoformat(),
        "scenarios": scenarios,
        "attribution": attribution,
        "basis": "RAW_PRICE_ONLY_EXPOSED",
        "validated_alpha": False,
        "live_authority": "NONE",
    }
