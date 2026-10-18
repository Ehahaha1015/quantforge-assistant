"""Frozen historical CSV research; no model calls, authority or hidden data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantforge.product.context import code_context, source_identity
from quantforge.product.pilot.data import import_bars_csv, read_snapshot
from quantforge.product.storage import local_root, write_exclusive, writer_lock
from quantforge.product.strategy_optimization import (
    OptimizationProtocol,
    candidate_manifest,
    evaluate_optimization,
)

from .dataset_use import register_research_use
from .storage import canonical, digest


def render_report(result: dict[str, Any]) -> str:
    selections = dict(Counter(row["selected"] for row in result["decisions"]))
    lines = [
        "# Bounded historical strategy optimization",
        "",
        f"Symbol: {result['symbol']}; bars: {result['observations']}; "
        f"evaluation: {result['evaluation_start'][:10]} to "
        f"{result['evaluation_end'][:10]}.",
        "Exploratory price-return study. NO VALID ALPHA; no live authority.",
        "",
        "## Frozen Protocol",
        "",
        f"Protocol: {json.dumps(result['protocol'], sort_keys=True)}",
        f"Six fixed candidates per window; {result['candidate_evaluations']} "
        "candidate-window evaluations, each at 10/30 bps.",
        f"Selection counts: {selections}",
        f"Parameter switches: {result['parameter_switches']}.",
        "",
        "Past training only: positive cost-stressed median return minus 0.25 "
        "median drawdown, capped by the family median score.",
        "Requires positive results in at least half the inner windows, "
        "at least two position switches and drawdown no worse than 20%.",
        "Otherwise select cash. Selection rule is fixed before calculation.",
        "",
        "## Chronological Evaluation",
        "",
        "| Cost bps | Strategy | Price return | Max drawdown | Sharpe | Turnover |",
        "| ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for scenario in result["scenarios"]:
        for row in scenario["strategies"]:
            metrics = row["metrics"]
            lines.append(
                f"| {scenario['fee_bps']} | {row['strategy']} | "
                f"{metrics['total_return']:.4%} | {metrics['max_drawdown']:.4%} | "
                f"{metrics['sharpe']:.4f} | {metrics['turnover']:.0f} |"
            )
    lines.extend(
        [
            "",
            "## Parameter Selection History",
            "",
            "| Train end | Evaluation start | Evaluation end | Selected |",
            "| --- | --- | --- | --- |",
            *[
                f"| {row['train_end'][:10]} | {row['evaluation_start'][:10]} | "
                f"{row['evaluation_end'][:10]} | {row['selected']} |"
                for row in result["decisions"]
            ],
            "",
            "## Source and Limitations",
            "",
            result["source"]["declaration"],
            "Prior exposure: " + result["source"]["prior_exposure"],
            *["- " + value for value in result["limitations"]],
            "- Raw prices omit distribution cash flows and may contain "
            "corporate-action discontinuities.",
            "- One retrospectively selected ETF is not a representative "
            "or point-in-time universe.",
            "",
            "No final lockbox or prior campaign has been opened or relabelled.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_csv(
    csv_path: Path,
    symbol: str,
    output: Path,
    *,
    source: str,
    adjustment: str,
    prior_exposure: str,
    protocol: OptimizationProtocol | None = None,
    usage_root: Path | None = None,
) -> dict[str, Any]:
    if not source.strip() or not adjustment.strip() or len(prior_exposure.strip()) < 10:
        raise ValueError("source, adjustment and prior-exposure declarations required")
    selected = protocol or OptimizationProtocol()
    output.mkdir(parents=True, exist_ok=True)
    output = local_root(output)
    if csv_path.is_symlink() or not csv_path.is_file():
        raise ValueError("regular historical CSV required")
    retained_plan = output / "plan.json"
    if retained_plan.is_symlink():
        raise ValueError("redirected frozen plan")
    as_of = (
        datetime.fromisoformat(
            json.loads(retained_plan.read_text(encoding="utf-8"))["data_as_of"]
        )
        if retained_plan.exists()
        else datetime.now(UTC)
    )
    snapshot = import_bars_csv(
        output,
        csv_path,
        symbol,
        as_of=as_of,
        declared_adjustment=adjustment,
    )
    frame, verified = read_snapshot(output, snapshot["snapshot_id"])
    plan = {
        "schema_version": "strategy-optimization-plan-v1",
        "symbol": symbol,
        "dataset_hash": verified["content_hash"],
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "data_as_of": as_of.isoformat(),
        "protocol": asdict(selected),
        "candidates": candidate_manifest(),
        "selection_costs_bps": [10, 30],
        "evaluation_costs_bps": [0, 10, 30],
        "risk_rule": "positive family-capped score; turnover>=2; "
        "drawdown>=-0.20; positive folds>=0.5; cash fallback",
        "source": {
            "declaration": source.strip(),
            "adjustment": adjustment.strip(),
            "prior_exposure": prior_exposure.strip(),
        },
        "core_context": code_context(),
        "core_source_hash": source_identity(),
        "host_source_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "validated_alpha": False,
        "live_eligible": False,
    }
    with writer_lock(output):
        target = output / "plan.json"
        if target.exists():
            if target.is_symlink() or target.read_text(encoding="utf-8") != canonical(
                plan
            ):
                raise ValueError(
                    "frozen plan/source changed; use a new explicitly declared run"
                )
        else:
            write_exclusive(target, plan)
        register_research_use(
            frame,
            program="optimization-" + digest(plan)[:16],
            reference_hash=digest(plan),
            purpose="SELECTION",
            registry_root=usage_root,
        )
        existing = output / "result.json"
        if existing.exists():
            if existing.is_symlink():
                raise ValueError("redirected result")
            result = json.loads(existing.read_text(encoding="utf-8"))
            checksum = json.loads(
                (output / "result-digest.json").read_text(encoding="utf-8")
            )
            if result["plan_id"] != digest(plan) or checksum["sha256"] != digest(
                result
            ):
                raise ValueError("result binding/integrity mismatch")
            return dict(result)
        result = evaluate_optimization(frame, selected)
        result.update(
            mode="HISTORICAL_EXPLORATORY",
            plan_id=digest(plan),
            source=plan["source"],
            core_source_hash=plan["core_source_hash"],
        )
        write_exclusive(existing, result)
        write_exclusive(output / "result-digest.json", {"sha256": digest(result)})
        (output / "report.md").write_text(render_report(result), encoding="utf-8")
        with (output / "metrics.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    "fee_bps",
                    "strategy",
                    "total_return",
                    "max_drawdown",
                    "sharpe",
                    "turnover",
                ],
            )
            writer.writeheader()
            for scenario in result["scenarios"]:
                for row in scenario["strategies"]:
                    writer.writerow(
                        {
                            "fee_bps": scenario["fee_bps"],
                            "strategy": row["strategy"],
                            **{
                                key: row["metrics"][key]
                                for key in (
                                    "total_return",
                                    "max_drawdown",
                                    "sharpe",
                                    "turnover",
                                )
                            },
                        }
                    )
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--adjustment", required=True)
    parser.add_argument("--prior-exposure", required=True)
    args = parser.parse_args()
    result = run_csv(
        args.csv,
        args.symbol,
        args.out,
        source=args.source,
        adjustment=args.adjustment,
        prior_exposure=args.prior_exposure,
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "mode",
                    "symbol",
                    "observations",
                    "evaluation_start",
                    "evaluation_end",
                    "candidate_evaluations",
                    "parameter_switches",
                    "validated_alpha",
                    "live_eligible",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
