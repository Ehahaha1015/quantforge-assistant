"""Frozen known-share correction; original research and paper facts retained."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
from quantforge.data import logical_content_hash
from quantforge.product.context import code_context, source_identity
from quantforge.product.execution_replay import (
    WeightRisk,
    replay_adjusted_weights,
    replay_weights,
)
from quantforge.product.fixed_portfolio import (
    replay_fixed_portfolio,
    replay_fixed_with_share_events,
)
from quantforge.product.share_adjustments import ShareAdjustment, buy_hold_known_actions
from quantforge.product.storage import write_exclusive

from .data_quality_cli import _frame
from .dataset_use import WORKSPACE, register_research_use
from .long_term_study_cli import declared_policy
from .storage import digest

CSV_SHA = "e2ebf836cf52a3e4c50d557157355f6773b3986522cead850864d9de66a0b7ff"
NOTICE_SHA = "af9a35f59f6029bc19e0fc239ec13911d94c0b352d7acbef4e2470e1381c6d57"
NOTICE_URL = "https://www.nffund.com/DownLoader.java?filePath=%252Fmain%252Ffiles%252F2022%252F08%252F22%252F513929807319.pdf&utf8=1"


def declared_event() -> ShareAdjustment:
    return ShareAdjustment(
        "510500-split-20220826",
        "510500",
        date(2022, 8, 23),
        datetime(2022, 8, 24, tzinfo=ZoneInfo("Asia/Shanghai")),
        date(2022, 8, 26),
        date(2022, 8, 29),
        Decimal("1.14539"),
        "CEILING_WHOLE_SHARES",
        NOTICE_URL,
        NOTICE_SHA,
        True,
    )


def run_correction(csv_path: Path, notice_path: Path, output: Path) -> dict[str, Any]:
    if csv_path.is_symlink() or notice_path.is_symlink():
        raise ValueError("regular source files required")
    csv_raw, notice_raw = csv_path.read_bytes(), notice_path.read_bytes()
    if (
        hashlib.sha256(csv_raw).hexdigest() != CSV_SHA
        or hashlib.sha256(notice_raw).hexdigest() != NOTICE_SHA
    ):
        raise ValueError("original exposed CSV or reviewed manager notice differs")
    rows = list(csv.DictReader(csv_raw.decode().splitlines()))
    frame = _frame(
        "510500",
        [
            [r[k] for k in ("date", "open", "close", "high", "low", "volume")]
            for r in rows
        ],
    )
    event = declared_event()
    policy = declared_policy()
    start = frame["timestamp"][504].date()
    plan = {
        "schema_version": "known-share-correction-plan-v1",
        "run_id": "share-actions-" + uuid.uuid4().hex[:12],
        "source_sha256": CSV_SHA,
        "content_hash": logical_content_hash(frame),
        "notice_sha256": NOTICE_SHA,
        "event": event.to_json(),
        "event_review": "PREVIOUS_HUMAN_VISUAL_REVIEW_AND_EXACT_ORIGINAL_PDF_BYTES",
        "policy": policy.to_json(),
        "core_source_hash": source_identity(),
        "core_context": code_context(),
        "host_source_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "start_session": start.isoformat(),
        "fixed_weight": "0.5",
        "history_bars": 504,
        "scenarios": [
            "1000_SHARE_BUY_HOLD_KNOWN_UNIT_EVENT",
            "V2_V3_FIXED_HALF_WEIGHT",
            "V1_V2_SHARED_ACCOUNT_HALF_WEIGHT",
        ],
        "cash_events": [],
        "cash_coverage": "UNRESOLVED_510500_DISTRIBUTIONS_NOT_MODELED",
        "prior_exposure": "FULL_WINDOW_ALREADY_EXPOSED_DEVELOPMENT",
        "parameter_search": False,
        "final_confirmation": False,
        "live_authority": "NONE",
    }
    root = output / plan["run_id"]
    root.mkdir(parents=True, exist_ok=False)
    write_exclusive(root / "plan.json", plan)
    (root / "510500-raw.csv").write_bytes(csv_raw)
    (root / "reviewed-split.pdf").write_bytes(notice_raw)
    sources = {}
    core = WORKSPACE / "quantforge"
    for source in sorted(
        [
            *(core / "src").rglob("*.py"),
            core / "pyproject.toml",
            core / "uv.lock",
            Path(__file__),
        ]
    ):
        relative = source.relative_to(WORKSPACE)
        target = root / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = source.read_bytes()
        target.write_bytes(raw)
        sources[relative.as_posix()] = hashlib.sha256(raw).hexdigest()
    write_exclusive(
        root / "source-manifest.json",
        {"files": sources, "scope": "FROZEN_CORE_AND_HOST_ENTRY_NOT_WHOLE_PLATFORM"},
    )
    admission = register_research_use(
        frame, program=plan["run_id"], reference_hash=digest(plan)
    )
    weights = frame.select("symbol", "timestamp").with_columns(
        pl.Series("weight", ["0"] * 504 + ["0.5"] * (frame.height - 504))
    )
    buy_hold = buy_hold_known_actions(frame, shares=Decimal(1000), adjustments=(event,))
    single_old = replay_weights(frame, weights, policy, WeightRisk(Decimal("0.5")))
    single_new = replay_adjusted_weights(
        frame, weights, policy, WeightRisk(Decimal("0.5")), adjustments=(event,)
    )
    shared_old = replay_fixed_portfolio(
        {"510500": frame},
        {"510500": Decimal("0.5")},
        policy,
        start_session=start,
        max_gross_weight=Decimal("0.5"),
    )
    shared_new = replay_fixed_with_share_events(
        {"510500": frame},
        {"510500": Decimal("0.5")},
        policy,
        start_session=start,
        max_gross_weight=Decimal("0.5"),
        adjustments=(event,),
    )
    result = {
        "schema_version": "known-share-correction-v1",
        "plan_id": digest(plan),
        "admission": admission,
        "buy_hold": buy_hold,
        "single_legacy": single_old,
        "single_adjusted": single_new,
        "shared_legacy": shared_old,
        "shared_adjusted": shared_new,
        "total_return_qualified": False,
        "validated_alpha": False,
        "paid_model_calls": 0,
        "live_authority": "NONE",
    }
    if (
        source_identity() != plan["core_source_hash"]
        or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        != plan["host_source_hash"]
    ):
        raise ValueError("executing source changed during research")
    write_exclusive(root / "result.json", result)
    write_exclusive(root / "result-digest.json", {"sha256": digest(result)})
    return {
        "root": str(root),
        "buy_hold_unit_return": buy_hold["original_unit_price_return"],
        "buy_hold_known_share_return": buy_hold["known_action_return"],
        "single": {
            "legacy_equity": single_old["final_equity"],
            "adjusted_equity": single_new["final_equity"],
        },
        "shared": {
            "legacy_equity": shared_old["final_equity"],
            "adjusted_equity": shared_new["final_equity"],
        },
        "total_return_qualified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        default=WORKSPACE / "local-smoke/trend-refinement-20261013/510500-raw.csv",
    )
    parser.add_argument(
        "--notice",
        type=Path,
        default=WORKSPACE
        / "local-smoke/corporate-action-source-review/20261014T170239-47d95b96"
        / "510500-split-20220823.pdf",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=WORKSPACE / "local-smoke/share-action-corrections",
    )
    args = parser.parse_args()
    print(json.dumps(run_correction(args.csv, args.notice, args.output)))


if __name__ == "__main__":
    main()
