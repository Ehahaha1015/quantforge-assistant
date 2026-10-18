"""Frozen research evidence aggregation; directions are never long/flat votes."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any

import polars as pl

from .chart_decision import decide
from .chart_suite import ATR_LIMITS, _series, generate_chart_signals, rules
from .moving_average import _single_symbol_bars

VERSION = "chart-synthesis-v3"
PROFILES: dict[str, dict[str, Any]] = {
    "60m": {"id": "hour-v1", "atr_limit": ATR_LIMITS["60m"], "minimum_bars": 80},
    "1d": {"id": "day-v1", "atr_limit": ATR_LIMITS["1d"], "minimum_bars": 80},
    "1w": {"id": "week-v1", "atr_limit": ATR_LIMITS["1w"], "minimum_bars": 80},
}
GROUPS = (
    ("TREND", "趋势与动量", ("SMA", "MOMENTUM", "MACD"), 2),
    ("BREAKOUT", "突破与量能", ("DONCHIAN", "VOLUME"), 1),
    ("REVERSION", "回归与超跌", ("BOLLINGER", "RSI"), 1),
)
DIRECTIONS = {-1: "BEARISH", 0: "RANGE", 1: "BULLISH"}


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def _band(values: list[Decimal]) -> tuple[Decimal, Decimal]:
    average = _mean(values)
    width = _mean([(x - average) ** 2 for x in values]).sqrt() * 2
    return average - width, average + width


def _sign(value: Decimal, threshold: Decimal) -> int:
    return 1 if value > threshold else -1 if value < -threshold else 0


def _adx(highs: list[Decimal], lows: list[Decimal], trs: list[Decimal]) -> Decimal:
    """Wilder14 TR/DM seed uses the first fourteen price changes, not bar zero."""
    plus, minus, true_range = Decimal(0), Decimal(0), Decimal(0)
    dx: list[Decimal] = []
    adx = Decimal(0)
    for i in range(1, len(highs)):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        p = max(up, Decimal(0)) if up > down else Decimal(0)
        m = max(down, Decimal(0)) if down > up else Decimal(0)
        if i <= 14:
            plus += p
            minus += m
            true_range += trs[i]
        else:
            plus = plus - plus / 14 + p
            minus = minus - minus / 14 + m
            true_range = true_range - true_range / 14 + trs[i]
        if i >= 14:
            denominator = plus + minus
            dx.append(
                100 * abs(plus - minus) / denominator if denominator else Decimal(0)
            )
            if len(dx) == 14:
                adx = _mean(dx)
            elif len(dx) > 14:
                adx = (adx * 13 + dx[-1]) / 14
    return adx


def aggregate(evidence: list[dict[str, Any]], risk_flags: list[str]) -> dict[str, Any]:
    """One contribution per group; missing or conflicting evidence is explicit."""
    declared = {x.id: x.family for x in rules()}
    if len(evidence) != 8 or {x["id"] for x in evidence} != set(declared):
        raise ValueError("all eight unique declared rules are required")
    if any(
        x["direction"] not in {*DIRECTIONS.values(), "UNCLEAR"}
        or x["status"] not in {"COMPUTED", "INSUFFICIENT_HISTORY"}
        for x in evidence
    ):
        raise ValueError("declared evidence status/direction required")
    groups = []
    for group_id, label, families, minimum in GROUPS:
        members = [x for x in evidence if declared[x["id"]] in families]
        up = [x["id"] for x in members if x["direction"] == "BULLISH"]
        down = [x["id"] for x in members if x["direction"] == "BEARISH"]
        available = all(x["status"] == "COMPUTED" for x in members)
        direction = (
            "UNCLEAR"
            if not available
            else (
                "BULLISH"
                if len(up) >= minimum and not down
                else "BEARISH"
                if len(down) >= minimum and not up
                else "RANGE"
            )
        )
        groups.append(
            {
                "id": group_id,
                "label": label,
                "direction": direction,
                "members": [x["id"] for x in members],
                "supporting": up,
                "opposing": down,
                "internal_conflict": bool(up and down),
            }
        )
    up_groups = [x["id"] for x in groups if x["direction"] == "BULLISH"]
    down_groups = [x["id"] for x in groups if x["direction"] == "BEARISH"]
    computed = sum(x["status"] == "COMPUTED" for x in evidence)
    conflict = bool(up_groups and down_groups) or any(
        x["internal_conflict"] for x in groups
    )
    direction = (
        "UNCLEAR"
        if computed != 8 or risk_flags
        else (
            "RANGE"
            if conflict
            else "BULLISH"
            if len(up_groups) >= 2 and not down_groups
            else "BEARISH"
            if len(down_groups) >= 2 and not up_groups
            else "RANGE"
        )
    )
    return {
        "direction": direction,
        "groups": groups,
        "supporting_groups": up_groups,
        "opposing_groups": down_groups,
        "conflict": conflict,
        "coverage": {"computed": computed, "total": 8},
        "agreement": "INSUFFICIENT"
        if computed != 8
        else "CONFLICT"
        if conflict
        else "DIRECTIONAL"
        if direction in {"BULLISH", "BEARISH"}
        else "MIXED",
    }


def synthesize(
    bars: pl.DataFrame,
    timeframe: str,
    states: Mapping[str, tuple[int | None, int | None]] | None = None,
) -> dict[str, Any]:
    if timeframe not in PROFILES:
        raise ValueError("declared timeframe required")
    frame = _single_symbol_bars(bars)
    profile = PROFILES[timeframe]
    evidence: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "schema_version": VERSION,
        "profile": profile["id"],
        "timeframe": timeframe,
        "as_of_bar": frame["timestamp"][-1].isoformat(),
        "risk_flags": [],
        "risk_level": "UNKNOWN",
        "market_state": "INSUFFICIENT",
        "indicators": {},
        "evidence": evidence,
        "forecast_validated": False,
        "basis": "固定研究规则综合倾向；证据一致性和覆盖率不是上涨概率。",
    }
    if frame.height < profile["minimum_bars"]:
        for rule in rules():
            evidence.append(
                {
                    "id": rule.id,
                    "label": rule.label,
                    "family": rule.family,
                    "status": "INSUFFICIENT_HISTORY",
                    "direction": "UNCLEAR",
                    "research_state": None,
                    "transition": "UNAVAILABLE",
                    "reason": "综合规则需要至少80根完整K线。",
                }
            )
        result.update(aggregate(evidence, ["INSUFFICIENT_HISTORY"]))
        result["risk_flags"] = ["INSUFFICIENT_HISTORY"]
        return result
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        series = _series(frame)
        closes = series["close"]
        high = [Decimal(str(x)) for x in frame["high"]]
        low = [Decimal(str(x)) for x in frame["low"]]
        volume = [Decimal(str(x)) for x in frame["volume"]]
        close, previous = closes[-1], closes[-2]
        atr = _mean(series["tr"][-14:])
        sma20 = _mean(closes[-20:])
        previous_sma20 = _mean(closes[-23:-3])
        volume_mean = _mean(volume[-21:-1])
        ratio = volume[-1] / volume_mean if volume_mean else Decimal(0)
        lower, upper = _band(closes[-20:])
        previous_lower, previous_upper = _band(closes[-21:-1])
        adx = _adx(high, low, series["tr"])
        normalized_atr = atr / close
        flags = result["risk_flags"]
        if atr <= 0:
            flags.append("ZERO_VOLATILITY")
        if volume_mean <= 0 or volume[-1] <= 0:
            flags.append("VOLUME_UNAVAILABLE")
        if normalized_atr >= Decimal(str(profile["atr_limit"])):
            flags.append("HIGH_VOLATILITY")
        prior_atr = _mean(series["tr"][-15:-1])
        if abs(close / previous - 1) > max(Decimal("0.10"), 6 * prior_atr / previous):
            flags.append("POSSIBLE_PRICE_DISCONTINUITY")
        scale = atr if atr > 0 else Decimal(1)
        slope = (sma20 - previous_sma20) / scale
        result.update(
            risk_level="HIGH" if flags else "NORMAL",
            market_state="HIGH_VOLATILITY"
            if "HIGH_VOLATILITY" in flags
            else "TREND"
            if adx >= 25 and abs(slope) >= Decimal("0.15")
            else "RANGE",
            indicators={
                k: format(v, ".8f")
                for k, v in {
                    "atr14": atr,
                    "atr_ratio": normalized_atr,
                    "adx14_wilder": adx,
                    "sma20_slope_3_atr": slope,
                    "bollinger_width_ratio": (upper - lower) / close,
                    "volume_ratio_prior20": ratio,
                    "reference_close": close,
                }.items()
            },
        )
        result["indicators"].update(reference_close=str(close), atr14=str(atr))
        for rule in rules():
            latest: int | None
            prior: int | None
            if states is None:
                values = generate_chart_signals(frame, rule)["value"]
                latest, prior = int(values[-1]), int(values[-2])
            else:
                latest, prior = states[rule.id]
                if (
                    type(latest) is not int
                    or type(prior) is not int
                    or latest not in (0, 1)
                    or prior not in (0, 1)
                ):
                    raise ValueError("computed states must be binary integers")
            sign = 0
            reason = "方向条件未确认；研究空仓不等于看跌。"
            if rule.family == "SMA":
                short, long = (
                    int(rule.parameters["short_window"]),
                    int(rule.parameters["long_window"]),
                )
                sign = _sign(
                    (_mean(closes[-short:]) - _mean(closes[-long:])) / scale,
                    Decimal("0.15"),
                )
                reason = "均线价差按ATR归一化，阈值0.15ATR。"
            elif rule.family == "MOMENTUM":
                sign = _sign((close - closes[-21]) / scale, Decimal("0.5"))
                reason = "20根价格变化超过0.5ATR才作为方向证据。"
            elif rule.family == "MACD":
                sign = _sign(
                    (series["dif"][-1] - series["dea"][-1]) / scale, Decimal("0.03")
                )
                if sign * series["dif"][-1] <= 0:
                    sign = 0
                reason = "DIF方向与超过0.03ATR的柱差方向一致。"
            elif rule.family in {"DONCHIAN", "VOLUME"}:
                if close > max(high[-21:-1]) + scale * Decimal("0.1"):
                    sign = 1
                elif close < min(low[-11:-1]) - scale * Decimal("0.1"):
                    sign = -1
                if rule.family == "VOLUME" and ratio < Decimal("1.5"):
                    sign = 0
                reason = (
                    "排除当前K线的通道边界，突破幅度0.1ATR；量价证据另需1.5倍均量。"
                )
            elif rule.family == "RSI":
                rsi, prev_rsi = series["rsi"][-1], series["rsi"][-2]
                if prev_rsi <= 30 and rsi - prev_rsi >= 3 and close > previous:
                    sign = 1
                elif prev_rsi >= 70 and prev_rsi - rsi >= 3 and close < previous:
                    sign = -1
                reason = "RSI越过超跌/超买后的变化需价格确认；仅超卖不判断上涨。"
            elif rule.family == "BOLLINGER":
                if previous < previous_lower and close >= lower and close > previous:
                    sign = 1
                elif previous > previous_upper and close <= upper and close < previous:
                    sign = -1
                reason = "前根越过布林带、当前回到带内并由价格方向确认。"
            evidence.append(
                {
                    "id": rule.id,
                    "label": rule.label,
                    "family": rule.family,
                    "status": "COMPUTED",
                    "direction": DIRECTIONS[sign],
                    "research_state": latest,
                    "transition": "ENTRY"
                    if latest == 1 and prior == 0
                    else "EXIT"
                    if latest == 0 and prior == 1
                    else "CONTINUING"
                    if latest == 1
                    else "WAITING",
                    "reason": reason,
                }
            )
    result.update(aggregate(evidence, result["risk_flags"]))
    result.update(
        decide(result["groups"], result["market_state"], result["risk_flags"])
    )
    return result
