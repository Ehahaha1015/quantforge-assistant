from copy import deepcopy
from decimal import Decimal, localcontext

import polars as pl
from test_chart_quality import sessions

from quantforge.strategies.daily_five import prediction, prepare_inputs
from quantforge.strategies.daily_replay import replay


def snapshots(frame):
    values = {}
    for index in range(79, frame.height):
        data = prepare_inputs(frame.head(index + 1), frame.head(index + 1))
        data["base"]["market_state"] = "TREND"
        data["base"]["groups"][0]["direction"] = "BULLISH"
        data["features"]["momentum5_atr"] = "1"
        data["features"]["rsi14"] = "50"
        data["features"]["extension20_atr"] = "0"
        values[frame["timestamp"][index].isoformat()] = {
            "inputs": data,
            "decision": prediction(data, [], "REGIME_ROUTER"),
        }
    return values


def test_orders_wait_for_next_open_and_stop_cannot_sell_on_purchase_day():
    frame = sessions(84)
    frame = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 80)
        .then(95.0)
        .otherwise(pl.col("low"))
        .alias("low")
    )
    report = replay(frame.slice(79), snapshots(frame), friction_bps=10)
    entry = next(x for x in report["events"] if x["kind"] == "SIMULATED_ENTRY")
    assert entry["signal_at"] == frame["timestamp"][79].isoformat()
    assert entry["at"] == frame["timestamp"][80].isoformat()
    assert any(x["kind"] == "EXIT_BLOCKED_T1" for x in report["events"])
    assert report["closed_trades"] >= 1
    first = report["trades"][0]
    assert first["entry_at"][:10] < first["exit_at"][:10]
    assert report["execution_qualified"] is False


def test_gap_and_lock_filters_do_not_fabricate_entries():
    frame = sessions(84)
    data = snapshots(frame)
    gap = frame.with_columns(
        *[
            pl.when(pl.int_range(pl.len()) == 80)
            .then(110.0 + offset)
            .otherwise(pl.col(col))
            .alias(col)
            for col, offset in (("open", 0), ("high", 1), ("low", -1), ("close", 0))
        ]
    )
    result = replay(gap.slice(79), data)
    assert any(x["kind"] == "ENTRY_CANCELLED_GAP" for x in result["events"])
    locked = frame.with_columns(pl.lit(100.0).alias("high"), pl.lit(100.0).alias("low"))
    assert not any(
        x["kind"] == "SIMULATED_ENTRY" for x in replay(locked.slice(79), data)["events"]
    )


def test_cash_and_passive_baselines_do_not_inherit_report_stop_policy():
    frame = sessions(90)
    data = snapshots(frame)
    cash = replay(frame.slice(79), data, mode="CASH")
    assert (
        Decimal(cash["price_return_after_assumed_costs"]) == 0
        and Decimal(cash["total_fees"]) == 0
    )
    hold = replay(frame.slice(79), data, mode="BUY_AND_HOLD", max_holding_bars=3)
    assert hold["closed_trades"] == 0 and hold["open_quantity"] > 0
    assert not any(x["kind"] == "EXIT_BLOCKED_T1" for x in hold["events"])
    expected = replay(frame.slice(79), data)
    with localcontext() as context:
        context.prec = 3
        assert replay(frame.slice(79), deepcopy(data)) == expected


def test_large_down_gap_does_not_allocate_a_position_without_a_valid_plan():
    frame = sessions(84)
    data = snapshots(frame)
    gap = frame.with_columns(
        *[
            pl.when(pl.int_range(pl.len()) == 80)
            .then(value)
            .otherwise(pl.col(col))
            .alias(col)
            for col, value in (
                ("open", 2.0),
                ("high", 3.0),
                ("low", 1.0),
                ("close", 2.0),
            )
        ]
    )
    result = replay(gap.slice(79, 2), data)
    assert not any(x["kind"] == "SIMULATED_ENTRY" for x in result["events"])
    assert result["open_quantity"] == 0 and result["blocked_attempts"] == 1


def test_missing_sessions_cancel_pending_entry_and_higher_costs_reduce_cash():
    frame = sessions(90)
    data = snapshots(frame)
    missing = frame.filter(pl.int_range(pl.len()) != 80)
    result = replay(missing.slice(79, 2), data)
    assert any(x["kind"] == "ENTRY_CANCELLED_DATA_GAP" for x in result["events"])
    assert result["open_quantity"] == 0
    low = replay(frame.slice(79), data, friction_bps=10, mode="BUY_AND_HOLD")
    high = replay(frame.slice(79), data, friction_bps=30, mode="BUY_AND_HOLD")
    assert Decimal(high["ending_equity"]) < Decimal(low["ending_equity"])
