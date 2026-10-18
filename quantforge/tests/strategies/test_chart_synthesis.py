import copy
import math
from decimal import localcontext

import pytest
from test_chart_suite import bars

from quantforge.strategies.chart_suite import (
    conditional_plan,
    legacy_conditional_plan,
    rules,
)
from quantforge.strategies.chart_synthesis import aggregate, synthesize


def evidence(directions=None):
    directions = directions or {}
    return [
        {"id": x.id, "status": "COMPUTED", "direction": directions.get(x.id, "RANGE")}
        for x in rules()
    ]


def test_grouping_does_not_count_four_trend_rules_as_four_independent_votes():
    values = evidence(
        {x.id: "BULLISH" for x in rules() if x.family in {"SMA", "MOMENTUM", "MACD"}}
    )
    result = aggregate(values, [])
    assert result["direction"] == "RANGE"
    assert result["supporting_groups"] == ["TREND"]
    values[3]["direction"] = "BULLISH"
    assert aggregate(values, [])["direction"] == "BULLISH"
    values[6]["direction"] = "BEARISH"
    assert aggregate(values, [])["direction"] == "RANGE"
    assert aggregate(values, [])["conflict"]
    assert aggregate(values, ["HIGH_VOLATILITY"])["direction"] == "UNCLEAR"


def test_duplicates_missing_members_and_internal_conflicts_cannot_create_consensus():
    with pytest.raises(ValueError):
        aggregate(evidence()[:-1], [])
    values = evidence()
    values[1] = copy.deepcopy(values[0])
    with pytest.raises(ValueError):
        aggregate(values, [])
    values = evidence({"sma-5-20": "BULLISH", "sma-20-60": "BEARISH"})
    assert aggregate(values, [])["groups"][0]["internal_conflict"]
    values[0]["status"] = "INSUFFICIENT_HISTORY"
    assert aggregate(values, [])["direction"] == "UNCLEAR"


def test_prefixes_and_decimal_context_cannot_change_past_evidence():
    frame = bars([100 + i * 0.05 + math.sin(i * 0.5) * 2 for i in range(100)])
    at = synthesize(frame.head(83), "1d")
    with localcontext() as context:
        context.prec = 3
        assert synthesize(frame.head(83), "1d") == at
    assert synthesize(frame, "1d")["as_of_bar"] != at["as_of_bar"]
    # An append-only evaluation always supplies only the information prefix.
    changed = bars(frame["close"].head(83).to_list() + [9999.0] * 17)
    assert synthesize(changed.head(83), "1d") == at
    assert at["coverage"] == {"computed": 8, "total": 8}
    assert at["forecast_validated"] is False


def test_flat_state_is_neutral_and_oversold_long_is_not_an_upward_prediction():
    flat = synthesize(bars([100.0] * 80), "1d")
    assert flat["direction"] == "RANGE"
    assert flat["indicators"]["adx14_wilder"] == "0.00000000"
    falling = synthesize(bars([200.0 - i for i in range(80)]), "1d")
    rsi = next(x for x in falling["evidence"] if x["family"] == "RSI")
    assert rsi["research_state"] == 1 and rsi["direction"] != "BULLISH"
    assert falling["indicators"]["adx14_wilder"] == "100.00000000"
    assert synthesize(bars([100.0] * 40), "1d")["direction"] == "UNCLEAR"


def test_a_trend_without_independent_confirmation_is_abstention_not_range_prediction():
    result = synthesize(bars([100.0 + i for i in range(100)]), "1d")
    assert result["market_state"] == "TREND"
    assert result["direction"] == "UNCLEAR"
    assert result["abstention_reason"] == "TREND_WITHOUT_CONFIRMATION"


@pytest.mark.parametrize("change", ["high-volatility", "no-volume", "cold-history"])
def test_position_review_is_not_skipped_when_entry_filter_fails(change):
    frame = bars(
        [100.0] * (10 if change == "cold-history" else 80),
        [0.0] * 80 if change == "no-volume" else None,
    )
    if change == "high-volatility":
        import polars as pl

        frame = frame.with_columns(
            pl.lit(120.0).alias("high"), pl.lit(80.0).alias("low")
        )
    plan = conditional_plan(
        frame, timeframe="1d", horizon_bars=5, purchase_price="100", held_bars=5
    )
    assert plan["entry_action"] in {"AVOID", "UNAVAILABLE"}
    assert plan["action"] == plan["position_action"] == "EXIT_REVIEW"
    assert plan["exit_rules"] and plan["remaining_observation_bars"] == 0


def test_linked_initial_levels_do_not_widen_with_new_atr_or_change_in_place():
    import polars as pl

    frame = bars([100.0] * 80)
    first = conditional_plan(
        frame, timeframe="1d", horizon_bars=5, purchase_price="100"
    )
    levels = copy.deepcopy(first["levels"])
    wider = frame.with_columns(pl.lit(104.0).alias("high"), pl.lit(96.0).alias("low"))
    linked = conditional_plan(
        wider,
        timeframe="1d",
        horizon_bars=5,
        purchase_price="100",
        initial_levels=levels,
    )
    assert linked["levels"] == first["levels"] == levels
    assert linked["levels_scope"] == "LINKED_INITIAL"
    assert (
        legacy_conditional_plan(wider, timeframe="1d", horizon_bars=5)["levels"] is None
    )
    with pytest.raises(ValueError):
        conditional_plan(
            frame,
            timeframe="1d",
            horizon_bars=5,
            purchase_price="101",
            initial_levels=levels,
        )
