from __future__ import annotations

import json
from decimal import ROUND_UP, Decimal, Inexact, localcontext
from typing import Any

import polars as pl
import pytest
from test_evaluator import RECIPE, bars

from quantforge.portfolio import create_portfolio
from quantforge.product.scale.replay import replay_step


def replay(frames: dict[str, pl.DataFrame], n: int) -> list[dict[str, Any]]:
    state: dict[str, Any] = {}
    events = []
    for cursor in range(n):
        event = replay_step(frames, RECIPE, cursor, state)
        state = json.loads(
            json.dumps(event["state"])
        )  # simulate fresh process durable JSON
        events.append(event)
    return events


def test_multi_asset_replay_restarts_without_duplicate_and_decimal_accounting() -> None:
    frames = {"BBB": bars("BBB"), "AAA": bars("AAA")}
    uninterrupted = replay(frames, 30)
    early = replay(frames, 15)
    state = json.loads(json.dumps(early[-1]["state"]))
    late = []
    for cursor in range(15, 30):
        event = replay_step(frames, RECIPE, cursor, state)
        late.append(event)
        state = event["state"]
    assert early + late == uninterrupted
    ids = [f["order_id"] for event in uninterrupted for f in event["fills"]]
    assert ids and len(ids) == len(set(ids))
    assert uninterrupted[-1]["state"]["processed_order_ids"] == ids
    cash = Decimal(100000)
    fees = Decimal(0)
    held: dict[str, Decimal] = {}
    for event in uninterrupted:
        for fill in event["fills"]:
            quantity, price, fee = (
                Decimal(fill[k]) for k in ("quantity", "price", "fee")
            )
            assert quantity == quantity.to_integral_value() and fee > 0
            fees += fee
            direction = Decimal(1) if fill["side"] == "BUY" else Decimal(-1)
            cash -= direction * quantity * price + fee
            held[fill["symbol"]] = (
                held.get(fill["symbol"], Decimal(0)) + direction * quantity
            )
        assert Decimal(event["state"]["portfolio"]["cash"]) == cash
        assert Decimal(event["state"]["portfolio"]["cumulative_fees"]) == fees
        assert all(r["approved"] for r in event["risk"])
    assert {
        p["symbol"]: Decimal(p["quantity"]) for p in state["portfolio"]["positions"]
    } == {s: q for s, q in held.items() if q}
    assert uninterrupted == replay(dict(reversed(list(frames.items()))), 30)


def test_next_open_intents_use_previous_closes_and_never_later_tail() -> None:
    frame = bars("AAA")
    baseline = replay({"AAA": frame}, 15)
    later = (
        frame.with_row_index("row")
        .with_columns(
            [
                pl.when(pl.col("row") >= 15)
                .then(float("nan"))
                .otherwise(pl.col(p))
                .alias(p)
                for p in ("open", "close", "high", "low", "volume")
            ]
        )
        .drop("row")
    )
    assert baseline == replay({"AAA": later}, 15)
    current_close = (
        frame.with_row_index("row")
        .with_columns(
            pl.when(pl.col("row") == 14)
            .then(pl.col("close") * 0.5)
            .otherwise(pl.col("close"))
            .alias("close"),
            pl.when(pl.col("row") == 14)
            .then(pl.col("low") * 0.5)
            .otherwise(pl.col("low"))
            .alias("low"),
        )
        .drop("row")
    )
    changed = replay_step({"AAA": current_close}, RECIPE, 14, baseline[13]["state"])
    assert changed["intents"] == baseline[14]["intents"]
    assert changed["fills"] == baseline[14]["fills"]
    assert changed["valuation"] != baseline[14]["valuation"]


def test_returned_state_blocks_duplicate_cursor_recipe_and_order_history() -> None:
    frames = {"AAA": bars("AAA")}
    event = replay(frames, 12)[-1]
    with pytest.raises(ValueError, match="CURSOR_STATE_MISMATCH"):
        replay_step(frames, RECIPE, 11, event["state"])
    with pytest.raises(ValueError, match="RECIPE_CHANGED"):
        replay_step(frames, {"short_window": 2, "long_window": 7}, 12, event["state"])
    with pytest.raises(ValueError, match="RECIPE_CHANGED"):
        replay_step(frames, RECIPE, 12, event["state"], fee_bps="25")
    altered = {**event["state"], "processed_order_ids": ["duplicate", "duplicate"]}
    with pytest.raises(ValueError, match="DUPLICATE_ORDER_HISTORY"):
        replay_step(frames, RECIPE, 12, altered)


def test_frozen_risk_rejection_cannot_make_fill() -> None:
    frames = {"AAA": bars("AAA")}
    state = replay(frames, 7)[-1]["state"]
    wealthy = create_portfolio(Decimal(1_000_000)).model_dump(mode="json")
    event = replay_step(frames, RECIPE, 7, {**state, "portfolio": wealthy})
    assert event["intents"] and event["risk"] and not event["risk"][0]["approved"]
    assert "ORDER_NOTIONAL_LIMIT" in event["risk"][0]["reasons"]
    assert event["fills"] == [] and event["state"]["portfolio"] == wealthy


@pytest.mark.parametrize("fee", ["0", "NaN", "Infinity", "10000"])
def test_zero_nonfinite_oversized_fees_reject(fee: str) -> None:
    with pytest.raises(ValueError, match="REPLAY_FEE"):
        replay_step({"AAA": bars("AAA")}, RECIPE, 0, {}, fee_bps=fee)


def test_caller_decimal_context_does_not_change_accounting() -> None:
    frames = {"AAA": bars("AAA"), "BBB": bars("BBB", multiplier=1.25)}
    expected = replay(frames, 12)
    with localcontext() as ambient:
        ambient.prec = 3
        ambient.rounding = ROUND_UP
        ambient.traps[Inexact] = True
        assert replay(frames, 12) == expected


def test_exhaustion_fails_without_advancing_and_initial_cursor_is_zero() -> None:
    frames = {"AAA": bars("AAA", n=15)}
    state = replay(frames, 15)[-1]["state"]
    with pytest.raises(ValueError, match="INCOMPLETE_PREFIX"):
        replay_step(frames, RECIPE, 15, state)
    with pytest.raises(ValueError, match="INITIAL_CURSOR"):
        replay_step(frames, RECIPE, 3, {})
