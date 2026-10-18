"""Freeze and run three bounded historical studies; no provider or live interfaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
from quantforge.data import logical_content_hash
from quantforge.product.context import code_context, repository_root, source_identity
from quantforge.product.execution_replay import ExecutionPolicy, replay_quantities
from quantforge.product.fixed_portfolio import replay_fixed_portfolio
from quantforge.product.pilot.fees import CnFeeProfile
from quantforge.product.single_factor_study import (
    buffer_targets,
    evaluate_buffer_study,
    study_manifest,
)
from quantforge.product.storage import local_root, write_exclusive
from quantforge.product.strategy_stability import evaluate_stability, stability_manifest
from quantforge.risk import RiskLimits

from .dataset_use import register_research_use
from .execution_cli import load_cash_events
from .storage import digest


def declared_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        Decimal(100000),
        100,
        "LOTS",
        Decimal("0.01"),
        Decimal(20),
        CnFeeProfile(
            name="HIGH_DECLARED_ETF",
            commission_bps="10",
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


def run_studies(
    inventory_path: Path,
    cash_path: Path,
    output: Path,
    *,
    usage_root: Path | None = None,
) -> dict[str, Any]:
    local_root(inventory_path.parent)
    local_root(cash_path.parent)
    inventory = json.loads(inventory_path.read_bytes())
    if (
        digest(inventory)
        != json.loads(inventory_path.with_name("inventory-digest.json").read_bytes())[
            "sha256"
        ]
    ):
        raise ValueError("source inventory mismatch")
    frames = {}
    for symbol in ("510300", "510500", "159915"):
        frame = pl.read_parquet(inventory_path.parent / (symbol + "-bars.parquet"))
        if (
            logical_content_hash(frame) != inventory["assets"][symbol]["content_hash"]
            or not inventory["assets"][symbol]["price_research_allowed"]
        ):
            raise ValueError("quality gate or source identity refused")
        frames[symbol] = frame
    cash_plan = json.loads(cash_path.read_bytes())
    all_events = load_cash_events(cash_plan)
    output.mkdir(parents=True, exist_ok=True)
    root = local_root(output) / ("run-" + uuid.uuid4().hex[:12])
    root.mkdir()
    # Identical calendar boundary; keep each asset's actual sessions.
    first = max(frame["timestamp"][504] for frame in frames.values())
    histories = {
        key: sum(value < first for value in frame["timestamp"])
        for key, frame in frames.items()
    }
    policy = declared_policy()
    scenarios = {
        "CASH": ({key: "0" for key in frames}, "0"),
        "EQUAL_FIXED": ({key: "0.3333" for key in frames}, "0"),
        "FIXED_50_30_20": ({"510300": "0.5", "510500": "0.3", "159915": "0.2"}, "0"),
        "FIXED_50_30_20_BAND": (
            {"510300": "0.5", "510500": "0.3", "159915": "0.2"},
            "0.02",
        ),
        **{
            ("SINGLE_" + key): (
                {item: "1" if item == key else "0" for item in frames},
                "0",
            )
            for key in frames
        },
    }
    plan = {
        "schema_version": "long-term-three-study-plan-v1",
        "frozen_at": datetime.now(UTC).isoformat(),
        "inventory_hash": digest(inventory),
        "cash_plan_hash": digest(cash_plan),
        "source_hashes": {
            key: logical_content_hash(frame) for key, frame in frames.items()
        },
        "histories": histories,
        "evaluation_start": first.isoformat(),
        "ablation": study_manifest(),
        "stability": stability_manifest(),
        "portfolio": {
            key: {"weights": value[0], "band": value[1]}
            for key, value in scenarios.items()
        },
        "execution_policy": policy.to_json(),
        "core_source_hash": source_identity(),
        "core_context": code_context(),
        "host_source_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "eligibility": "ALREADY_EXPOSED_EXPLORATION_ONLY",
        "promotion": "NONE",
        "open_quality": (
            "SUSPENSION/PARTIAL_SESSION_PRE2022_OUTSIDE_EVALUATION; "
            "NOT_EXCHANGE_CERTIFIED"
        ),
    }
    write_exclusive(root / "plan.json", plan)
    write_exclusive(root / "plan-digest.json", {"sha256": digest(plan)})
    archive = root / "source"
    files = {}
    core_root = repository_root()
    sources = list((core_root / "src/quantforge").rglob("*.py")) + [
        core_root / "uv.lock",
        core_root / "pyproject.toml",
    ]
    for source in sources:
        relative = Path("quantforge") / source.relative_to(core_root)
        target = archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = source.read_bytes()
        target.write_bytes(raw)
        files[relative.as_posix()] = hashlib.sha256(raw).hexdigest()
    target = archive / "long_term_study_cli.py"
    target.write_bytes(Path(__file__).read_bytes())
    files[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    write_exclusive(
        root / "source-manifest.json",
        {"files": files, "scope": "CORE_SOURCE_LOCK_HOST_NOT_GIT_HEAD_OR_ALL_PLATFORM"},
    )
    for key, frame in frames.items():
        register_research_use(
            frame,
            program="long-term-" + digest(plan)[:16],
            reference_hash=digest(plan),
            registry_root=usage_root,
        )
        frame.write_parquet(root / (key + "-bars.parquet"))
    write_exclusive(root / "cash-plan.json", cash_plan)
    ablation = {}
    stability = {}
    for key, frame in frames.items():
        print("STUDY " + key, flush=True)
        ablation[key] = evaluate_buffer_study(frame, history_bars=histories[key])
        relevant = tuple(event for event in all_events if event.symbol == key)
        ablation[key]["declared_quantity_replays"] = {
            band: replay_quantities(
                frame,
                buffer_targets(frame, band, history_bars=histories[key]),
                policy,
                dividends=relevant,
            )
            for band in ("0", "0.5", "1", "2")
        }
        stability[key] = evaluate_stability(frame)
    relevant = tuple(event for event in all_events if event.record_date >= first.date())
    portfolio = {
        name: replay_fixed_portfolio(
            frames,
            {key: Decimal(value) for key, value in weights.items()},
            policy,
            start_session=first.date(),
            band=Decimal(band),
            dividends=relevant,
        )
        for name, (weights, band) in scenarios.items()
    }
    result = {
        "schema_version": "long-term-three-study-result-v1",
        "plan_id": digest(plan),
        "ablation": ablation,
        "stability": stability,
        "portfolio": portfolio,
        "validated_alpha": False,
        "live_authority": "NONE",
        "new_data_confirmation": "PENDING_PROSPECTIVE_DATA",
    }
    if (
        source_identity() != plan["core_source_hash"]
        or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        != plan["host_source_hash"]
    ):
        raise ValueError("study executing source changed")
    write_exclusive(root / "result.json", result)
    write_exclusive(root / "result-digest.json", {"sha256": digest(result)})
    lines = [
        "# Long-term frozen historical studies",
        "",
        "Already exposed development data. No promotion or alpha certification.",
        "",
        "## Buffer-only attribution (net log gap = gross timing + relative cost drag)",
        "",
        "| Asset | Buffer % | Timing | Cost drag | Net gap | Exposure gap |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, value in ablation.items():
        for row in value["attribution"]:
            lines.append(
                f"| {key} | {row['buffer_pct']} | "
                f"{row['zero_fee_timing_log_gap']:.6f} | "
                f"{row['relative_cost_drag_log_gap']:.6f} | "
                f"{row['net_log_gap']:.6f} | {row['exposure_gap']:.4f} |"
            )
    lines += [
        "",
        "## Stability",
        "",
        "Full grid, rankings, block intervals and failures are in result.json.",
        "| Asset | Evaluations | Nonpositive returns | Selection frequency |",
        "| --- | ---: | ---: | --- |",
    ]
    for key, value in stability.items():
        lines.append(
            f"| {key} | {len(value['rows'])} | {len(value['failures'])} | "
            f"{json.dumps(value['selection_frequencies'])} |"
        )
    lines += [
        "",
        "## Shared-cash fixed portfolios",
        "",
        "| Baseline | Return | Drawdown | Fees | Paused | Cap breach days |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, value in portfolio.items():
        lines.append(
            f"| {key} | {value['net_return']} | {value['max_drawdown']} | "
            f"{value['fee_total']} | {value['paused_sessions']} | "
            f"{value['gross_cap_breach_days']} |"
        )
    lines += [
        "",
        "Raw prices plus known 510300 cash events; "
        "other assets have unrebuilt distributions.",
        "Different cash-flow coverage prevents a complete total-return ranking.",
        "Intervals overlap; same-market ETFs and block intervals are exploratory, "
        "without multiplicity correction.",
        "PBO/DSR not applied; statistics cannot replace new data or forward time.",
        "Costs, capacity and fills are declared assumptions, not broker evidence.",
        "All positive and negative paths retained; no winner selected for live use.",
    ]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "artifact_root": str(root),
        "plan_id": digest(plan),
        "stability_evaluations": sum(
            len(value["rows"]) for value in stability.values()
        ),
        "portfolio_scenarios": len(portfolio),
        "paid_model_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--cash-plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_studies(args.inventory, args.cash_plan, args.out), indent=2))


if __name__ == "__main__":
    main()
