"""Frozen multi-ETF buffered-trend study; independent available sessions retained."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from quantforge.product.context import code_context, source_identity
from quantforge.product.pilot.data import import_bars_csv, read_snapshot
from quantforge.product.storage import local_root, write_exclusive, writer_lock
from quantforge.product.trend_refinement import (
    evaluate_refinements,
    refinement_manifest,
)

from .dataset_use import register_research_use
from .storage import canonical, digest


def _rows(study: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for symbol, asset in study["assets"].items():
        for scenario in asset["scenarios"]:
            for row in scenario["strategies"]:
                result.append(
                    {
                        "symbol": symbol,
                        "fee_bps": scenario["fee_bps"],
                        "extra_delay_bars": scenario["extra_delay_bars"],
                        "strategy": row["strategy"],
                        **row["metrics"],
                        "exposure_fraction": row["exposure_fraction"],
                        "flat_up_day_fraction": row["flat_up_day_fraction"],
                    }
                )
    return result


def render_suite(study: dict[str, Any]) -> str:
    lines = [
        "# Fixed buffered-trend ablation",
        "",
        "HISTORICAL_EXPLORATORY. Raw price returns; NO VALID ALPHA.",
        f"Common evaluation dates: {study['evaluation_start'][:10]} to "
        f"{study['evaluation_end'][:10]}.",
        "Ten variants per asset; fees 0/10/30 bps and 30 bps + one-bar delay.",
        "All variants retained; no strategy promoted from this study.",
        "",
        "| ETF | bps | Delay | Variant | Price return | Drawdown | "
        "Turnover | Exposure | Flat on up days |",
        "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in _rows(study):
        missing = row["flat_up_day_fraction"]
        flat = "N/A" if missing is None else f"{missing:.2%}"
        lines.append(
            f"| {row['symbol']} | {row['fee_bps']} | {row['extra_delay_bars']} | "
            f"{row['strategy']} | {row['total_return']:.2%} | "
            f"{row['max_drawdown']:.2%} | {row['turnover']:.0f} | "
            f"{row['exposure_fraction']:.2%} | {flat} |"
        )
    lines.extend(
        [
            "",
            "## Data",
            "",
            study["source"]["declaration"],
            study["source"]["adjustment"],
            "Prior exposure: " + study["source"]["prior_exposure"],
            "Session discrepancies relative to the supplied union: "
            + json.dumps(study["session_diagnostics"], sort_keys=True),
            "",
            "Missing dates remain gaps, not zero returns or certified holidays.",
            "Shared evaluation dates; each asset keeps its available sessions.",
            "",
            "## Rules",
            "",
            "Slow SMA: 20/60; momentum: 60 bars. Buffers: +/-0.5% and +/-1%.",
            "Enter above the positive band; exit at/below the negative band; "
            "hold inside the band.",
            "Filtered variants require close above its trailing 120-bar mean.",
            "Past/current closes only; execute at the next open.",
            "",
            "## Limits",
            "",
            "Fixed retrospective signal ablations, not independent final validation.",
            "Exposure is mean post-open position; flat-up-days require "
            "no position before/after open.",
            "Raw quotes omit dividend cash flows and corporate-action adjustment.",
            "Fees and delay are research stress, not calibrated execution.",
            "ETF returns can be correlated; no universe or live qualification.",
            "Variant comparison retains selection bias; no post-result tuning.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_suite(
    csvs: dict[str, Path],
    output: Path,
    *,
    source: str,
    adjustment: str,
    prior_exposure: str,
    history_bars: int = 504,
    usage_root: Path | None = None,
) -> dict[str, Any]:
    if not 1 <= len(csvs) <= 5 or any(
        not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key) for key in csvs
    ):
        raise ValueError("one to five explicitly supplied assets required")
    if not source.strip() or not adjustment.strip() or len(prior_exposure.strip()) < 10:
        raise ValueError("data and exposure declarations required")
    if type(history_bars) is not int or history_bars < 120:
        raise ValueError("at least 120 historical bars required")
    output.mkdir(parents=True, exist_ok=True)
    output = local_root(output)
    path = output / "plan.json"
    if path.is_symlink():
        raise ValueError("redirected plan")
    stamp = (
        datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["as_of"])
        if path.exists()
        else datetime.now(UTC)
    )
    frames: dict[str, pl.DataFrame] = {}
    sources = {}
    dates = {}
    for symbol, csv_path in sorted(csvs.items()):
        if csv_path.is_symlink() or not csv_path.is_file():
            raise ValueError("regular CSV files required")
        root = output / ("asset-" + symbol)
        root.mkdir(exist_ok=True)
        local_root(root)
        snapshot = import_bars_csv(
            root, csv_path, symbol, as_of=stamp, declared_adjustment=adjustment
        )
        frame, verified = read_snapshot(root, snapshot["snapshot_id"])
        if frame.height < history_bars + 20:
            raise ValueError("each asset must leave twenty evaluation observations")
        frames[symbol] = frame
        dates[symbol] = frame["timestamp"].cast(pl.Int64).to_list()
        sources[symbol] = {
            "content_hash": verified["content_hash"],
            "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            "rows": frame.height,
            "snapshot_id": snapshot["snapshot_id"],
        }
    common = set.intersection(*(set(value) for value in dates.values()))
    if not common:
        raise ValueError("assets have no common dates")
    threshold = max(values[history_bars] for values in dates.values())
    shared = sorted(value for value in common if value >= threshold)
    if len(shared) < 20:
        raise ValueError("common evaluation range is too short")
    first, last = shared[0], shared[-1]
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    union = set.union(*(set(value) for value in dates.values()))
    diagnostics = {
        symbol: [
            (epoch + timedelta(microseconds=value)).isoformat()[:10]
            for value in sorted(union - set(asset_dates))
        ]
        for symbol, asset_dates in dates.items()
    }
    plan = {
        "schema_version": "fixed-trend-suite-plan-v1",
        "as_of": stamp.isoformat(),
        "sources": sources,
        "minimum_history_bars": history_bars,
        "variants": refinement_manifest(),
        "scenarios": [[0, 0], [10, 0], [30, 0], [30, 1]],
        "evaluation_first_us": first,
        "evaluation_last_us": last,
        "source": {
            "declaration": source,
            "adjustment": adjustment,
            "prior_exposure": prior_exposure,
        },
        "core_context": code_context(),
        "core_source_hash": source_identity(),
        "host_source_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    with writer_lock(output):
        if path.exists():
            if path.read_text(encoding="utf-8") != canonical(plan):
                raise ValueError("frozen suite plan changed")
        else:
            write_exclusive(path, plan)
        for frame in frames.values():
            register_research_use(
                frame,
                program="refinement-" + digest(plan)[:16],
                reference_hash=digest(plan),
                registry_root=usage_root,
            )
        result_path = output / "result.json"
        if result_path.exists():
            if result_path.is_symlink():
                raise ValueError("redirected suite result")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            checksum = json.loads(
                (output / "result-digest.json").read_text(encoding="utf-8")
            )
            if result["plan_id"] != digest(plan) or checksum["sha256"] != digest(
                result
            ):
                raise ValueError("suite result integrity mismatch")
            return dict(result)
        assets = {}
        for symbol, frame in frames.items():
            frame = frame.filter(pl.col("timestamp").cast(pl.Int64) <= last)
            local_history = sum(value < first for value in dates[symbol])
            assets[symbol] = evaluate_refinements(frame, history_bars=local_history)
        result = {
            "schema_version": "fixed-trend-suite-v1",
            "mode": "HISTORICAL_EXPLORATORY",
            "plan_id": digest(plan),
            "evaluation_start": next(iter(assets.values()))["evaluation_start"],
            "evaluation_end": next(iter(assets.values()))["evaluation_end"],
            "source": plan["source"],
            "session_diagnostics": diagnostics,
            "assets": assets,
            "variant_evaluations": len(assets) * 10,
            "scenario_evaluations": len(assets) * 40,
            "validated_alpha": False,
            "live_eligible": False,
        }
        write_exclusive(result_path, result)
        write_exclusive(output / "result-digest.json", {"sha256": digest(result)})
        (output / "report.md").write_text(render_suite(result), encoding="utf-8")
        rows = _rows(result)
        with (output / "metrics.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", action="append", required=True, help="SYMBOL=PATH")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--adjustment", required=True)
    parser.add_argument("--prior-exposure", required=True)
    args = parser.parse_args()
    values = {}
    for item in args.csv:
        symbol, path = item.split("=", 1)
        if symbol in values:
            parser.error("duplicate symbol")
        values[symbol] = Path(path)
    result = run_suite(
        values,
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
                    "evaluation_start",
                    "evaluation_end",
                    "session_diagnostics",
                    "variant_evaluations",
                    "scenario_evaluations",
                    "validated_alpha",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
