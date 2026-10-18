"""Frozen daily-five candidates and plans shared by reports and research replay."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .chart_decision import GROUP_IDS, context_relation, decide
from .chart_quality import daily_quality
from .chart_suite import _series, conditional_plan
from .chart_synthesis import synthesize
from .moving_average import _single_symbol_bars

VERSION = "daily-five-research-v1"
POLICIES = ("BASELINE_V2", "REGIME_ROUTER", "REGIME_PERSISTENT")
EXIT_POLICIES = ("ATR_TREND_TIME", "FIXED_TIME", "ATR_TIME")


def prepare_inputs(
    bars: pl.DataFrame,
    benchmark: pl.DataFrame | None,
    *,
    base: dict[str, Any] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    frame = _single_symbol_bars(bars)
    quality = daily_quality(frame, as_of)
    synthesis = deepcopy(base if base is not None else synthesize(frame, "1d"))
    features: dict[str, Any] = {}
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        series = _series(frame)
        closes = series["close"]
        close = closes[-1]
        atr = sum(series["tr"][-14:], Decimal(0)) / min(14, len(closes))
        if frame.height >= 80:
            sma20 = sum(closes[-20:], Decimal(0)) / 20
            recent_atr = [
                sum(series["tr"][i - 13 : i + 1], Decimal(0)) / 14 / closes[i]
                for i in range(max(13, len(closes) - 120), len(closes))
            ]
            ratio = atr / close
            features = {
                "close": str(close),
                "atr14": str(atr),
                "sma20": str(sma20),
                "momentum5_atr": str((close - closes[-6]) / atr) if atr > 0 else None,
                "rsi14": str(series["rsi"][-1]),
                "extension20_atr": str((close - sma20) / atr) if atr > 0 else None,
                "volatility_percentile120": str(
                    Decimal(sum(x <= ratio for x in recent_atr)) / len(recent_atr)
                ),
                "breakout_distance20_atr": str(
                    (close - max(Decimal(str(x)) for x in frame["high"][-21:-1])) / atr
                )
                if atr > 0
                else None,
                "volume_confirmations_last3": sum(
                    Decimal(str(frame["volume"][i]))
                    >= sum(
                        (Decimal(str(x)) for x in frame["volume"][i - 20 : i]),
                        Decimal(0),
                    )
                    / 20
                    for i in range(len(closes) - 3, len(closes))
                ),
                "relative_return5": None,
                "relative_return20": None,
            }
            if benchmark is not None:
                reference = _single_symbol_bars(benchmark)
                zone = ZoneInfo("Asia/Shanghai")
                prices = {
                    at.astimezone(zone).date(): Decimal(str(value))
                    for at, value in reference.select("timestamp", "close").iter_rows()
                }
                for horizon in (5, 20):
                    last = frame["timestamp"][-1].astimezone(zone).date()
                    first = frame["timestamp"][-horizon - 1].astimezone(zone).date()
                    if last in prices and first in prices:
                        features[f"relative_return{horizon}"] = str(
                            close / closes[-horizon - 1] - prices[last] / prices[first]
                        )
    return {
        "schema_version": VERSION,
        "target": {"timeframe": "1d", "horizon_bars": 5},
        "base": synthesis,
        "features": features,
        "quality": quality,
        "benchmark_scope": "RAW_ETF_MARKET_PROXY_NOT_TOTAL_RETURN_INDEX",
        "legacy_entry": conditional_plan(frame, timeframe="1d", horizon_bars=5),
        "as_of_bar": frame["timestamp"][-1].isoformat(),
    }


def _router(
    inputs: dict[str, Any], active_groups: tuple[str, ...] = GROUP_IDS
) -> tuple[str, str]:
    base, features = inputs["base"], inputs["features"]
    by_id = {x["id"]: x for x in base["groups"]}
    evidence = {x["family"]: x for x in base["evidence"]}
    breakout = by_id["BREAKOUT"]
    if (
        "BREAKOUT" in active_groups
        and breakout["direction"] in {"BULLISH", "BEARISH"}
        and not breakout["internal_conflict"]
        and evidence["VOLUME"]["direction"] == breakout["direction"]
    ):
        return str(breakout["direction"]), "BREAKOUT"
    if base["market_state"] == "TREND":
        if "TREND" not in active_groups:
            return "UNCLEAR", "PRIMARY_DISABLED"
        trend = by_id["TREND"]
        if trend["internal_conflict"] or trend["direction"] not in {
            "BULLISH",
            "BEARISH",
        }:
            return "UNCLEAR", "TREND"
        momentum = features.get("momentum5_atr")
        sign = 1 if trend["direction"] == "BULLISH" else -1
        if momentum is None or Decimal(momentum) * sign < Decimal("0.5"):
            return "UNCLEAR", "TREND"
        return str(trend["direction"]), "TREND"
    if base["market_state"] == "RANGE":
        if "REVERSION" not in active_groups:
            return "UNCLEAR", "PRIMARY_DISABLED"
        group = by_id["REVERSION"]
        return (
            "UNCLEAR" if group["internal_conflict"] else str(group["direction"])
        ), "REVERSION"
    return "UNCLEAR", "NONE"


def prediction(
    inputs: dict[str, Any],
    contexts: list[dict[str, Any]],
    policy: str,
    previous: dict[str, Any] | None = None,
    active_groups: tuple[str, ...] = GROUP_IDS,
) -> dict[str, Any]:
    if policy not in POLICIES:
        raise ValueError("predeclared daily-five candidate required")
    base = inputs["base"]
    canonical = decide(
        base["groups"], base["market_state"], base["risk_flags"], active_groups
    )
    flags = list(dict.fromkeys(base["risk_flags"] + inputs["quality"]["hard_flags"]))
    reason: str | None
    if flags:
        direction, primary, reason = "UNCLEAR", "NONE", "RISK_OR_DATA_FILTER"
    elif policy == "BASELINE_V2":
        decision = decide(base["groups"], base["market_state"], flags, active_groups)
        direction, primary, reason = (
            decision["direction"],
            "CONSENSUS",
            decision["abstention_reason"],
        )
    else:
        direction, primary = _router(inputs, active_groups)
        reason = "NO_HORIZON_CONFIRMATION" if direction == "UNCLEAR" else None
        if (
            policy == "REGIME_PERSISTENT"
            and direction in {"BULLISH", "BEARISH"}
            and (
                previous is None
                or previous["quality"]["hard_flags"]
                or previous["base"]["risk_flags"]
                or _router(previous, active_groups) != (direction, primary)
                or previous["base"]["market_state"] != base["market_state"]
                or previous["as_of_bar"] >= inputs["as_of_bar"]
            )
        ):
            direction, reason = "UNCLEAR", "SECOND_CLOSED_BAR_REQUIRED"
    relation = context_relation(direction, contexts)
    return {
        "version": VERSION,
        "policy": policy,
        "timeframe": "1d",
        "horizon_bars": 5,
        "direction": direction,
        "primary_group": primary,
        "abstention_reason": reason,
        "risk_flags": flags,
        "context_relation": relation,
        "forecast_validated": False,
        "horizon_specific": policy != "BASELINE_V2",
        "active_groups": canonical["active_groups"],
    }


def report_plan(
    inputs: dict[str, Any],
    decision: dict[str, Any],
    *,
    max_holding_bars: int = 5,
    purchase_price: str | None = None,
    held_bars: int = 0,
    initial_levels: dict[str, Any] | None = None,
    exit_policy: str = "ATR_TREND_TIME",
) -> dict[str, Any]:
    if (
        type(max_holding_bars) is not int
        or max_holding_bars not in (3, 5, 10, 20)
        or type(held_bars) is not int
        or held_bars < 0
        or exit_policy not in EXIT_POLICIES
    ):
        raise ValueError("declared holding, review and exit policy required")
    if purchase_price is None and (held_bars or initial_levels is not None):
        raise ValueError("position review requires a cost")
    plan = deepcopy(inputs["legacy_entry"])
    plan.update(
        version="conditional-entry-exit-v3",
        forecast_horizon_bars=5,
        max_observation_bars=max_holding_bars,
        held_bars=held_bars,
        remaining_observation_bars=max(0, max_holding_bars - held_bars),
        review_interval_bars=1,
        position_basis="USER_COST" if purchase_price else "HYPOTHETICAL_REFERENCE",
        levels_scope="LINKED_INITIAL" if initial_levels else "THIS_REPORT_ONLY",
        candidate_policy=decision["policy"],
        exit_policy=exit_policy,
        levels=None,
        entry_action="UNAVAILABLE",
        position_action="NONE",
        as_of_bar=inputs["as_of_bar"],
    )
    entry = "WAIT"
    reasons = []
    features = inputs["features"]
    if decision["risk_flags"] or not features:
        entry = "UNAVAILABLE"
        reasons.append("交易日、历史、量能或波动检查未通过。")
    elif decision["direction"] != "BULLISH":
        reasons.append("日线5根证据尚未形成偏强确认，暂不新增购入。")
    elif decision["context_relation"] in {"MISSING", "CONFLICT"}:
        reasons.append("较长周期行情缺失或明确反向，暂不新增购入。")
    elif decision["policy"] == "BASELINE_V2" and (
        inputs["legacy_entry"]["entry_action"] != "CONSIDER_ENTRY"
        or decision["context_relation"] not in {"ALIGNED", "SINGLE_PERIOD"}
    ):
        reasons.append("原保守入场条件或较长周期同向确认未通过。")
    elif Decimal(features["rsi14"]) >= 70 or Decimal(features["extension20_atr"]) > 2:
        reasons.append("已触发追高过滤，等待下一根完整K线复评。")
    else:
        entry = "CONSIDER_ENTRY"
        reasons.append(
            "所声明日线5根规则及风险过滤通过；次根开盘仍需检查跳空与可交易条件。"
        )
    plan.update(
        action=entry, entry_action=entry, entry_reasons=reasons, reasons=reasons
    )
    if features:
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            close, atr, sma20 = (
                Decimal(features[x]) for x in ("close", "atr14", "sma20")
            )
            reference = Decimal(purchase_price) if purchase_price is not None else close
            if not reference.is_finite() or reference <= 0:
                raise ValueError("positive finite cost required")
            stop, target = reference - 2 * atr, reference + 3 * atr
            if initial_levels is not None:
                reference, stop, target = (
                    Decimal(initial_levels[x])
                    for x in ("reference", "stop_reference", "take_profit_reference")
                )
                if reference != Decimal(purchase_price or "0"):
                    raise ValueError("linked cost differs")
            if (
                all(x.is_finite() for x in (reference, stop, target))
                and 0 < stop < reference < target
                and (not decision["risk_flags"] or initial_levels is not None)
            ):
                plan["levels"] = {
                    "reference": format(reference, ".4f"),
                    "stop_reference": format(stop, ".4f"),
                    "take_profit_reference": format(target, ".4f"),
                    "rule": "固定2ATR/3ATR研究参考，非预测目标；关联计划沿用初始参考线。",
                    "entry_primary_group": decision["primary_group"],
                    "entry_mean_reference": format(sma20, ".4f"),
                    "entry_breakout_reference": format(
                        close - Decimal(features["breakout_distance20_atr"]) * atr,
                        ".4f",
                    ),
                }
                if initial_levels is not None:
                    plan["levels"] = dict(initial_levels)
            plan["entry_gap_upper_reference"] = format(close + atr, ".4f")
            if purchase_price is not None:
                exits = (
                    bool(decision["risk_flags"])
                    or not plan["levels"]
                    or decision["context_relation"] == "CONFLICT"
                )
                exits = exits or held_bars >= max_holding_bars
                if exit_policy in {"ATR_TREND_TIME", "ATR_TIME"}:
                    exits = exits or close <= stop or close >= target
                if exit_policy == "ATR_TREND_TIME":
                    group = (plan["levels"] or {}).get("entry_primary_group", "UNKNOWN")
                    if group in {"TREND", "CONSENSUS", "UNKNOWN"}:
                        exits = exits or close < sma20
                    elif group == "REVERSION":
                        exits = exits or close >= Decimal(
                            plan["levels"]["entry_mean_reference"]
                        )
                    elif group == "BREAKOUT":
                        exits = exits or close < Decimal(
                            plan["levels"]["entry_breakout_reference"]
                        )
                    exits = exits or decision["direction"] == "BEARISH"
                plan.update(
                    action="EXIT_REVIEW" if exits else "HOLD_REVIEW",
                    position_action="EXIT_REVIEW" if exits else "HOLD_REVIEW",
                    reasons=[
                        "持仓风险、价格、趋势或时间条件触发复核。"
                        if exits
                        else "尚未触发所声明退出条件，每根完整K线重新复评。"
                    ],
                )
    elif purchase_price is not None:
        plan.update(
            action="EXIT_REVIEW",
            position_action="EXIT_REVIEW",
            reasons=["缺少有效价格资料，优先人工核对持仓风险。"],
        )
    plan["exit_rules"] = [
        "信号后次根开盘尝试执行；超过参考1ATR的向上跳空取消追入。",
        "买入当日保守禁止卖出；停牌、锁定价格和成交量单位未知时不保证成交。",
        f"预测未来5根；最长持仓观察{max_holding_bars}根，每根完成K线复评。",
        "按声明的参考线、趋势与时间政策复核退出；不是保证盈利或最佳卖出日期。",
    ]
    return plan
