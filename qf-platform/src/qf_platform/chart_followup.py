"""Read-only outcome observations, separate from immutable original report snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from quantforge.data.bars import logical_content_hash
from quantforge.product.storage import local_root, writer_lock
from quantforge.strategies.chart_evaluation import assess_observation

from .chart_algorithms import fetch_daily, parse_daily, symbol_code
from .chart_market_cache import cached_fetch
from .chart_timeframes import ZONE, cutoff_at, fetch_period, parse_period
from .storage import canonical, digest


def observe(snapshot: dict[str, Any], root: Path) -> dict[str, Any]:
    with writer_lock(root):
        return _observe(snapshot, root)


def _observe(snapshot: dict[str, Any], root: Path) -> dict[str, Any]:
    target = snapshot.get("evaluation_target")
    if target is None:
        return {
            "schema_version": "chart-observation-v1",
            "status": "NO_FROZEN_TARGET",
            "note": "此报告没有冻结的综合预测目标；不追溯补造。",
            "forecast_validated": False,
        }
    directory = local_root(root)
    artifact = directory / ("observation-" + snapshot["id"] + ".json")
    if artifact.is_symlink():
        raise ValueError("观察结果归档路径异常。")
    if artifact.exists():
        saved = json.loads(artifact.read_bytes())
        result = cast(dict[str, Any], saved["observation"])
        if saved["sha256"] != digest(result) or result["target_sha256"] != digest(
            target
        ):
            raise ValueError("观察归档或冻结目标校验失败。")
        source = directory / (result["source"]["raw_sha256"] + ".json")
        if (
            source.is_symlink()
            or hashlib.sha256(source.read_bytes()).hexdigest()
            != result["source"]["raw_sha256"]
        ):
            raise ValueError("观察行情原响应校验失败。")
        return result
    now = datetime.now(UTC)
    timeframe = target["timeframe"]
    origin = datetime.fromisoformat(target["origin"])
    metadata = {
        "timeframe": timeframe,
        "period_end": now.astimezone(ZONE).date().isoformat(),
        "period_end_time": now.astimezone(ZONE).strftime("%H:%M"),
    }
    cutoff = cutoff_at(metadata, now)
    code = symbol_code(snapshot["request"]["symbol"])
    start = origin.astimezone(ZONE).date() - timedelta(days=7)
    query = {
        "followup": True,
        "symbol": code,
        "timeframe": timeframe,
        "start": start.isoformat(),
        "cutoff": cutoff.isoformat(),
    }
    raw, url, retrieved_at, _ = cached_fetch(
        root,
        query,
        lambda: (
            fetch_daily(code, start, cutoff.date())
            if timeframe == "1d"
            else fetch_period(code, start, cutoff, timeframe)
        ),
    )
    if timeframe == "1d":
        frame = parse_daily(raw, code, start, cutoff.date())
    else:
        frame, _ = parse_period(raw, code, start, cutoff, timeframe)
    result = assess_observation(
        target, frame, cutoff, (snapshot.get("conclusion") or {}).get("levels")
    )
    result.update(
        job_id=snapshot["id"],
        target_sha256=digest(target),
        observed_at=now.isoformat(),
        source={
            "url": url,
            "retrieved_at": retrieved_at,
            "raw_sha256": digest_bytes(raw),
            "bars_sha256": logical_content_hash(frame),
        },
        note="收盘方向结果与参考线触及分开评估；不代表实际成交或已验证预测能力。",
    )
    # Save a final observation once; original jobs and budgets remain unchanged.
    if result["status"] in {"MATURED", "ORIGIN_MISSING", "ORIGIN_REVISED"}:
        with artifact.open("x", encoding="utf-8") as stream:
            stream.write(canonical({"observation": result, "sha256": digest(result)}))
    return result


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
