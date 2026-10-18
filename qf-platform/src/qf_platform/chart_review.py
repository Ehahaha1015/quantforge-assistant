"""User-triggered current-bar reviews, separate from reports and model budgets."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from quantforge.product.storage import local_root, writer_lock
from quantforge.strategies.chart_quality import is_session

from . import chart_algorithms
from .chart_conclusions import conclusion
from .chart_timeframes import ZONE, cutoff_at
from .storage import PlatformError, canonical, digest


def latest_expected(metadata: dict[str, Any], at: datetime) -> datetime | None:
    cutoff = cutoff_at(metadata, at)
    day = cutoff.astimezone(ZONE).date()
    for _ in range(15):
        session = is_session(day, at.astimezone(ZONE).date())
        if session is None:
            return None
        if session:
            slots = (
                (time(10, 30), time(11, 30), time(14), time(15))
                if metadata.get("timeframe") == "60m"
                else (time(15),)
            )
            eligible = [
                datetime.combine(day, value, ZONE)
                for value in slots
                if datetime.combine(day, value, ZONE) <= cutoff
            ]
            if eligible:
                return max(eligible)
        day -= timedelta(days=1)
    return None


def review(
    snapshot: dict[str, Any], root: Path, *, now: datetime | None = None
) -> dict[str, Any]:
    original = conclusion(snapshot)
    if (
        snapshot["status"] != "SUCCEEDED"
        or not original
        or original["status"] != "READY"
        or snapshot["request"].get("adjustment") != "RAW"
        or snapshot["report"].get("recognized_adjustment") != "RAW"
    ):
        raise PlatformError("CHART_REVIEW_REQUIRES_ALIGNED_REPORT")
    at = now or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("aware review time required")
    old = snapshot["algorithm_analysis"]
    origin = datetime.fromisoformat(old["source"]["last_timestamp"])
    if at < origin:
        raise ValueError("Review cannot precede original report")
    metadata = deepcopy(snapshot["request"])
    timeframe = metadata.get("timeframe", "1d")
    day = at.astimezone(ZONE).date()
    metadata.update(
        period_end=day.isoformat(),
        period_end_time=at.astimezone(ZONE).strftime("%H:%M"),
        period_start=(
            day - timedelta(days=366 if timeframe == "1w" else 93)
        ).isoformat(),
    )
    initial = (
        deepcopy(old["trade_plan"].get("levels"))
        if metadata.get("purchase_price")
        else None
    )
    with writer_lock(root):
        current = chart_algorithms.analyze(
            metadata,
            root,
            initial_levels=initial,
            as_of=at,
            review_origin=origin,
            review_reference=old["synthesis"]["indicators"]["reference_close"],
        )
        available = current["status"] == "AVAILABLE"
        plan = current.get("trade_plan") or {}
        direction = (current.get("synthesis") or {}).get("direction", "UNCLEAR")
        source = current.get("source")
        last = source["last_timestamp"] if source else None
        flags = (current.get("synthesis") or {}).get("risk_flags", [])
        if available:
            expected = latest_expected(metadata, at)
            if expected is None or datetime.fromisoformat(str(last)) != expected:
                available = False
                current["reason"] = "供应商未覆盖最新已完成交易日或交易日历尚未核验。"
        data_flags = {
            "SESSION_GAPS",
            "NON_SESSION_BARS",
            "ZERO_VOLUME_HISTORY",
            "POSSIBLE_CORPORATE_ACTION_OR_GAP",
            "CALENDAR_OUTSIDE_VERIFIED_RANGE",
            "POSSIBLE_PRICE_DISCONTINUITY",
            "INSUFFICIENT_HISTORY",
        }
        complete = available and not data_flags.intersection(flags)
        state = (
            "DATA_UNAVAILABLE"
            if not complete
            else "NO_NEW_BAR"
            if last == origin.isoformat()
            else "UNCHANGED"
            if direction == original["direction"]
            and plan.get("action") == original["action"]
            and flags == old["synthesis"].get("risk_flags", [])
            else "CHANGED"
        )
        value = {
            "schema_version": "chart-review-v1",
            "job_id": snapshot["id"],
            "status": state,
            "original_snapshot_sha256": digest(snapshot),
            "reviewed_at": at.isoformat(),
            "original_as_of": origin.isoformat(),
            "latest_as_of": last,
            "original_direction": original["direction"],
            "direction": direction if complete else "UNCLEAR",
            "original_action": original["action"],
            "action": plan.get("action", "UNAVAILABLE") if complete else "UNAVAILABLE",
            "held_bars": plan.get("held_bars"),
            "levels": plan.get("levels") if complete else None,
            "reasons": plan.get("reasons", []) if available else [current["reason"]],
            "core_source_sha256": current.get("core_source_sha256"),
            "source": source,
            "forecast_validated": False,
            "model_calls": 0,
            "note": (
                "仅按最新完整行情复评固定规则，不重新识图；"
                "原报告和初始关联参考线不变，不代表实际持仓或成交。"
            ),
        }
        directory = root / "reviews"
        directory.mkdir(exist_ok=True)
        local_root(directory)
        key = digest(
            {
                **{
                    k: v for k, v in value.items() if k not in {"reviewed_at", "source"}
                },
                "source": {
                    k: source.get(k)
                    for k in (
                        "symbol",
                        "timeframe",
                        "raw_sha256",
                        "bars_sha256",
                        "last_timestamp",
                    )
                }
                if source
                else None,
            }
        )
        path = directory / (key + ".json")
        if path.is_symlink():
            raise ValueError("Review archive redirected")
        if path.exists():
            saved = json.loads(path.read_bytes())
            if (
                digest(
                    {
                        "review": saved["review"],
                        "latest_algorithm": saved["latest_algorithm"],
                    }
                )
                != saved["sha256"]
            ):
                raise ValueError("Review archive differs")
            return dict(saved["review"])
        with path.open("x", encoding="utf-8") as stream:
            stream.write(
                canonical(
                    {
                        "review": value,
                        "sha256": digest(
                            {"review": value, "latest_algorithm": current}
                        ),
                        "latest_algorithm": current,
                    }
                )
            )
        return value
