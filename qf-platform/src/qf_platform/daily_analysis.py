"""Daily-five report integration over retained raw market artifacts."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
from quantforge.data.bars import logical_content_hash
from quantforge.strategies.daily_five import prediction, prepare_inputs, report_plan
from quantforge.strategies.daily_replay import replay

from . import daily_release
from .chart_market_cache import cached_fetch
from .chart_timeframes import parse_period
from .history_cache import cached_history
from .storage import canonical


def enhance(
    metadata: dict[str, Any],
    result: dict[str, Any],
    frame: pl.DataFrame,
    root: Path,
    fetch_daily: Any,
    parse_daily: Any,
    start: date,
    cutoff: datetime,
    initial_levels: dict[str, Any] | None,
) -> None:
    kind = (
        "ETF"
        if result["source"]["symbol"][2:].startswith(("5", "15", "16"))
        else "STOCK"
    )
    policy = daily_release.POLICIES[kind]
    exit_policy = daily_release.EXITS[kind]
    benchmark = None
    benchmark_source = None
    reference_error = None
    if result["source"]["symbol"] == "sh510300":
        benchmark = frame
        benchmark_source = dict(result["source"])
    else:
        try:
            query = {
                "symbol": "sh510300",
                "timeframe": "1d",
                "start": start.isoformat(),
                "cutoff": cutoff.isoformat(),
            }
            raw, url, retrieved, hit = cached_fetch(
                root, query, lambda: fetch_daily("sh510300", start, cutoff.date())
            )
            benchmark = parse_daily(raw, "sh510300", start, cutoff.date())
            checksum = hashlib.sha256(raw).hexdigest()
            benchmark_source = {
                "symbol": "sh510300",
                "timeframe": "1d",
                "url": url,
                "retrieved_at": retrieved,
                "cache_hit": hit,
                "raw_sha256": checksum,
                "artifact": checksum + ".json",
                "bars_sha256": logical_content_hash(benchmark),
                "bars": benchmark.height,
                "first": benchmark["timestamp"][0].date().isoformat(),
                "last": benchmark["timestamp"][-1].date().isoformat(),
            }
        except (OSError, ValueError, KeyError, TypeError, pl.exceptions.PolarsError):
            reference_error = "市场参照不可用，相对强弱留空，不填0。"
    inputs = prepare_inputs(
        frame, benchmark, base=result["synthesis"], as_of=cutoff.date()
    )
    previous = (
        prepare_inputs(frame.head(frame.height - 1), benchmark)
        if frame.height > 80
        else None
    )
    contexts = result["cross_timeframe"]["items"]
    decision = prediction(inputs, contexts, policy, previous)
    combined = deepcopy(result["synthesis"])
    combined.update(
        schema_version="chart-synthesis-v4",
        profile="day-five-v1",
        direction=decision["direction"],
        horizon_bars=5,
        horizon_specific=decision["horizon_specific"],
        primary_group=decision["primary_group"],
        risk_flags=decision["risk_flags"],
        abstention_reason=decision["abstention_reason"],
    )
    if decision["risk_flags"]:
        combined["risk_level"] = "HIGH"
    result["synthesis"] = combined
    result["cross_timeframe"]["status"] = decision["context_relation"]
    result["trade_plan"] = report_plan(
        inputs,
        decision,
        max_holding_bars=metadata.get("max_holding_bars") or 5,
        purchase_price=metadata.get("purchase_price"),
        held_bars=metadata.get("held_bars", 0),
        initial_levels=initial_levels,
        exit_policy=exit_policy,
    )
    result["quality"].update(
        inputs["quality"],
        trading_calendar_verified=inputs["quality"]["calendar_verified"],
    )
    result["daily_research"] = {
        "version": inputs["schema_version"],
        "target": inputs["target"],
        "policy": policy,
        "release_status": daily_release.STATUS,
        "horizon_specific": decision["horizon_specific"],
        "features": inputs["features"],
        "benchmark_scope": inputs["benchmark_scope"],
        "benchmark_error": reference_error,
        "benchmark_source": benchmark_source,
        "exit_policy": exit_policy,
        "evaluation_summary": deepcopy(daily_release.EVALUATION_SUMMARY),
    }
    result["research_sources"] = []
    if benchmark_source:
        result["research_sources"].append(benchmark_source)
    weekly = None
    for item in contexts:
        if item["timeframe"] == "1w" and item.get("source"):
            source = item["source"]
            raw = (root / source["artifact"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != source["raw_sha256"]:
                raise ValueError("Weekly source hash differs")
            weekly = parse_period(
                raw,
                source["symbol"],
                date.fromisoformat(source["first"][:10]),
                datetime.fromisoformat(source["cutoff_at"]),
                "1w",
            )[0]
    history, profile = cached_history(frame, weekly, benchmark, root)
    result["calculation_profile"] = profile
    begin = datetime.combine(
        date.fromisoformat(metadata["period_start"]), datetime.min.time(), UTC
    )
    window = frame.filter(pl.col("timestamp") >= begin)
    summaries = []
    if window.height >= 2:
        snapshots = {
            at: {"inputs": value["inputs"], "decision": value["decisions"][policy]}
            for at, value in history.items()
        }
        for friction in (10, 30):
            value = replay(
                window,
                snapshots,
                max_holding_bars=metadata.get("max_holding_bars") or 5,
                exit_policy=exit_policy,
                friction_bps=friction,
                tick_size="0.001" if kind == "ETF" else "0.01",
            )
            raw = canonical(value).encode()
            checksum = hashlib.sha256(raw).hexdigest()
            path = root / (checksum + ".json")
            if path.is_symlink():
                raise ValueError("Replay archive redirected")
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write(raw)
            elif path.read_bytes() != raw:
                raise ValueError("Replay archive differs")
            artifact = {
                "artifact": path.name,
                "raw_sha256": checksum,
                "provider": "DETERMINISTIC_REPORT_PLAN_REPLAY",
            }
            result["research_sources"].append(artifact)
            summaries.append(
                {
                    key: value[key]
                    for key in (
                        "friction_bps_each_side",
                        "price_return_after_assumed_costs",
                        "max_drawdown",
                        "closed_trades",
                        "open_quantity",
                        "blocked_attempts",
                        "execution_qualified",
                    )
                }
                | {
                    "entries": sum(
                        x["kind"] == "SIMULATED_ENTRY" for x in value["events"]
                    ),
                    "artifact_sha256": checksum,
                }
            )
    result["report_plan_replay"] = {
        "schema_version": "report-plan-replay-v1",
        "policy": policy,
        "forecast_horizon_bars": 5,
        "max_holding_bars": metadata.get("max_holding_bars") or 5,
        "review_interval_bars": 1,
        "summaries": summaries,
        "scope": "REPORT_SHARED_RULES_RESEARCH_ASSUMPTIONS_NOT_EXECUTION_PROOF",
        "limitations": [
            "公司行动现金流、历史涨跌幅与成交容量未核验。",
            "每侧10/30bp综合摩擦，包含费用和滑点；无实际订单。",
            "原始价格、保守T+1、虚拟手数与期末市值为研究口径。",
            "日线高低价及成交量仅作事后可行性筛选，未还原盘中排队及成交时刻。",
        ],
    }
