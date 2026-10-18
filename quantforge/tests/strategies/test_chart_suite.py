import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import polars as pl
import pytest

from quantforge.data.bars import BAR_SCHEMA
from quantforge.strategies.chart_suite import (
    conditional_plan,
    generate_chart_signals,
    rules,
)


def bars(closes: list[float], volumes: list[float] | None = None) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "symbol": "sh600519",
                "timestamp": datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i),
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": volumes[i] if volumes else 1000.0,
            }
            for i, close in enumerate(closes)
        ],
        schema=BAR_SCHEMA,
    )


@pytest.mark.parametrize("rule", rules(), ids=lambda rule: rule.id)
def test_eight_rules_are_causal_and_do_not_treat_warmup_as_a_computed_entry(rule):
    frame = bars([100 + i * 0.05 + math.sin(i * 0.5) * 2 for i in range(160)])
    full = generate_chart_signals(frame, rule)
    prefix = generate_chart_signals(frame.head(83), rule)
    assert full.head(83).equals(prefix)
    assert set(full["value"].to_list()) <= {0.0, 1.0}
    assert full.head(rule.required_bars - 1)["value"].sum() == 0
    assert len(rules()) == 8 and len({item.family for item in rules()}) == 7


def test_macd_and_wilder_rsi_edge_cases_have_explicit_meaning():
    macd = next(x for x in rules() if x.family == "MACD")
    rsi = next(x for x in rules() if x.family == "RSI")
    up, down, flat = (
        bars([100 + float(i) for i in range(80)]),
        bars([200 - float(i) for i in range(80)]),
        bars([100.0] * 80),
    )
    assert generate_chart_signals(up, macd)["value"][-1] == 1
    assert generate_chart_signals(down, macd)["value"].sum() == 0
    assert generate_chart_signals(up, rsi)["value"].sum() == 0
    assert generate_chart_signals(down, rsi)["value"][14] == 1
    assert generate_chart_signals(flat, rsi)["value"].sum() == 0
    plan = conditional_plan(flat, timeframe="1d", horizon_bars=5)
    assert plan["indicators"]["rsi14_wilder"] == "50.0000"
    assert plan["indicators"]["atr14_sma"] == "2.0000"


def test_volume_rule_excludes_current_high_and_retains_exit_state():
    frame = bars([100.0] * 21 + [104.0, 98.0], [1000.0] * 21 + [2000.0, 1000.0])
    rule = next(x for x in rules() if x.family == "VOLUME")
    assert generate_chart_signals(frame, rule)["value"].tail(3).to_list() == [
        0.0,
        1.0,
        0.0,
    ]


def test_plan_uses_cost_time_and_atr_without_claiming_an_optimal_sale_date():
    frame = bars([100.0] * 80)
    held = conditional_plan(
        frame, timeframe="1d", horizon_bars=5, purchase_price="100", held_bars=2
    )
    assert held["action"] == "HOLD_REVIEW"
    assert held["remaining_observation_bars"] == 3
    assert held["levels"]["stop_reference"] == "96.0000"
    assert held["levels"]["take_profit_reference"] == "106.0000"
    for cost, count in [("110", 0), ("90", 0), ("100", 5)]:
        assert (
            conditional_plan(
                frame,
                timeframe="1d",
                horizon_bars=5,
                purchase_price=cost,
                held_bars=count,
            )["action"]
            == "EXIT_REVIEW"
        )
    assert "T+1" in held["execution_constraint"]
    assert "机械卖出" in held["exit_rules"][-1]


def test_conditional_entry_exists_but_overbought_and_declining_data_are_not_votes():
    frame = bars([100 + i * 0.05 + math.sin(i * 0.5) * 2 for i in range(180)])
    plans = [
        conditional_plan(frame.head(n), timeframe="1d", horizon_bars=5)
        for n in range(120, 160)
    ]
    assert any(x["action"] == "CONSIDER_ENTRY" for x in plans)
    declining = conditional_plan(
        bars([200 - float(i) for i in range(80)]), timeframe="1d", horizon_bars=5
    )
    assert declining["action"] == "AVOID"
    rising = conditional_plan(
        bars([100 + float(i) for i in range(80)]), timeframe="1d", horizon_bars=5
    )
    assert rising["action"] == "WAIT"  # Overbought, despite trend-rule longs.


def test_cold_history_and_invalid_positions_cannot_get_a_purchase_plan():
    assert (
        conditional_plan(bars([100.0] * 10), timeframe="1d", horizon_bars=5)["levels"]
        is None
    )
    for changes in (
        {"purchase_price": "NaN"},
        {"purchase_price": "-1"},
        {"held_bars": 1},
        {"held_bars": True},
        {"horizon_bars": 0},
        {"timeframe": "60m", "horizon_bars": 20},
    ):
        with pytest.raises((ValueError, TypeError)):
            conditional_plan(
                bars([100.0] * 80), **{"timeframe": "1d", "horizon_bars": 5, **changes}
            )


def test_ambient_decimal_precision_does_not_change_signals_or_reference_prices():
    frame = bars([100 + i * 0.05 + math.sin(i * 0.5) * 2 for i in range(90)])
    expected = conditional_plan(frame, timeframe="1d", horizon_bars=5)
    with localcontext() as context:
        context.prec = 3
        assert conditional_plan(frame, timeframe="1d", horizon_bars=5) == expected
        assert Decimal(expected["levels"]["stop_reference"]) > 0
