"""Rebuild historical candles and existing direction thresholds from frozen data."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Context, Decimal, DecimalException, localcontext
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, Field
from quantforge.data.bars import BAR_SCHEMA, logical_content_hash, prepare_bars
from quantforge.strategies.chart_evaluation import label_threshold

from .chart_algorithms import parse_daily
from .chart_conclusions import conclusion
from .chart_metadata import metadata as analysis_metadata
from .chart_quality_gate import quality_blockers
from .chart_timeframes import bar_label, cutoff_at, horizon, parse_period
from .storage import digest

Price = str
Direction = Literal["BULLISH", "RANGE", "BEARISH", "UNCLEAR"]


class Candle(BaseModel):
    at: str
    label: str
    open: Price
    high: Price
    low: Price
    close: Price
    sma20: Price | None
    sma60: Price | None


class Boundary(BaseModel):
    step: int = Field(ge=0, le=20)
    upper: Price
    reference: Price
    lower: Price


class ReferenceLevels(BaseModel):
    reference: Price
    stop_reference: Price
    take_profit_reference: Price
    rule: str


class ChartVisualization(BaseModel):
    schema_version: Literal["chart-visualization-v1"] = "chart-visualization-v1"
    job_id: str
    snapshot_sha256: str
    status: Literal["AVAILABLE", "BLOCKED", "UNAVAILABLE"]
    view_mode: Literal["ALIGNED", "BACKGROUND"] = "ALIGNED"
    warnings: list[str] = Field(default_factory=list)
    reason: str | None
    symbol: str
    timeframe: Literal["60m", "1d", "1w"]
    horizon_bars: int = Field(ge=1, le=20)
    direction: Direction = "UNCLEAR"
    as_of: str | None = None
    offline: bool = False
    source_sha256: str | None = None
    bars_sha256: str | None = None
    candles: list[Candle] = Field(default_factory=list, max_length=120)
    boundaries: list[Boundary] = Field(default_factory=list, max_length=21)
    levels: ReferenceLevels | None = None
    atr14: Price | None = None
    reference_close: Price | None = None
    formula: str = "归档收盘价 ± 0.5 × ATR14 × √后续K线根数"
    interpretation: str = (
        "未来区域复用现有方向评测阈值，表示涨跌幅如何分类；"
        "不是预计价格路径、置信区间或涨跌概率。"
    )
    model_calls: Literal[0] = 0
    market_fetches: Literal[0] = 0
    forecast_validated: Literal[False] = False


def _frame(snapshot: dict[str, Any], directory: Path) -> pl.DataFrame:
    source = snapshot["algorithm_analysis"]["source"]
    checksum = source["raw_sha256"]
    if len(checksum) != 64 or any(x not in "0123456789abcdef" for x in checksum):
        raise ValueError("Invalid archive identity")
    if source["artifact"] != checksum + ".json":
        raise ValueError("Invalid archive name")
    path = directory / source["artifact"]
    if path.is_symlink() or path.is_junction():
        raise ValueError("Redirected market archive")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError("Market archive checksum differs")
    meta = analysis_metadata(snapshot)
    timeframe = meta.get("timeframe", "1d")
    if source.get("timeframe", timeframe) != timeframe:
        raise ValueError("Archived timeframe differs from report")
    if source.get("provider") == "OFFLINE_SYNTHETIC":
        payload = json.loads(raw)
        if payload.get("fixture") != "SYNTHETIC_NOT_MARKET_DATA":
            raise ValueError("Offline archive identity differs")
        rows = [
            {**row, "timestamp": datetime.fromisoformat(row["timestamp"])}
            for row in payload["bars"]
        ]
        frame = prepare_bars(
            pl.DataFrame(rows, schema=BAR_SCHEMA),
            as_of=datetime.fromisoformat(source["last_timestamp"]),
        )
    elif source.get("provider") in {None, "TENCENT_PUBLIC_HTTPS"}:
        start = date.fromisoformat(source["first"][:10])
        if timeframe == "1d":
            frame = parse_daily(
                raw, source["symbol"], start, date.fromisoformat(source["cutoff"])
            )
        else:
            cutoff = (
                datetime.fromisoformat(source["cutoff_at"])
                if source.get("cutoff_at")
                else cutoff_at(meta)
            )
            frame = parse_period(raw, source["symbol"], start, cutoff, timeframe)[0]
    else:
        raise ValueError("Unsupported archived source")
    if logical_content_hash(frame) != source["bars_sha256"]:
        raise ValueError("Normalized bars differ from frozen report")
    return frame


def visualization(
    snapshot: dict[str, Any], directory: Path, *, preview: bool = False
) -> ChartVisualization:
    meta = analysis_metadata(snapshot)
    result = ChartVisualization(
        job_id=snapshot["id"],
        snapshot_sha256=digest(snapshot),
        status="BLOCKED",
        reason="先完成证券、日期、周期和复权核对，才能展示价格情景。",
        symbol=meta["symbol"],
        timeframe=meta.get("timeframe", "1d"),
        horizon_bars=horizon(meta),
    )
    combined = conclusion(snapshot)
    report = snapshot.get("report") or {}
    background = False
    if (
        snapshot["status"] != "SUCCEEDED"
        or not combined
        or combined["status"] != "READY"
    ):
        if (
            not preview
            or snapshot["status"] != "SUCCEEDED"
            or report.get("image_quality") not in {"READABLE", "PARTIAL"}
        ):
            return result
        codes = set(
            re.findall(r"(?<!\d)\d{6}(?!\d)", report.get("recognized_symbol") or "")
        )
        expected = set(re.findall(r"(?<!\d)\d{6}(?!\d)", meta["symbol"]))
        if len(codes) != 1 or codes != expected:
            return result
        if report.get("recognized_timeframe") != meta.get("timeframe", "1d"):
            return result
        background = True
        result.view_mode = "BACKGROUND"
        result.warnings = [
            (combined or {}).get("alignment_note", "图像元数据尚未全部核对。"),
            "下图为同一证券的供应商不复权行情背景，与截图价格口径可能不同；不叠加精确买卖参考。",
        ]
    blockers = quality_blockers(snapshot)
    if blockers:
        result.reason = "；".join(blockers)
        return result
    algorithm = snapshot.get("algorithm_analysis") or {}
    source = algorithm.get("source")
    if algorithm.get("status") != "AVAILABLE" or not source:
        result.reason = "这份报告没有可用的归档行情。"
        return result
    try:
        frame = _frame(snapshot, directory)
        reference = Decimal(algorithm["synthesis"]["indicators"]["reference_close"])
        atr = Decimal(algorithm["synthesis"]["indicators"]["atr14"])
        if (
            not reference.is_finite()
            or not atr.is_finite()
            or reference <= 0
            or atr <= 0
        ):
            raise ValueError("No positive reference and volatility scale")
        if Decimal(str(frame["close"][-1])) != reference:
            raise ValueError("Archived close differs from synthesis reference")
        if frame.height < 80:
            raise ValueError("Insufficient warm-up history")
        with localcontext(Context(prec=50)):
            closes = [Decimal(str(x)) for x in frame["close"]]
            candles = []
            first = max(0, frame.height - 120)
            for index, row in enumerate(frame.iter_rows(named=True)):
                if index < first:
                    continue
                candles.append(
                    Candle(
                        at=row["timestamp"].isoformat(),
                        label=bar_label(row["timestamp"], result.timeframe),
                        **{
                            key: str(row[key])
                            for key in ("open", "high", "low", "close")
                        },
                        sma20=str(sum(closes[index - 19 : index + 1]) / 20)
                        if index >= 19
                        else None,
                        sma60=str(sum(closes[index - 59 : index + 1]) / 60)
                        if index >= 59
                        else None,
                    )
                )
            result.boundaries = [
                Boundary(
                    step=0,
                    upper=str(reference),
                    reference=str(reference),
                    lower=str(reference),
                )
            ]
            for step in range(1, result.horizon_bars + 1):
                span = reference * label_threshold(reference, atr, step)
                # A negative lower boundary is mathematically possible for this
                # descriptive rule; do not silently clip or change its meaning.
                if reference - span <= 0:
                    raise ValueError("Threshold exceeds positive price domain")
                result.boundaries.append(
                    Boundary(
                        step=step,
                        upper=str(reference + span),
                        reference=str(reference),
                        lower=str(reference - span),
                    )
                )
        plan = algorithm.get("trade_plan") or {}
        if plan.get("levels") and not background:
            levels = ReferenceLevels.model_validate(plan["levels"])
            for value in (
                levels.reference,
                levels.stop_reference,
                levels.take_profit_reference,
            ):
                price = Decimal(value)
                if not price.is_finite() or price <= 0:
                    raise ValueError("Invalid price reference")
            result.levels = levels
        result.status = "AVAILABLE"
        result.reason = None
        result.candles = candles
        result.reference_close = str(reference)
        result.atr14 = str(atr)
        result.direction = (
            combined["direction"] if combined and not background else "UNCLEAR"
        )
        result.as_of = source["last"]
        result.offline = source.get("provider") == "OFFLINE_SYNTHETIC"
        result.source_sha256 = source["raw_sha256"]
        result.bars_sha256 = source["bars_sha256"]
        return result
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        DecimalException,
        pl.exceptions.PolarsError,
    ):
        return result.model_copy(
            update={
                "status": "UNAVAILABLE",
                "reason": "归档行情或波动尺度无法核验，暂不绘制价格情景。",
                "candles": [],
                "boundaries": [],
                "levels": None,
            }
        )
