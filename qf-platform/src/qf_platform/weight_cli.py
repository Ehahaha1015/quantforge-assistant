"""Frozen single-asset fractional-position comparisons; no trading authority."""

from __future__ import annotations

import argparse
import json
import uuid
from decimal import Context, Decimal, localcontext
from pathlib import Path
from typing import Any

import polars as pl
from quantforge.product.context import code_context, source_identity
from quantforge.product.dividend_accounting import CashDividend
from quantforge.product.execution_replay import (
    ExecutionPolicy,
    WeightRisk,
    replay_weights,
)
from quantforge.product.pilot.fees import CnFeeProfile
from quantforge.product.storage import local_root, write_exclusive
from quantforge.risk import RiskLimits
from quantforge.strategies.models import generate_strategy_signals
from quantforge.strategies.time_series_momentum import TimeSeriesMomentumConfig

from .dataset_use import register_research_use
from .execution_cli import load_execution_inputs
from .storage import digest

WEIGHT_CONFIGURATIONS = (
    ("CASH", "FIXED", "0", "1", "0"),
    ("FIXED_100", "FIXED", "1", "1", "0.02"),
    ("FIXED_50", "FIXED", "0.5", "1", "0.02"),
    ("MOMENTUM_100", "MOMENTUM_20", "1", "1", "0.02"),
    ("MOMENTUM_50_CAPPED", "MOMENTUM_20", "0.5", "0.5", "0"),
    ("MOMENTUM_50_BAND", "MOMENTUM_20", "0.5", "1", "0.02"),
)


