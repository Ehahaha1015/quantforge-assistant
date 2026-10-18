"""Predeclared neighborhood diagnostics with causal selection and block intervals."""

from __future__ import annotations

import math
import random
from dataclasses import asdict
from decimal import Context, Decimal, localcontext
from typing import Any

import polars as pl

from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.validation.metrics import oos_metrics

from .trend_refinement import _frame

LOOKBACKS = (48, 60, 72)
BANDS = ("0.5", "1", "2")
STARTS = (504, 630, 756)
STRESSES = ((10, 0), (30, 0), (30, 1))


def stability_manifest() -> dict[str, Any]:
    return {
        "schema_version": "momentum-neighborhood-policy-v1",
        "lookbacks": list(LOOKBACKS),
        "buffer_pct": list(BANDS),
        "evaluation_start_bars": list(STARTS),
        "training_bars": [126, 252, 504],
        "selection_metric": "net total return at 30 bps, no extra delay",
        "selection_tie_break": "smallest lookback then smallest numeric band",
        "stress": [list(value) for value in STRESSES],
        "new_training_configurations": 9,
        "block_interval": {"block_bars": 20, "replicates": 500, "seed": 20261014},
        "qualification": "EXPOSED_DEVELOPMENT_NOT_NEW_DATA_CONFIRMATION",
        "pbo_dsr": "NOT_APPLIED: overlapping windows, one market, adaptive selection",
        "promotion": "NONE",
    }


def neighborhood_signals(bars: pl.DataFrame, lookback: int, band: str) -> pl.DataFrame:
    if lookback not in LOOKBACKS or band not in BANDS:
        raise ValueError("frozen neighborhood required")
    frame = _frame(bars)
    closes = [Decimal(str(value)) for value in frame["close"]]
    held = False
    values = []
    with localcontext(Context(prec=50)):
        for index, close in enumerate(closes):
            if index >= lookback:
                gap = (close / closes[index - lookback] - 1) * 100
                if held and gap <= -Decimal(band):
                    held = False
                elif not held and gap > Decimal(band):
                    held = True
            values.append(float(held))
    return frame.select("symbol", "timestamp").with_columns(
        pl.Series("value", values, dtype=pl.Float64)
    )


def block_log_mean_interval(values: list[float]) -> dict[str, Any]:
    if len(values) < 40 or any(not math.isfinite(value) for value in values):
        raise ValueError("at least forty finite paired log gaps required")
    rng = random.Random(20261014)
    means = []
    for _ in range(500):
        sample: list[float] = []
        while len(sample) < len(values):
            start = rng.randrange(len(values) - 19)
            sample.extend(values[start : start + 20])
        means.append(math.fsum(sample[: len(values)]) / len(values))
    means.sort()
    return {
        "n": len(values),
        "mean_daily_log_gap": math.fsum(values) / len(values),
        "lower_95": means[12],
        "upper_95": means[487],
        "method": "overlapping moving block bootstrap; fixed 20-bar blocks, 500 draws",
        "limitation": "exploratory; no multiplicity correction or independence guarantee",
    }


def evaluate_stability(bars: pl.DataFrame) -> dict[str, Any]:
    frame = _frame(bars)
    if not STARTS[-1] + 40 <= frame.height <= 20000:
        raise ValueError("frozen starts require forty final observations")
    grid: dict[str, dict[tuple[int, int], pl.DataFrame]] = {}
    rows: list[dict[str, Any]] = []
    for lookback in LOOKBACKS:
        for band in BANDS:
            key = f"L{lookback}_B{band}"
            signals = neighborhood_signals(frame, lookback, band)
            grid[key] = {}
            for fee, delay in STRESSES:
                shifted = signals.with_columns(
                    pl.col("value").shift(delay).fill_null(0)
                )
                grid[key][fee, delay] = run_vectorized_backtest(
                    frame, shifted, BacktestConfig(initial_cash=100000, fee_bps=fee)
                ).periods
                for start in STARTS:
                    # Begin each evaluation flat: only the previous close may enter.
                    values = [0.0] * (start - 1) + signals["value"].slice(
                        start - 1
                    ).to_list()
                    fresh = signals.with_columns(pl.Series("value", values))
                    fresh = fresh.with_columns(
                        pl.col("value").shift(delay).fill_null(0)
                    )
                    periods = run_vectorized_backtest(
                        frame, fresh, BacktestConfig(initial_cash=100000, fee_bps=fee)
                    ).periods.slice(start)
                    returns = periods["strategy_return"].to_list()
                    rows.append(
                        {
                            "candidate": key,
                            "lookback": lookback,
                            "buffer_pct": band,
                            "fee_bps": fee,
                            "extra_delay_bars": delay,
                            "start_bar": start,
                            "metrics": asdict(
                                oos_metrics(
                                    returns,
                                    periods["turnover"].to_list(),
                                    backtest_config=BacktestConfig(
                                        initial_cash=100000, fee_bps=fee
                                    ),
                                )
                            ),
                            "exposure": sum(periods["position_after_open"].to_list())
                            / periods.height,
                            "periods": [
                                {
                                    "timestamp": row["timestamp"].isoformat(),
                                    "return": row["strategy_return"],
                                    "turnover": row["turnover"],
                                }
                                for row in periods.iter_rows(named=True)
                            ],
                        }
                    )
    # Intervals pair aligned flat-start paths, never three ETFs as independent draws.
    by_scenario = {
        (
            row["candidate"],
            row["start_bar"],
            row["fee_bps"],
            row["extra_delay_bars"],
        ): row
        for row in rows
    }
    for row in rows:
        baseline = by_scenario[
            "L60_B1", row["start_bar"], row["fee_bps"], row["extra_delay_bars"]
        ]
        row["paired_interval"] = block_log_mean_interval(
            [
                math.log1p(value["return"]) - math.log1p(base["return"])
                for value, base in zip(row["periods"], baseline["periods"], strict=True)
            ]
        )
    selections = []
    frequencies = dict.fromkeys(grid, 0)
    for start in STARTS:
        for training in (126, 252, 504):
            ranking = []
            for key, scenarios in grid.items():
                # Train only on returns strictly before the evaluation session.
                past = scenarios[30, 0].slice(start - training, training)
                score = math.fsum(
                    math.log1p(value) for value in past["strategy_return"]
                )
                ranking.append((key, score))
            ranking.sort(key=lambda value: -value[1])
            selected = ranking[0][0]
            frequencies[selected] += 1
            selections.append(
                {
                    "start_bar": start,
                    "training_bars": training,
                    "training_last_timestamp": frame["timestamp"][
                        start - 1
                    ].isoformat(),
                    "evaluation_first_timestamp": frame["timestamp"][start].isoformat(),
                    "selected": selected,
                    "ranking": [
                        {"candidate": key, "log_wealth": score}
                        for key, score in ranking
                    ],
                    "evaluation_return": by_scenario[selected, start, 30, 0]["metrics"][
                        "total_return"
                    ],
                }
            )
    return {
        "schema_version": "momentum-neighborhood-result-v1",
        "symbol": str(frame["symbol"][0]),
        "rows": rows,
        "selections": selections,
        "selection_frequencies": frequencies,
        "failures": [
            {
                "candidate": row["candidate"],
                "start_bar": row["start_bar"],
                "fee_bps": row["fee_bps"],
                "extra_delay_bars": row["extra_delay_bars"],
                "reason": "NONPOSITIVE_COST_NET_RETURN",
            }
            for row in rows
            if row["metrics"]["total_return"] <= 0
        ],
        "validated_alpha": False,
        "live_authority": "NONE",
    }
