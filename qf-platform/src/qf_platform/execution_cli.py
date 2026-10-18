"""Source-bound quantity execution stress; all frictions remain declared paper rules."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
from quantforge.data import logical_content_hash
from quantforge.product.context import code_context, source_identity
from quantforge.product.dividend_accounting import CashDividend
from quantforge.product.execution_replay import ExecutionPolicy, replay_quantities
from quantforge.product.pilot.fees import CnFeeProfile
from quantforge.product.storage import local_root, write_exclusive
from quantforge.risk import RiskLimits
from quantforge.strategies.models import generate_strategy_signals
from quantforge.strategies.time_series_momentum import TimeSeriesMomentumConfig

from .dataset_use import register_research_use
from .storage import digest


def load_cash_events(plan: dict[str, Any]) -> tuple[CashDividend, ...]:
    events = []
    for item in plan["events"]:
        values: dict[str, Any] = {
            key: date.fromisoformat(value)
            if key
            in {
                "published_date",
                "record_date",
                "ex_date",
                "pay_date",
            }
            else Decimal(value)
            if key == "amount_per_share"
            else value
            for key, value in item.items()
            if key != "publication_precision"
        }
        events.append(CashDividend(**values))
    return tuple(events)


def load_execution_inputs(
    inventory_path: Path, dividend_plan: Path
) -> tuple[pl.DataFrame, dict[str, Any], dict[str, Any], tuple[CashDividend, ...]]:
    if inventory_path.is_symlink() or dividend_plan.is_symlink():
        raise ValueError("regular source manifests required")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    checksum = json.loads(
        inventory_path.with_name("inventory-digest.json").read_text(encoding="utf-8")
    )
    if digest(inventory) != checksum["sha256"]:
        raise ValueError("inventory checksum mismatch")
    frame = pl.read_parquet(inventory_path.parent / "510300-bars.parquet")
    if logical_content_hash(frame) != inventory["assets"]["510300"]["content_hash"]:
        raise ValueError("source bars and inventory disagree")
    dividend = json.loads(dividend_plan.read_text(encoding="utf-8"))
    events = load_cash_events(dividend)
    return frame, inventory, dividend, events


def run_execution(
    inventory_path: Path, dividend_plan: Path, output: Path
) -> dict[str, Any]:
    frame, inventory, dividend, events = load_execution_inputs(
        inventory_path, dividend_plan
    )
    output.mkdir(parents=True, exist_ok=True)
    base = local_root(output)
    root = base / ("run-" + uuid.uuid4().hex[:12])
    root.mkdir()
    configurations = (
        ("BASE_DECLARED", Decimal(3), Decimal(5), Decimal("0.01"), 0),
        ("HIGH_COST_DECLARED", Decimal(10), Decimal(20), Decimal("0.01"), 0),
        ("THIN_CAPACITY_DECLARED", Decimal(3), Decimal(5), Decimal("0.000001"), 0),
        ("DELAYED_DECLARED", Decimal(3), Decimal(5), Decimal("0.01"), 1),
    )
    policies = {}
    for name, commission, slippage, participation, delay in configurations:
        policies[name] = ExecutionPolicy(
            Decimal(100000),
            100,
            "LOTS",
            participation,
            slippage,
            CnFeeProfile(
                name=name,
                commission_bps=commission,
                min_commission="5",
                stamp_duty_bps="0",
                transfer_fee_bps="0",
            ),
            RiskLimits(
                max_order_notional=Decimal(30000),
                max_position_notional=Decimal(200000),
                max_gross_exposure=Decimal(200000),
            ),
            extra_delay_bars=delay,
        )
    plan = {
        "schema_version": "declared-quantity-stress-plan-v1",
        "source_inventory_hash": digest(inventory),
        "cash_event_plan_hash": digest(dividend),
        "strategy": {"family": "time_series_momentum", "lookback": 20},
        "history_bars": 504,
        "policies": {key: value.to_json() for key, value in policies.items()},
        "core_context": code_context(),
        "core_source_hash": source_identity(),
        "live_authority": "NONE",
    }
    write_exclusive(root / "plan.json", plan)
    register_research_use(
        frame,
        program="execution-" + digest(plan)[:16],
        reference_hash=digest(plan),
    )
    signals = generate_strategy_signals(frame, TimeSeriesMomentumConfig(20))
    values = [0.0] * 504 + signals["value"].slice(504).to_list()
    signals = signals.with_columns(pl.Series("value", values, dtype=pl.Float64))
    scenarios = {
        name: replay_quantities(frame, signals, policy, dividends=events)
        for name, policy in policies.items()
    }
    result = {
        "schema_version": "declared-quantity-stress-v1",
        "plan_id": digest(plan),
        "mode": "PAPER_RESEARCH",
        "scenarios": scenarios,
        "live_eligible": False,
        "validated_alpha": False,
    }
    write_exclusive(root / "result.json", result)
    write_exclusive(root / "result-digest.json", {"sha256": digest(result)})
    lines = [
        "# EXEC-LT-001: quantity, fee and past-capacity stress",
        "",
        "No observed brokerage fees or auction depth; declared assumptions only.",
        "| Scenario | Equity | Return | Drawdown | Fees | Fills | Partial | Refused |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| {name} | {value['final_equity']} | {value['net_return']} | "
            f"{value['max_drawdown']} | {value['fee_total']} | {value['fill_count']} "
            f"| {value['partial_count']} | {value['rejected_count']} |"
            for name, value in scenarios.items()
        ],
        "",
        "Targets use previous closes; capacity uses previous-session volume.",
        "Portfolio cash/fills/fees and paid dividends conserve every step.",
        "Partial fills are topped up only by new remaining-target intents.",
        "Book records actual simulated close quantity for dividend entitlement.",
        "Daily timestamps do not certify T+1 intraday or official halt/limit rules.",
    ]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "artifact_root": str(root),
        "scenarios": {
            name: {
                key: value[key]
                for key in (
                    "final_equity",
                    "fee_total",
                    "fill_count",
                    "partial_count",
                    "rejected_count",
                )
            }
            for name, value in scenarios.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--dividend-plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run_execution(args.inventory, args.dividend_plan, args.out), indent=2
        )
    )


if __name__ == "__main__":
    main()
