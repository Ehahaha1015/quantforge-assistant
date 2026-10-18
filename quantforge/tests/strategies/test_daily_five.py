from copy import deepcopy
from decimal import Decimal

import pytest
from test_chart_quality import sessions

from quantforge.strategies.daily_five import (
    POLICIES,
    prediction,
    prepare_inputs,
    report_plan,
)


def inputs():
    frame = sessions()
    return prepare_inputs(frame, frame)


def test_relative_features_are_past_only_and_do_not_fill_missing_reference_with_zero():
    frame = sessions()
    current = prepare_inputs(frame, frame)
    assert current["features"]["relative_return5"] == "0"
    assert current["quality"]["eligible"]
    absent = prepare_inputs(frame, None)
    assert absent["features"]["relative_return5"] is None
    assert absent["target"] == {"timeframe": "1d", "horizon_bars": 5}


def test_observation_horizon_and_holding_limit_are_independent():
    data = inputs()
    decision = prediction(data, [], "BASELINE_V2")
    plan = report_plan(
        data, decision, max_holding_bars=10, purchase_price="100", held_bars=7
    )
    assert plan["forecast_horizon_bars"] == 5 and plan["max_observation_bars"] == 10
    assert plan["remaining_observation_bars"] == 3 and plan["action"] == "HOLD_REVIEW"
    assert (
        report_plan(
            data, decision, max_holding_bars=10, purchase_price="100", held_bars=10
        )["action"]
        == "EXIT_REVIEW"
    )
    with pytest.raises(ValueError):
        report_plan(data, decision, max_holding_bars=0)


def test_router_and_confirmation_use_the_declared_primary_group_without_fake_votes():
    current = inputs()
    current["base"]["market_state"] = "TREND"
    current["base"]["groups"][0]["direction"] = "BULLISH"
    current["features"]["momentum5_atr"] = "1"
    current["features"]["rsi14"] = "50"
    assert prediction(current, [], "REGIME_ROUTER")["direction"] == "BULLISH"
    assert prediction(current, [], "REGIME_PERSISTENT")["direction"] == "UNCLEAR"
    previous = deepcopy(current)
    previous["as_of_bar"] = "2024-01-01T07:00:00+00:00"
    assert (
        prediction(current, [], "REGIME_PERSISTENT", previous)["direction"] == "BULLISH"
    )
    assert (
        prediction(current, [], "REGIME_PERSISTENT", current)["direction"] == "UNCLEAR"
    )
    current["quality"]["hard_flags"] = ["SESSION_GAPS"]
    assert all(prediction(current, [], p)["direction"] == "UNCLEAR" for p in POLICIES)


def test_frozen_cost_levels_are_reused_without_mutation():
    data = inputs()
    decision = prediction(data, [], "BASELINE_V2")
    first = report_plan(data, decision, purchase_price="100")
    levels = deepcopy(first["levels"])
    data["features"]["atr14"] = "3"
    linked = report_plan(data, decision, purchase_price="100", initial_levels=levels)
    assert linked["levels"] == levels == first["levels"]
    assert Decimal(linked["levels"]["stop_reference"]) == 96


def test_bad_data_withholds_new_levels_but_preserves_linked_position_history():
    data = inputs()
    original = prediction(data, [], "BASELINE_V2")
    levels = report_plan(data, original, purchase_price="100")["levels"]
    data["quality"]["hard_flags"] = ["SESSION_GAPS"]
    blocked = prediction(data, [], "BASELINE_V2")
    assert report_plan(data, blocked)["levels"] is None
    assert report_plan(data, blocked, purchase_price="100")["levels"] is None
    linked = report_plan(data, blocked, purchase_price="100", initial_levels=levels)
    assert linked["levels"] == levels and linked["action"] == "EXIT_REVIEW"
