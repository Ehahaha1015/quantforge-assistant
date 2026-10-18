"""Read-only daily-data bridge to the existing deterministic Core strategies."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

import polars as pl
from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.data.bars import BAR_SCHEMA, logical_content_hash, prepare_bars
from quantforge.product.context import source_identity
from quantforge.product.storage import local_root
from quantforge.strategies.chart_decision import context_relation
from quantforge.strategies.chart_suite import (
    conditional_plan,
    generate_chart_signals,
    rules,
)
from quantforge.strategies.chart_synthesis import synthesize

from .chart_market_cache import cached_fetch
from .chart_timeframes import (
    ZONE,
    bar_label,
    cutoff_at,
    fetch_period,
    horizon,
    parse_period,
)

VERSION = "core-chart-strategies-v4"
LIMITATIONS = [
    "按用户确认的代码与周期查询沪深完整K线；尚未独立核验截图中的证券和复权口径。",
    "行情为腾讯公开HTTPS供应商数据，不是交易所认证数据；不复权，成交量沿用供应商原单位。",
    "未核验停牌、交易日完整性和公司行动；分红送转可能使价格形态失真。",
    "7类8组固定参数策略按3组汇总证据；空仓不等于看跌，一致性不是概率。",
    "历史回放为价格序列模拟，不含分红总收益、T+1、涨跌停、整手与成交可得性；不代表实际可交易收益。",
    "本次不执行自动调参、组合风控或订单，也没有验证未来预测准确率。",
]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def symbol_code(value: str) -> str:
    match = re.fullmatch(
        r"(?:(sh|sz)[.:-]?)?([0-9]{6})(?:\.(SH|SZ))?", value.strip(), re.I
    )
    if not match:
        raise ValueError("请填写沪深六位股票或ETF代码，例如600519或sh600519。")
    prefix, code, suffix = match.groups()
    exchange = "sh" if code.startswith(("6", "5")) else "sz"
    if not code.startswith(("6", "5", "00", "30", "15", "16")):
        raise ValueError("当前自动行情支持沪深股票和ETF，暂不支持该代码范围。")
    if any(part and part.lower() != exchange for part in (prefix, suffix)):
        raise ValueError("证券代码与交易所前后缀不一致。")
    return exchange + code


def fetch_daily(code: str, start: date, end: date) -> tuple[bytes, str]:
    # Fixed HTTPS host; neither the user nor model can supply a URL.
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={code},day,{start.isoformat()},{end.isoformat()},640,"
    )
    with build_opener(NoRedirect()).open(
        Request(url, headers={"User-Agent": "QuantForge-research/0.1"}), timeout=15
    ) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("行情服务返回了非预期地址或状态。")
        raw = response.read(1_000_001)
    if not raw or len(raw) > 1_000_000:
        raise ValueError("行情响应为空或超过大小限制。")
    return raw, url


def parse_daily(raw: bytes, code: str, start: date, cutoff: date) -> pl.DataFrame:
    payload = json.loads(raw)
    if type(payload.get("code")) is not int or payload["code"] != 0:
        raise ValueError("行情供应商拒绝该请求。")
    rows = payload["data"][code]["day"]  # Never accept qfqday or current qt quotes.
    if not isinstance(rows, list) or not 1 <= len(rows) <= 640:
        raise ValueError("没有有效的有界不复权日线。")
    values = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise ValueError("日线字段不完整。")
        day = date.fromisoformat(row[0])
        if day < start or day > cutoff:
            raise ValueError("行情包含请求日期范围以外的数据。")
        if any(isinstance(x, bool) for x in row[1:6]):
            raise ValueError("日线数值格式异常。")
        open_, close, high, low, volume = map(float, row[1:6])
        values.append(
            {
                "symbol": code,
                "timestamp": datetime.combine(day, time(7), UTC),
                "open": open_,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
            }
        )
    frame = prepare_bars(
        pl.DataFrame(values, schema=BAR_SCHEMA),
        as_of=datetime.combine(cutoff, time(7), UTC),
    )
    last = frame["timestamp"][-1].date()
    if (cutoff - last).days > 14:
        raise ValueError("末根日线距指定截止日超过14天，不能当作该时点的有效分析。")
    return frame


def evaluate(
    frame: pl.DataFrame,
    period_start: date,
    metadata: dict[str, Any] | None = None,
    initial_levels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = metadata or {}
    timeframe = meta.get("timeframe", "1d")
    first = datetime.combine(period_start, time(0), ZONE).astimezone(UTC)
    window = frame.filter(pl.col("timestamp") >= first)
    if window.is_empty():
        raise ValueError("截图所示区间没有完整K线，不计算算法。")
    strategies = []
    periods_per_year = {"60m": 1008, "1d": 252, "1w": 52}[timeframe]
    for rule in rules():
        item: dict[str, Any] = {
            "id": rule.id,
            "label": rule.label,
            "family": rule.family,
            "parameters": rule.parameters,
            "required_bars": rule.required_bars,
            "entry_rule": rule.entry_rule,
            "exit_rule": rule.exit_rule,
            "status": "INSUFFICIENT_HISTORY",
            "latest_state": None,
            "previous_state": None,
            "replay": [],
        }
        if frame.height >= rule.required_bars:
            signals = generate_chart_signals(frame, rule)
            item.update(
                status="COMPUTED",
                latest_state=int(signals["value"][-1]),
                previous_state=int(signals["value"][-2])
                if frame.height > rule.required_bars
                else None,
            )
            if (
                frame.height - window.height >= rule.required_bars
                and window.height >= 2
            ):
                for fee in (10, 30):
                    replay = run_vectorized_backtest(
                        window,
                        signals.tail(window.height),
                        BacktestConfig(fee_bps=fee, periods_per_year=periods_per_year),
                    )
                    item["replay"].append(
                        {
                            "fee_bps": fee,
                            "price_return": replay.metrics.total_return,
                            "max_drawdown": replay.metrics.max_drawdown,
                            "turnover": replay.metrics.turnover,
                        }
                    )
        strategies.append(item)
    baseline = []
    if window.height >= 2:
        signals = window.select("symbol", "timestamp").with_columns(
            pl.lit(1.0).alias("value")
        )
        for fee in (10, 30):
            replay = run_vectorized_backtest(
                window,
                signals,
                BacktestConfig(fee_bps=fee, periods_per_year=periods_per_year),
            )
            baseline.append(
                {
                    "fee_bps": fee,
                    "price_return": replay.metrics.total_return,
                    "max_drawdown": replay.metrics.max_drawdown,
                }
            )
    plan = conditional_plan(
        frame,
        timeframe=timeframe,
        horizon_bars=horizon(meta),
        purchase_price=meta.get("purchase_price"),
        held_bars=meta.get("held_bars", 0),
        initial_levels=initial_levels,
    )
    plan["as_of_bar"] = bar_label(frame["timestamp"][-1], timeframe)
    return {
        "strategies": strategies,
        "baseline": baseline,
        "trade_plan": plan,
        "timeframe": timeframe,
        "family_count": 7,
        "synthesis": synthesize(
            frame,
            timeframe,
            {x["id"]: (x["latest_state"], x["previous_state"]) for x in strategies},
        ),
        "evaluation_first": bar_label(window["timestamp"][0], timeframe),
        "evaluation_last": bar_label(window["timestamp"][-1], timeframe),
        "evaluation_bars": window.height,
        "replay_convention": (
            "完整K线收盘信号在下一根开盘执行；空仓起步，10/30bp总摩擦。"
            "小时回放未模拟T+1，非可交易收益。"
        ),
    }


def analyze(
    metadata: dict[str, Any],
    root: Path,
    *,
    initial_levels: dict[str, Any] | None = None,
    as_of: datetime | None = None,
    review_origin: datetime | None = None,
    review_reference: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": VERSION,
        "status": "UNAVAILABLE",
        "reason": None,
        "source": None,
        "strategies": [],
        "baseline": [],
        "trade_plan": None,
        "timeframe": metadata.get("timeframe", "1d"),
        "limitations": LIMITATIONS,
        "forecast_validated": False,
        "screenshot_identity_verified": False,
    }
    try:
        code = symbol_code(metadata["symbol"])
        timeframe = metadata.get("timeframe", "1d")
        cutoff = cutoff_at(metadata, as_of or datetime.now(ZoneInfo("Asia/Shanghai")))
        beginning = date.fromisoformat(metadata["period_start"])
        if beginning > cutoff.date():
            raise ValueError("指定区间尚无已结束的K线。")
        start = beginning - timedelta(days=1600 if timeframe == "1w" else 400)
        discarded = 0
        query = {
            "symbol": code,
            "timeframe": timeframe,
            "start": start.isoformat(),
            "cutoff": cutoff.isoformat(),
        }
        if timeframe == "1d":
            raw, url, retrieved_at, cache_hit = cached_fetch(
                root, query, lambda: fetch_daily(code, start, cutoff.date())
            )
            frame = parse_daily(raw, code, start, cutoff.date())
        else:
            raw, url, retrieved_at, cache_hit = cached_fetch(
                root, query, lambda: fetch_period(code, start, cutoff, timeframe)
            )
            frame, discarded = parse_period(raw, code, start, cutoff, timeframe)
        if frame["timestamp"][0].astimezone(ZONE).date() > beginning + timedelta(
            days=14
        ):
            raise ValueError("供应商窗口未覆盖截图起始区间，不能替换为其他周期。")
        if review_origin is not None:
            if review_origin.tzinfo is None or review_reference is None:
                raise ValueError("复评需要原始完整时点和参考价格。")
            original = frame.filter(pl.col("timestamp") == review_origin)
            if original.height != 1:
                raise ValueError("当前行情未覆盖原报告时点，无法复核持有根数。")
            from decimal import Decimal

            if Decimal(str(original["close"][0])) != Decimal(review_reference):
                raise ValueError("原报告时点行情已修订，暂停精确参考。")
            if metadata.get("purchase_price"):
                metadata = {
                    **metadata,
                    "held_bars": metadata.get("held_bars", 0)
                    + frame.filter(pl.col("timestamp") > review_origin).height,
                }
        evaluated = evaluate(frame, beginning, metadata, initial_levels)
        directory = local_root(root)
        checksum = hashlib.sha256(raw).hexdigest()
        path = directory / (checksum + ".json")
        if path.is_symlink():
            raise ValueError("行情归档路径异常。")
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError("行情归档摘要冲突。")
        else:
            with path.open("xb") as stream:
                stream.write(raw)
        result.update(
            status="AVAILABLE",
            core_source_sha256=source_identity(),
            **evaluated,
            source={
                "provider": "TENCENT_PUBLIC_HTTPS",
                "url": url,
                "symbol": code,
                "retrieved_at": retrieved_at,
                "cache_hit": cache_hit,
                "raw_sha256": checksum,
                "bars_sha256": logical_content_hash(frame),
                "adjustment": "RAW_UNADJUSTED",
                "first": bar_label(frame["timestamp"][0], timeframe),
                "last": bar_label(frame["timestamp"][-1], timeframe),
                "last_timestamp": frame["timestamp"][-1].isoformat(),
                "bars": frame.height,
                "cutoff": cutoff.date().isoformat(),
                "cutoff_at": cutoff.isoformat(),
                "timeframe": timeframe,
                "discarded_outside_cutoff": discarded,
                "artifact": path.name,
            },
        )
        result["quality"] = {
            "bars": frame.height,
            "minimum_synthesis_bars": 80,
            "last_bar": result["source"]["last"],
            "age_calendar_days": (
                cutoff.date() - frame["timestamp"][-1].astimezone(ZONE).date()
            ).days,
            "volume_available": "VOLUME_UNAVAILABLE"
            not in result["synthesis"]["risk_flags"],
            "trading_calendar_verified": False,
            "corporate_actions_verified": False,
        }
        contexts = []
        if metadata.get("include_context", True):
            kinds = {"60m": ("1d", "1w"), "1d": ("1w",), "1w": ()}[timeframe]
            for kind in kinds:
                context_meta = {
                    **metadata,
                    "timeframe": kind,
                    "period_start": (
                        cutoff.date() - timedelta(days=366 if kind == "1w" else 93)
                    ).isoformat(),
                    "horizon_bars": 1,
                    "include_context": False,
                    "purchase_price": None,
                    "held_bars": 0,
                }
                context = analyze(context_meta, root, as_of=cutoff)
                contexts.append(
                    {
                        "timeframe": kind,
                        "status": context["status"],
                        "reason": context["reason"],
                        "source": context.get("source"),
                        "synthesis": context.get("synthesis"),
                    }
                )
        main = result["synthesis"]["direction"]
        relation = context_relation(main, contexts)
        result["cross_timeframe"] = {"status": relation, "items": contexts}
        if timeframe == "1d" and horizon(metadata) == 5:
            from .daily_analysis import enhance

            enhance(
                metadata,
                result,
                frame,
                root,
                fetch_daily,
                parse_daily,
                start,
                cutoff,
                initial_levels,
            )
            return result
        plan = result["trade_plan"]
        reasons = []
        if main != "BULLISH":
            reasons.append("综合证据未形成偏强共识，新增购入保持等待。")
        if relation in {"MISSING", "CONFLICT", "MIXED", "UNCONFIRMED", "NEUTRAL"}:
            reasons.append("较长周期缺失或未同向，暂不确认新增购入。")
        if reasons and plan["entry_action"] == "CONSIDER_ENTRY":
            plan["entry_action"] = "WAIT"
            plan["entry_reasons"] = reasons
            if plan["position_basis"] != "USER_COST":
                plan.update(action="WAIT", reasons=reasons)
        if plan["position_basis"] == "USER_COST" and (
            main == "BEARISH"
            or result["synthesis"]["risk_flags"]
            or relation == "CONFLICT"
        ):
            plan.update(
                action="EXIT_REVIEW",
                position_action="EXIT_REVIEW",
                reasons=plan["reasons"] + ["综合风险或反向证据触发持仓复核。"],
            )
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        IndexError,
        pl.exceptions.PolarsError,
    ) as error:
        result.update(
            status="UNAVAILABLE",
            source=None,
            strategies=[],
            baseline=[],
            trade_plan=None,
            synthesis=None,
        )
        result["reason"] = (
            str(error)[:200]
            if isinstance(error, ValueError)
            else "行情服务不可用或响应不完整，本次只提供截图解读。"
        )
    return result
