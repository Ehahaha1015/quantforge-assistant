"""Allowlisted, pure Core tools for explicitly synthetic offline tasks."""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator
from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.data import logical_content_hash
from quantforge.strategies.models import (
    config_from_strategy,
    generate_strategy_signals,
    strategy_from_parameters,
)

from .storage import PlatformError, digest

DATASET = "synthetic-regimes-v1"
FAMILIES = ("moving_average_crossover", "time_series_momentum")


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["OFFLINE_DEMO"]
    goal: str = Field(min_length=10, max_length=500)
    dataset: Literal["synthetic-regimes-v1"]
    max_experiments: Literal[2, 4, 6] = 6
    analysis: Literal["cost_sensitivity", "strategy_comparison"] = "cost_sensitivity"

    @field_validator("max_experiments", mode="before")
    @classmethod
    def exact_budget(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("integer experiment budget required")
        return value

    @field_validator("goal")
    @classmethod
    def meaningful_goal(cls, value: str) -> str:
        if len(value.strip()) < 10:
            raise ValueError("nonblank research goal required")
        return value.strip()


class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dataset: Literal["synthetic-regimes-v1"]
    family: Literal["moving_average_crossover", "time_series_momentum"]
    fee_bps: Literal[0, 10, 30]

    @field_validator("fee_bps", mode="before")
    @classmethod
    def exact_fee(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("integer fee basis points required")
        return value


def bars() -> pl.DataFrame:
    start = datetime(2023, 1, 1, tzinfo=UTC)
    rows = []
    previous = 100.0
    for index in range(240):
        drift = 0.0018 if index < 80 else -0.0015 if index < 160 else 0.0008
        opening = previous * (1 + 0.0004 * math.sin(index * 0.47))
        close = opening * (1 + drift + 0.006 * math.sin(index * 0.31))
        rows.append(
            {
                "symbol": "DEMO-ETF",
                "timestamp": start + timedelta(days=index),
                "open": opening,
                "high": max(opening, close) * 1.002,
                "low": min(opening, close) * 0.998,
                "close": close,
                "volume": 100000.0 + 1000 * (index % 17),
            }
        )
        previous = close
    return pl.DataFrame(rows)


def plan(request: TaskRequest) -> list[dict[str, Any]]:
    fees = [10] if request.analysis == "strategy_comparison" else [0, 10, 30]
    fees = fees[: request.max_experiments // 2]
    return [
        {"tool": "inspect_dataset", "input": {"dataset": DATASET}},
        *[
            {
                "tool": "run_experiment",
                "input": {
                    "dataset": DATASET,
                    "family": family,
                    "fee_bps": fee,
                },
            }
            for fee in fees
            for family in FAMILIES
        ],
        {"tool": "compare_results", "input": {}},
    ]


def _inspect(payload: dict[str, Any]) -> dict[str, Any]:
    if payload != {"dataset": DATASET}:
        raise PlatformError("TASK_DATASET_NOT_ALLOWED")
    frame = bars()
    return {
        "dataset": DATASET,
        "content_hash": logical_content_hash(frame),
        "symbol": "DEMO-ETF",
        "observations": frame.height,
        "start": frame["timestamp"][0].isoformat(),
        "end": frame["timestamp"][-1].isoformat(),
        "source": "Deterministic synthetic fixture; no exchange authentication",
        "quality": {"nulls": 0, "duplicate_keys": 0},
        "prices": [
            {"date": row["timestamp"].isoformat()[:10], "close": row["close"]}
            for row in frame.iter_rows(named=True)
        ],
    }


def _experiment(payload: dict[str, Any]) -> dict[str, Any]:
    spec = ExperimentInput.model_validate(payload)
    parameters: dict[str, Any] = (
        {"short_window": 5, "long_window": 20}
        if spec.family == "moving_average_crossover"
        else {"lookback": 20, "threshold_pct": "0"}
    )
    strategy = strategy_from_parameters(spec.family, parameters)
    frame = bars()
    signals = generate_strategy_signals(frame, config_from_strategy(strategy))
    result = run_vectorized_backtest(
        frame, signals, BacktestConfig(initial_cash=10000.0, fee_bps=spec.fee_bps)
    )
    return {
        "dataset": DATASET,
        "dataset_hash": logical_content_hash(frame),
        "family": spec.family,
        "parameters": parameters,
        "fee_bps": spec.fee_bps,
        "metrics": asdict(result.metrics),
        "timing": "next_bar_open",
        "scope": "SYNTHETIC_DEVELOPMENT_ONLY",
        "validated_alpha": False,
        "equity_curve": [
            {"date": row["timestamp"].isoformat()[:10], "equity": row["equity"]}
            for row in result.periods.iter_rows(named=True)
        ],
    }


def execute(
    tool: str, payload: dict[str, Any], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    if tool == "inspect_dataset":
        result = _inspect(payload)
    elif tool == "run_experiment":
        result = _experiment(payload)
    elif tool == "compare_results":
        if payload:
            raise PlatformError("TASK_COMPARE_SCHEMA_INVALID")
        results = [row for row in evidence if row["tool"] == "run_experiment"]
        if not results:
            raise PlatformError("TASK_EVIDENCE_REQUIRED")
        result = {
            "rows": [
                {
                    "evidence_id": row["evidence_id"],
                    "family": row["result"]["family"],
                    "fee_bps": row["result"]["fee_bps"],
                    **row["result"]["metrics"],
                }
                for row in results
            ],
            "conclusion": "DESCRIPTIVE_ONLY",
            "limitations": [
                "Synthetic regimes are a plumbing fixture, not historical market data.",
                "Fixed parameters; no unseen test, calibrated slippage or broker fees.",
                "Position-switch basis points are not a real commission schedule.",
                "Offline scripted planner; no LLM, retrieval or paid model calls.",
                "Daily UTC labels do not represent an exchange trading calendar.",
            ],
        }
    else:
        raise PlatformError("TASK_TOOL_NOT_ALLOWED")
    value = {"tool": tool, "input": payload, "result": result}
    return {"evidence_id": "demo-evidence-" + digest(value), **value}


def report(state: dict[str, Any]) -> dict[str, Any]:
    comparison = next(
        row for row in state["evidence"] if row["tool"] == "compare_results"
    )
    rows = comparison["result"]["rows"]
    markdown = [
        "# QuantForge offline research report",
        "",
        state["request"]["goal"],
        "",
        "Mode: OFFLINE_DEMO / scripted planner / 0 paid model calls.",
        "All results use synthetic data. NO VALID ALPHA. No trading authority.",
        "",
        "| Strategy | Fee (bps) | Return | Drawdown | Turnover | Evidence |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
        *[
            f"| {row['family']} | {row['fee_bps']} | "
            f"{row['total_return']:.4%} | {row['max_drawdown']:.4%} | "
            f"{row['turnover']:.0f} | {row['evidence_id']} |"
            for row in rows
        ],
        "",
        "## Limitations",
        *["- " + item for item in comparison["result"]["limitations"]],
    ]
    return {
        "title": state["request"]["goal"],
        "conclusion": "DESCRIPTIVE_ONLY",
        "rows": rows,
        "limitations": comparison["result"]["limitations"],
        "evidence_refs": [row["evidence_id"] for row in state["evidence"]],
        "markdown": "\n".join(markdown) + "\n",
    }
