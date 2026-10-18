"""Versioned, causal chart research rules and a deterministic conditional plan.

These fixed educational rules are not parameter-selected or execution-qualified.
EMA is seeded at the first close; RSI uses Wilder smoothing after its SMA seed;
ATR is the arithmetic mean of trailing true ranges. No future bars are read.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any

import polars as pl

from ._common import signal_frame
from .bollinger_reversion import BollingerReversionConfig
from .donchian_breakout import DonchianBreakoutConfig
from .models import StrategyConfig, generate_strategy_signals, required_observations
from .moving_average import MovingAverageCrossoverConfig, _single_symbol_bars
from .time_series_momentum import TimeSeriesMomentumConfig

ATR_LIMITS = {"60m": "0.04", "1d": "0.08", "1w": "0.16"}


@dataclass(frozen=True)
class ChartRule:
    id: str
    label: str
    family: str
    parameters: dict[str, Any]
    required_bars: int
    entry_rule: str
    exit_rule: str
    config: StrategyConfig | None = None


def rules() -> tuple[ChartRule, ...]:
    base = (
        (
            "sma-5-20",
            "均线趋势5/20",
            "SMA",
            MovingAverageCrossoverConfig(5, 20),
            {"short_window": 5, "long_window": 20},
            "5周期均价高于20周期均价",
            "5周期均价回落至20周期均价及以下",
        ),
        (
            "sma-20-60",
            "均线趋势20/60",
            "SMA",
            MovingAverageCrossoverConfig(20, 60),
            {"short_window": 20, "long_window": 60},
            "20周期均价高于60周期均价",
            "20周期均价回落至60周期均价及以下",
        ),
        (
            "momentum-20",
            "20周期动量",
            "MOMENTUM",
            TimeSeriesMomentumConfig(20),
            {"lookback": 20, "threshold_pct": "0.0"},
            "收盘价高于20根K线前收盘",
            "20周期价格变化不再为正",
        ),
        (
            "donchian-20-10",
            "通道突破",
            "DONCHIAN",
            DonchianBreakoutConfig(20, 10),
            {"entry_window": 20, "exit_window": 10},
            "收盘突破前20根K线最高价，不含当前K线",
            "收盘跌破前10根K线最低价，不含当前K线",
        ),
        (
            "bollinger-20-2",
            "布林带回归",
            "BOLLINGER",
            BollingerReversionConfig(),
            {"window": 20, "k_pct": "200.0"},
            "收盘触及20周期均价减2倍总体标准差",
            "收盘回到20周期均价及以上",
        ),
    )
    return tuple(
        ChartRule(
            i,
            label,
            family,
            params,
            required_observations(config),
            entry,
            exit_,
            config,
        )
        for i, label, family, config, params, entry, exit_ in base
    ) + (
        ChartRule(
            "macd-12-26-9",
            "MACD趋势",
            "MACD",
            {"fast": 12, "slow": 26, "signal": 9},
            35,
            "DIF高于DEA且DIF为正",
            "DIF不再高于DEA或DIF不再为正",
        ),
        ChartRule(
            "rsi-14-30-50",
            "RSI回归",
            "RSI",
            {"window": 14, "entry": 30, "exit": 50},
            15,
            "14周期Wilder RSI不高于30",
            "RSI回升至50及以上",
        ),
        ChartRule(
            "volume-breakout-20",
            "量价突破",
            "VOLUME",
            {"window": 20, "volume_multiple": "1.5"},
            21,
            "突破前20根K线最高价且量能高于前20根均量1.5倍",
            "收盘跌破前10根K线最低价",
        ),
    )


def _series(frame: pl.DataFrame) -> dict[str, list[Decimal]]:
    closes = [Decimal(str(x)) for x in frame["close"]]
    fast, slow, dea = closes[0], closes[0], Decimal(0)
    difs, deas, rsis, trs = [], [], [], []
    gain, loss = Decimal(0), Decimal(0)
    for i, close in enumerate(closes):
        fast += Decimal(2) / 13 * (close - fast)
        slow += Decimal(2) / 27 * (close - slow)
        dif = fast - slow
        dea += Decimal(2) / 10 * (dif - dea)
        difs.append(dif)
        deas.append(dea)
        delta = close - closes[i - 1] if i else Decimal(0)
        up, down = max(delta, Decimal(0)), max(-delta, Decimal(0))
        if 0 < i <= 14:
            gain += up / 14
            loss += down / 14
        elif i > 14:
            gain = (gain * 13 + up) / 14
            loss = (loss * 13 + down) / 14
        rsis.append(
            Decimal(50)
            if gain == loss == 0
            else Decimal(100)
            if loss == 0
            else 100 - 100 / (1 + gain / loss)
        )
        high, low = Decimal(str(frame["high"][i])), Decimal(str(frame["low"][i]))
        previous = closes[max(0, i - 1)]
        trs.append(max(high - low, abs(high - previous), abs(low - previous)))
    return {"close": closes, "dif": difs, "dea": deas, "rsi": rsis, "tr": trs}


def generate_chart_signals(bars: pl.DataFrame, rule: ChartRule) -> pl.DataFrame:
    if rule not in rules():
        raise ValueError("declared chart rule required")
    if rule.config is not None:
        return generate_strategy_signals(bars, rule.config)
    frame = _single_symbol_bars(bars)
    held = False
    values = []
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        series = _series(frame)
        for i, close in enumerate(series["close"]):
            if i + 1 >= rule.required_bars:
                if rule.family == "MACD":
                    held = series["dif"][i] > series["dea"][i] and series["dif"][i] > 0
                elif rule.family == "RSI":
                    if held and series["rsi"][i] >= 50:
                        held = False
                    elif not held and series["rsi"][i] <= 30:
                        held = True
                else:
                    highs = [Decimal(str(x)) for x in frame["high"][i - 20 : i]]
                    lows = [Decimal(str(x)) for x in frame["low"][i - 10 : i]]
                    average = (
                        sum(
                            (Decimal(str(x)) for x in frame["volume"][i - 20 : i]),
                            Decimal(0),
                        )
                        / 20
                    )
                    if held and close < min(lows):
                        held = False
                    elif (
                        not held
                        and average > 0
                        and close > max(highs)
                        and Decimal(str(frame["volume"][i])) >= average * Decimal("1.5")
                    ):
                        held = True
            values.append(float(held))
    return signal_frame(frame, values)


def legacy_conditional_plan(
    bars: pl.DataFrame,
    *,
    timeframe: str,
    horizon_bars: int,
    purchase_price: str | None = None,
    held_bars: int = 0,
    _atr_limit: Decimal = Decimal("0.08"),
) -> dict[str, Any]:
    frame = _single_symbol_bars(bars)
    if (
        timeframe not in ("60m", "1d", "1w")
        or type(horizon_bars) is not int
        or not 1 <= horizon_bars <= 20
    ):
        raise ValueError("bounded timeframe/horizon required")
    if horizon_bars not in {"60m": (1, 4), "1d": (1, 5, 20), "1w": (1, 4)}[timeframe]:
        raise ValueError("timeframe-specific horizon required")
    if type(held_bars) is not int or not 0 <= held_bars <= 10000:
        raise ValueError("bounded held bars required")
    if purchase_price is not None:
        if not isinstance(purchase_price, str):
            raise TypeError("purchase price must be decimal text")
        cost = Decimal(purchase_price)
        if not cost.is_finite() or cost <= 0:
            raise ValueError("positive finite purchase price required")
    if held_bars and purchase_price is None:
        raise ValueError("held bars require purchase price")
    result: dict[str, Any] = {
        "version": "conditional-entry-exit-v1",
        "action": "UNAVAILABLE",
        "basis": "固定规则的研究计划，不是个性化适当性评估或最佳买卖时点预测。",
        "reasons": [],
        "indicators": {},
        "levels": None,
        "exit_rules": [],
        "max_observation_bars": horizon_bars,
        "held_bars": held_bars,
        "remaining_observation_bars": max(0, horizon_bars - held_bars),
        "position_basis": "USER_COST" if purchase_price else "HYPOTHETICAL_REFERENCE",
        "timeframe": timeframe,
        "execution_constraint": "普通A股买入当日不能卖出；ETF回转规则未核验时按T+1保守处理。触发退出不保证可成交。",
        "trading_rules_source": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml",
    }
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        series = _series(frame)
        close = series["close"][-1]
        if frame.height < 60:
            result["reasons"] = ["不足60根完整K线，不生成购入或卖出价位。"]
            return result
        sma20 = sum(series["close"][-20:], Decimal(0)) / 20
        sma60 = sum(series["close"][-60:], Decimal(0)) / 60
        atr = sum(series["tr"][-14:], Decimal(0)) / 14
        volume_mean = (
            sum((Decimal(str(x)) for x in frame["volume"][-21:-1]), Decimal(0)) / 20
        )
        ratio = (
            Decimal(str(frame["volume"][-1])) / volume_mean
            if volume_mean
            else Decimal(0)
        )
        momentum = close / series["close"][-21] - 1

        def rounded(value: Decimal) -> str:
            return format(value, ".4f")

        result["indicators"] = {
            k: rounded(v)
            for k, v in {
                "reference_close": close,
                "sma20": sma20,
                "sma60": sma60,
                "atr14_sma": atr,
                "rsi14_wilder": series["rsi"][-1],
                "macd_dif": series["dif"][-1],
                "macd_dea": series["dea"][-1],
                "volume_ratio_prior20": ratio,
            }.items()
        }
        if (
            close < Decimal("0.0001")
            or atr <= 0
            or volume_mean <= 0
            or atr / close >= _atr_limit
        ):
            result.update(
                action="AVOID",
                reasons=["波动或量能风险过滤未通过，暂不购入；不补造止损止盈价。"],
            )
            return result
        trend = close > sma20 > sma60
        macd = series["dif"][-1] > series["dea"][-1] and series["dif"][-1] > 0
        bounded = series["rsi"][-1] < 70 and (close - sma20) / atr <= 2
        if close < sma60 and momentum <= 0:
            result.update(
                action="AVOID",
                reasons=["收盘低于60周期均线且20周期价格变化不为正，暂不购入。"],
            )
        elif trend and macd and bounded and ratio >= 1:
            result.update(
                action="CONSIDER_ENTRY",
                reasons=[
                    "趋势、MACD、不过度追高及量能过滤均通过；下一根完整K线仍满足时才考虑购入。"
                ],
            )
        else:
            result.update(
                action="WAIT",
                reasons=[
                    "趋势、MACD、追高或量能确认至少一项未满足，暂不购入；等待完整K线重新确认。"
                ],
            )
        reference = Decimal(purchase_price) if purchase_price else close
        if not reference.is_finite() or reference <= 0:
            raise ValueError("positive finite purchase price required")
        stop, target = reference - 2 * atr, reference + 3 * atr
        if stop <= 0:
            result.update(
                action="AVOID",
                reasons=["基于成本的估算止损价不为正，无法构建有效退出计划。"],
            )
            return result
        result["levels"] = {
            "reference": rounded(reference),
            "stop_reference": rounded(stop),
            "take_profit_reference": rounded(target),
            "rule": "参考价减2倍ATR与加3倍ATR；ATR为14周期TR简单均值；不是预测目标或保证成交价。",
        }
        result["exit_rules"] = [
            "已持仓时，触及估算止损参考线应优先复核退出；跳空、停牌或跌停可能无法按该价卖出。",
            "到达估算止盈参考线时评估兑现；这不是模型预测会到达的价格。",
            "完整K线收盘跌破20周期均线时重新评估持有逻辑。",
            f"自实际购入后最多观察{horizon_bars}根所选周期完整K线，然后强制复评；不是承诺届时盈利或机械卖出日期。",
        ]
        if purchase_price:
            exits = (
                close <= stop
                or close >= target
                or held_bars >= horizon_bars
                or close < sma20
            )
            result["action"] = "EXIT_REVIEW" if exits else "HOLD_REVIEW"
            result["reasons"] = [
                "成本/时间/趋势退出条件已触发，评估退出且遵守可卖规则。"
                if exits
                else "尚未触发退出复核条件，按参考线及观察周期继续检查；不自动下单。"
            ]
    return result


def conditional_plan(
    bars: pl.DataFrame,
    *,
    timeframe: str,
    horizon_bars: int,
    purchase_price: str | None = None,
    held_bars: int = 0,
    initial_levels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Separate entry eligibility from position review, retaining the old benchmark."""
    if timeframe not in ATR_LIMITS:
        raise ValueError("declared timeframe required")
    limit = Decimal(ATR_LIMITS[timeframe])
    entry = legacy_conditional_plan(
        bars, timeframe=timeframe, horizon_bars=horizon_bars, _atr_limit=limit
    )
    result = legacy_conditional_plan(
        bars,
        timeframe=timeframe,
        horizon_bars=horizon_bars,
        purchase_price=purchase_price,
        held_bars=held_bars,
        _atr_limit=limit,
    )
    result.update(
        version="conditional-entry-exit-v2",
        entry_action=entry["action"],
        entry_reasons=entry["reasons"],
        position_action="NONE" if purchase_price is None else result["action"],
        levels_scope="LINKED_INITIAL" if initial_levels else "THIS_REPORT_ONLY",
        position_note=(
            "沿用明确关联的初始参考线；不会随最新ATR增加而放宽。"
            if initial_levels
            else "本次报告独立估算；未重建真实买入时ATR，不代表持续跟踪的止损线。"
        ),
    )
    if initial_levels is not None:
        if purchase_price is None:
            raise ValueError("linked levels require purchase price")
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            reference, stop, target = (
                Decimal(initial_levels[k])
                for k in ("reference", "stop_reference", "take_profit_reference")
            )
            if not all(x.is_finite() for x in (reference, stop, target)) or not (
                0 < stop < reference < target and reference == Decimal(purchase_price)
            ):
                raise ValueError("invalid linked initial levels")
        result["levels"] = dict(initial_levels)
    if purchase_price is not None:
        exits = result["action"] not in {"HOLD_REVIEW", "EXIT_REVIEW"}
        reasons = list(result["reasons"])
        if exits:
            reasons.append(
                "风险或数据门槛未通过；已持仓仍须复核退出，不能只处理新增购入。"
            )
        if initial_levels is not None:
            close = Decimal(str(bars["close"][-1]))
            sma20 = result["indicators"].get("sma20")
            exits = (
                entry["action"] in {"AVOID", "UNAVAILABLE"}
                or close <= stop
                or close >= target
                or (sma20 is not None and close < Decimal(sma20))
            )
        if held_bars >= horizon_bars:
            exits = True
            reasons.append("已达到最长观察根数，需要复评持仓。")
        if initial_levels is None and result["action"] == "EXIT_REVIEW":
            exits = True
        result.update(
            action="EXIT_REVIEW" if exits else "HOLD_REVIEW",
            position_action="EXIT_REVIEW" if exits else "HOLD_REVIEW",
            reasons=reasons,
        )
        if not result["exit_rules"]:
            result["exit_rules"] = [
                "风险、数据或参考线不完整，先核对持仓风险；不能推断可按指定价格成交。",
                f"最多观察{horizon_bars}根完整K线后强制复评；买入当日仍遵守T+1。",
            ]
    return result
