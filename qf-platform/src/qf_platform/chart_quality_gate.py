"""Shared conservative presentation gate for archived data quality."""

from __future__ import annotations

from datetime import date
from typing import Any

from quantforge.strategies.chart_quality import SCHEDULE, SOURCES

BLOCKING_FLAGS = {
    "SESSION_GAPS": "交易日行情有缺口，尚未区分停牌与缺失。",
    "NON_SESSION_BARS": "行情含非交易日记录。",
    "ZERO_VOLUME_HISTORY": "历史成交量有零值或异常。",
    "POSSIBLE_CORPORATE_ACTION_OR_GAP": "价格跳变可能涉及公司行动，需核对。",
    "CALENDAR_OUTSIDE_VERIFIED_RANGE": "该时点超出已核验交易日历。",
    "POSSIBLE_PRICE_DISCONTINUITY": "价格可能不连续，暂停精确参考。",
    "INSUFFICIENT_HISTORY": "指标预热历史不足。",
}


def quality_blockers(snapshot: dict[str, Any]) -> list[str]:
    algorithm = snapshot.get("algorithm_analysis") or {}
    quality = algorithm.get("quality") or {}
    flags = list((algorithm.get("synthesis") or {}).get("risk_flags", []))
    flags.extend(quality.get("hard_flags", []))
    source = algorithm.get("source") or {}
    # Evaluate the archived period, not today's calendar: old reports stay dated.
    last = source.get("last")
    if last:
        try:
            if date.fromisoformat(last[:10]).year not in SCHEDULE:
                flags.append("CALENDAR_OUTSIDE_VERIFIED_RANGE")
        except (ValueError, TypeError):
            flags.append("CALENDAR_OUTSIDE_VERIFIED_RANGE")
    return [BLOCKING_FLAGS[x] for x in dict.fromkeys(flags) if x in BLOCKING_FLAGS]


def calendar_status(today: date) -> dict[str, Any]:
    years = sorted(SCHEDULE)
    return {
        "schema_version": "chart-calendar-status-v1",
        "supported_years": years,
        "current_year_supported": today.year in SCHEDULE,
        "next_year_supported": today.year + 1 in SCHEDULE,
        "maintenance_due": today.year not in SCHEDULE
        or (today.month >= 10 and today.year + 1 not in SCHEDULE),
        "sources": SOURCES,
        "scope": "ANNUAL_SCHEDULE_NOT_EMERGENCY_CLOSURES",
        "note": (
            "未核验年份继续返回未知，不用工作日推断交易日；年度日历不证明个股未停牌。"
        ),
    }