def evaluate_weights(
    frame: pl.DataFrame,
    policy: ExecutionPolicy,
    *,
    history_bars: int,
    dividends: tuple[CashDividend, ...] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    if type(history_bars) is not int or not 20 <= history_bars < frame.height:
        raise ValueError("bounded causal warmup required")
    configurations = {item[0]: item[1:] for item in WEIGHT_CONFIGURATIONS}
    momentum = generate_strategy_signals(frame, TimeSeriesMomentumConfig(20))[
        "value"
    ].to_list()
    results = {}
    for name, (family, scale, cap, band) in configurations.items():
        values = [
            scale
            if index >= history_bars and (family == "FIXED" or momentum[index] == 1.0)
            else "0"
            for index in range(frame.height)
        ]
        weights = frame.select("symbol", "timestamp").with_columns(
            pl.Series("weight", values, dtype=pl.String)
        )
        value = replay_weights(
            frame,
            weights,
            policy,
            WeightRisk(Decimal(cap), Decimal(band)),
            dividends=dividends,
        )
        periods = value["periods"][history_bars:]
        with localcontext(Context(prec=50)):
            value["exposure_summary"] = {
                "observations": len(periods),
                "average_market_weight": str(
                    sum(
                        (Decimal(row["weight_at_close"]) for row in periods), Decimal(0)
                    )
                    / len(periods)
                ),
                "average_cash_fraction": str(
                    sum(
                        (Decimal(row["cash_fraction_at_close"]) for row in periods),
                        Decimal(0),
                    )
                    / len(periods)
                ),
                "flat_sessions": sum(Decimal(row["quantity"]) == 0 for row in periods),
                "traded_notional": str(
                    sum(
                        (
                            Decimal(row["filled"]) * Decimal(row["price"])
                            for row in value["orders"]
                        ),
                        Decimal(0),
                    )
                ),
            }
        results[name] = value
    with localcontext(Context(prec=50)):
        for value in results.values():
            value["exposure_summary"]["fixed_100_return_gap"] = str(
                Decimal(value["net_return"])
                - Decimal(results["FIXED_100"]["net_return"])
            )
    return configurations, results


def run_weights(
    inventory_path: Path,
    dividend_plan: Path,
    output: Path,
    *,
    history_bars: int = 504,
    usage_root: Path | None = None,
) -> dict[str, Any]:
    frame, inventory, dividend, events = load_execution_inputs(
        inventory_path, dividend_plan
    )
    if type(history_bars) is not int or not 20 <= history_bars < frame.height:
        raise ValueError("bounded causal warmup required")
    output.mkdir(parents=True, exist_ok=True)
    root = local_root(output) / ("run-" + uuid.uuid4().hex[:12])
    root.mkdir()
    policy = ExecutionPolicy(
        Decimal(100000),
        100,
        "LOTS",
        Decimal("0.01"),
        Decimal(5),
        CnFeeProfile(
            name="DECLARED_ETF",
            commission_bps="3",
            min_commission="5",
            stamp_duty_bps="0",
            transfer_fee_bps="0",
        ),
        RiskLimits(
            max_order_notional=Decimal(30000),
            max_position_notional=Decimal(200000),
            max_gross_exposure=Decimal(200000),
        ),
    )
    # This grid is fixed before computation; no winner is promoted.
    plan = {
        "schema_version": "declared-weight-comparison-plan-v1",
        "inventory_hash": digest(inventory),
        "cash_event_plan_hash": digest(dividend),
        "history_bars": history_bars,
        "momentum_lookback": 20,
        "configurations": {item[0]: item[1:] for item in WEIGHT_CONFIGURATIONS},
        "policy": policy.to_json(),
        "core_context": code_context(),
        "core_source_hash": source_identity(),
        "qualification": "EXPOSED_EXPLORATORY_DECLARED_EXECUTION",
        "live_authority": "NONE",
    }
    write_exclusive(root / "plan.json", plan)
    register_research_use(
        frame,
        program="weights-" + digest(plan)[:16],
        reference_hash=digest(plan),
        registry_root=usage_root,
    )
    configurations, scenarios = evaluate_weights(
        frame, policy, history_bars=history_bars, dividends=events
    )
    result = {
        "schema_version": "declared-weight-comparison-v1",
        "plan_id": digest(plan),
        "mode": "PAPER_RESEARCH",
        "configurations": configurations,
        "scenarios": scenarios,
        "live_eligible": False,
        "validated_alpha": False,
    }
    write_exclusive(root / "result.json", result)
    write_exclusive(root / "result-digest.json", {"sha256": digest(result)})
    lines = [
        "# EXEC-LT-002: versioned weights and exposure",
        "",
        "Raw prices plus declared known cash events; "
        "full corporate-action qualification remains false.",
        "Costs, lot, tick and volume capacity are declared, not broker authenticated.",
        "| Scenario | Net Return | Drawdown | Avg Exposure | Avg Cash | Fees "
        "| Fills | Close Breaches | Return Gap vs Fixed 100 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| {name} | {value['net_return']} | {value['max_drawdown']} "
            f"| {value['exposure_summary']['average_market_weight']} "
            f"| {value['exposure_summary']['average_cash_fraction']} "
            f"| {value['fee_total']} | {value['fill_count']} "
            f"| {value['market_weight_breach_days']} "
            f"| {value['exposure_summary']['fixed_100_return_gap']} |"
            for name, value in scenarios.items()
        ],
        "",
        "The return gap is a baseline comparison, not isolated timing/fee attribution.",
        "Buy caps apply after fees at the current open mark; "
        "price appreciation may breach the close cap.",
        "Known breaches require later tradable rebalancing; "
        "a daily cap is not a continuous guarantee.",
        "Zero targets exit independently of the rebalance band; "
        "no ETF is retrospectively selected.",
        "Correlated ETF diversification and multi-asset accounting "
        "are separate STRAT-LT-003 work.",
    ]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "artifact_root": str(root),
        "scenarios": {
            key: {
                field: value[field]
                for field in (
                    "final_equity",
                    "net_return",
                    "max_drawdown",
                    "fee_total",
                    "fill_count",
                    "market_weight_breach_days",
                    "exposure_summary",
                )
            }
            for key, value in scenarios.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--dividend-plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(run_weights(args.inventory, args.dividend_plan, args.out), indent=2)
    )


if __name__ == "__main__":
    main()
