"""Versioned exchange schedules and raw daily-price quality, never inferred adjustments."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .moving_average import _single_symbol_bars

VERSION = "chart-daily-quality-v1"
SCHEDULE = {
    2024: (
        ("01-01", "01-01"),
        ("02-09", "02-17"),
        ("04-04", "04-06"),
        ("05-01", "05-05"),
        ("06-10", "06-10"),
        ("09-15", "09-17"),
        ("10-01", "10-07"),
    ),
    2025: (
        ("01-01", "01-01"),
        ("01-28", "02-04"),
        ("04-04", "04-06"),
        ("05-01", "05-05"),
        ("05-31", "06-02"),
        ("10-01", "10-08"),
    ),
    2026: (
        ("01-01", "01-03"),
        ("02-15", "02-23"),
        ("04-04", "04-06"),
        ("05-01", "05-05"),
        ("06-19", "06-21"),
        ("09-25", "09-27"),
        ("10-01", "10-07"),
    ),
}
SCHEDULE_KNOWN_AT = {
    2024: date(2023, 12, 26),
    2025: date(2024, 12, 23),
    2026: date(2025, 12, 22),
}
SOURCES = {
    "SSE2024": "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20231226_5733941.shtml",
    "SSE2025": "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20241223_10767110.shtml",
    "SSE2026": "https://www.sse.com.cn/disclosure/dealinstruc/closed/",
    "SZSE2024": "https://www.szse.cn/disclosure/notice/t20231226_605108.html",
    "SZSE2025": "https://www.szse.cn/disclosure/notice/t20241223_611283.html",
    "SZSE2026": "https://www.szse.cn/disclosure/notice/t20251222_618087.html",
}


def is_session(day: date, known_at: date | None = None) -> bool | None:
    if day.year not in SCHEDULE or (
        known_at is not None and known_at < SCHEDULE_KNOWN_AT[day.year]
    ):
        return None
    if day.weekday() >= 5:
        return False
    return not any(
        date.fromisoformat(f"{day.year}-{start}")
        <= day
        <= date.fromisoformat(f"{day.year}-{end}")
        for start, end in SCHEDULE[day.year]
    )


def daily_quality(bars: pl.DataFrame, as_of: date | None = None) -> dict[str, Any]:
    frame = _single_symbol_bars(bars)
    zone = ZoneInfo("Asia/Shanghai")
    days = [at.astimezone(zone).date() for at in frame["timestamp"]]
    end = as_of or days[-1]
    if days[-1] > end:
        raise ValueError("quality cannot use future daily bars")
    recent = set(days[-80:])
    start = days[max(0, len(days) - 80)]
    missing, unknown = [], []
    cursor = start
    while cursor <= end:
        session = is_session(cursor, end)
        if session is None:
            unknown.append(cursor.isoformat())
        elif session and cursor not in recent:
            missing.append(cursor.isoformat())
        cursor += timedelta(days=1)
    unexpected = [x.isoformat() for x in days[-80:] if is_session(x, end) is False]
    zero = [
        days[i].isoformat()
        for i in range(max(0, len(days) - 80), len(days))
        if frame["volume"][i] <= 0
    ]
    jumps = []
    for index in range(max(1, len(days) - 20), len(days)):
        previous = Decimal(str(frame["close"][index - 1]))
        if abs(Decimal(str(frame["open"][index])) / previous - 1) > Decimal("0.15"):
            jumps.append(days[index].isoformat())
    flags = []
    if frame.height < 80:
        flags.append("INSUFFICIENT_HISTORY")
    if missing:
        flags.append("SESSION_GAPS")
    if unexpected:
        flags.append("NON_SESSION_BARS")
    if zero:
        flags.append("ZERO_VOLUME_HISTORY")
    if jumps:
        flags.append("POSSIBLE_CORPORATE_ACTION_OR_GAP")
    if unknown:
        flags.append("CALENDAR_OUTSIDE_VERIFIED_RANGE")
    return {
        "schema_version": VERSION,
        "bars": frame.height,
        "as_of": end.isoformat(),
        "calendar_verified": not unknown,
        "calendar_scope": "ANNUAL_SCHEDULE_NOT_EMERGENCY_CLOSURES",
        "missing_sessions": missing,
        "unexpected_sessions": unexpected,
        "zero_volume_dates": zero,
        "suspected_discontinuities": jumps,
        "hard_flags": flags,
        "corporate_actions_verified": False,
        "adjustment": "RAW_UNADJUSTED",
        "volume_unit": "VENDOR_UNVERIFIED",
        "eligible": not flags,
    }
