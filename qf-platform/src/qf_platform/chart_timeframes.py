"""Bounded closed-bar timestamps and vendor parsing for hourly/weekly charts."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

import polars as pl
from quantforge.data.bars import BAR_SCHEMA, prepare_bars

ZONE = ZoneInfo("Asia/Shanghai")
LABELS = {"60m": "60分钟", "1d": "日线", "1w": "周线"}


def horizon(meta: dict[str, Any]) -> int:
    return int(
        meta.get("horizon_bars")
        or (
            4
            if meta.get("timeframe") == "60m"
            else 1
            if meta.get("timeframe") == "1w"
            else meta.get("horizon_sessions", 5)
        )
    )


def horizon_text(meta: dict[str, Any]) -> str:
    return f"后续{horizon(meta)}根{LABELS[meta.get('timeframe', '1d')]}K线"


def cutoff_at(meta: dict[str, Any], now: datetime | None = None) -> datetime:
    current = (now or datetime.now(ZONE)).astimezone(ZONE)
    kind = meta.get("timeframe", "1d")
    requested = datetime.combine(
        date.fromisoformat(meta["period_end"]),
        time.fromisoformat(meta.get("period_end_time", "15:00"))
        if kind == "60m"
        else time(16),
        ZONE,
    )
    cutoff = min(requested, current)
    if kind == "60m":
        return cutoff
    day = cutoff.date() if cutoff.hour >= 16 else cutoff.date() - timedelta(days=1)
    if kind == "1w":
        day -= timedelta(days=(day.weekday() - 4) % 7)
    return datetime.combine(day, time(16), ZONE)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def fetch_period(
    code: str, start: date, cutoff: datetime, timeframe: str
) -> tuple[bytes, str]:
    if timeframe == "60m":
        url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},m60,,640"
    elif timeframe == "1w":
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},week,{start.isoformat()},{cutoff.date().isoformat()},640,"
    else:
        raise ValueError("周期未支持。")
    with build_opener(NoRedirect()).open(
        Request(url, headers={"User-Agent": "QuantForge-research/0.1"}), timeout=15
    ) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("行情响应地址或状态异常。")
        raw = response.read(1_000_001)
    if not raw or len(raw) > 1_000_000:
        raise ValueError("行情响应为空或过大。")
    return raw, url


def parse_period(
    raw: bytes, code: str, start: date, cutoff: datetime, timeframe: str
) -> tuple[pl.DataFrame, int]:
    value = json.loads(raw)
    if type(value.get("code")) is not int or value["code"] != 0:
        raise ValueError("行情供应商拒绝请求。")
    key = "m60" if timeframe == "60m" else "week"
    rows = value["data"][code][key]  # No adjusted key/current qt quote fallback.
    if not isinstance(rows, list) or not 1 <= len(rows) <= 640:
        raise ValueError("缺少有界行情数据。")
    values, discarded = [], 0
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise ValueError("行情字段不完整。")
        at = (
            datetime.strptime(row[0], "%Y%m%d%H%M").replace(tzinfo=ZONE)
            if timeframe == "60m"
            else datetime.combine(date.fromisoformat(row[0]), time(15), ZONE)
        )
        # Hourly endpoint returns a rolling window; discard all future rows
        # BEFORE touching their prices. Later bars cannot affect a past plan.
        if at > cutoff or at.date() < start:
            discarded += 1
            continue
        if timeframe == "60m" and (
            at.strftime("%H:%M") not in {"10:30", "11:30", "14:00", "15:00"}
            or at.weekday() > 4
        ):
            raise ValueError("60分钟K线时间标签不符合声明的沪深交易时段。")
        if any(isinstance(x, bool) for x in row[1:6]):
            raise ValueError("行情数值格式异常。")
        open_, close, high, low, volume = map(float, row[1:6])
        values.append(
            {
                "symbol": code,
                "timestamp": at.astimezone(UTC),
                "open": open_,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
            }
        )
    if not values:
        raise ValueError(
            "请求时点没有完整K线；60分钟接口仅覆盖最近640根，不能用日线替代。"
        )
    frame = prepare_bars(pl.DataFrame(values, schema=BAR_SCHEMA), as_of=cutoff)
    if (cutoff.date() - frame["timestamp"][-1].astimezone(ZONE).date()).days > 14:
        raise ValueError("末根K线过旧，无法生成该时点的建议。")
    return frame, discarded


def bar_label(at: datetime, timeframe: str) -> str:
    local = at.astimezone(ZONE)
    return local.isoformat() if timeframe == "60m" else local.date().isoformat()
