"""Keep submitted metadata separate from image-derived analysis metadata."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, cast


def metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], snapshot.get("resolved_request") or snapshot["request"])


def resolve(
    snapshot: dict[str, Any], report: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    from .chart_algorithms import symbol_code
    from .chart_reports import ChartRequest

    submitted = snapshot["request"]
    codes = set(
        re.findall(r"(?<!\d)\d{6}(?!\d)", report.get("recognized_symbol") or "")
    )
    timeframe = report.get("recognized_timeframe")
    period = report.get("recognized_period") or ""
    dates = re.findall(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?(?!\d)", period)
    notes = []
    if len(codes) != 1:
        notes.append("未能唯一辨认证券，先展示图像证据与条件情景。")
    if timeframe not in {"60m", "1d", "1w"}:
        notes.append("未识别为受支持K线周期；分时图先展示图像情景，不套用日线指标。")
    if not dates:
        notes.append("图中日期缺失，先展示图像情景，不把上传日期当作交易日期。")
    if notes:
        return None, notes
    timeframe = str(timeframe)
    try:
        symbol = codes.pop()
        symbol_code(symbol)
        values = [
            datetime(int(y), int(m), int(d)).date().isoformat() for y, m, d in dates
        ]
        if "1970-01-01" in values:
            return None, ["识别结果含协议占位日期，不据此查询行情；先展示图像情景。"]
        if any(a > b for a, b in zip(values, values[1:], strict=False)):
            raise ValueError("unordered dates")
        clocks = re.findall(r"(?<!\d)([0-2]\d:[0-5]\d)(?!\d)", period)
        if timeframe == "60m" and not clocks:
            return None, ["小时图截止时间未识别，先展示图像情景；不会默认成15:00。"]
        values_request = {
            **submitted,
            "symbol": symbol,
            "timeframe": timeframe,
            "period_start": values[0],
            "period_end": values[-1],
            "period_end_time": clocks[-1] if timeframe == "60m" else "15:00",
            "adjustment": report.get("recognized_adjustment") or "UNKNOWN",
            "horizon_bars": {"60m": 4, "1d": 5, "1w": 1}[timeframe],
            "max_holding_bars": None,
        }
        # Validation is shared with manual submissions; the image is already verified.
        parsed = ChartRequest.model_validate(
            {**values_request, "image_base64": "x" * 20}
        )
        values_request = parsed.model_dump(exclude={"image_base64"})
        notes = [
            "代码、日期与周期由本次识图提取，可在识别结果中核对；未标记为人工确认。"
        ]
        if values_request["adjustment"] != "RAW":
            notes.append(
                "截图复权未与不复权行情对齐，图中行情仅作独立背景，不展示精确买卖参考。"
            )
        return values_request, notes
    except (ValueError, TypeError, KeyError):
        return None, ["图中元数据尚不完整，已保留图像分析与条件情景。"]
