from __future__ import annotations

from decimal import Decimal, localcontext

import polars as pl
import pytest

from quantforge.product.execution_replay import ExecutionPolicy, replay_quantities
from quantforge.product.pilot.fees import CnFeeProfile
from quantforge.risk import RiskLimits
from tests.product.test_strategy_optimization import bars


def policy(*, cash: Decimal = Decimal(100000)) -> ExecutionPolicy:
    return ExecutionPolicy(
        cash,
        100,
        "SHARES",
        Decimal("0.01"),
        Decimal(0),
        CnFeeProfile(
            name="DECLARED",
            commission_bps="3",
            min_commission="5",
            stamp_duty_bps="0",
            transfer_fee_bps="0",
        ),
        RiskLimits(
            max_order_notional=Decimal(30000),
            max_position_notional=Decimal(200000),
            max_gross_exposure=Decimal(200000),
        ),
    )


def targets(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.select("symbol", "timestamp").with_columns(pl.lit(1.0).alias("value"))


def test_partial_orders_top_up_with_fees_and_never_negative_cash() -> None:
    frame = bars(12, trend=True).with_columns(pl.lit(10000.0).alias("volume"))
    result = replay_quantities(frame, targets(frame), policy())
    assert result["partial_count"] > 1
    assert all(Decimal(row["cash"]) >= 0 for row in result["periods"])
    assert all(Decimal(row["filled"]) % 100 == 0 for row in result["orders"])
    assert Decimal(result["fee_total"]) == sum(
        (Decimal(row["fee"]) for row in result["orders"]), Decimal(0)
    )
    assert all(row["known_volume_from"] < row["session"] for row in result["orders"])
    assert result["mode"] == "PAPER_RESEARCH" and not result["live_eligible"]


def test_current_daily_volume_does_not_change_current_open_fill() -> None:
    frame = bars(12, trend=True).with_columns(pl.lit(10000.0).alias("volume"))
    revised = (
        frame.with_row_index("index")
        .with_columns(
            pl.when(pl.col("index") == 1)
            .then(10000000.0)
            .otherwise(pl.col("volume"))
            .alias("volume")
        )
        .drop("index")
    )
    original = replay_quantities(frame, targets(frame), policy())
    changed = replay_quantities(revised, targets(revised), policy())
    assert original["orders"][0] == changed["orders"][0]
    assert original["periods"][:2] == changed["periods"][:2]


def test_cash_shortfall_and_blocked_open_are_honest_rejections() -> None:
    frame = bars(12, trend=True)
    blocked = {frame["timestamp"][1].date().isoformat(): "OPEN_UNQUALIFIED"}
    result = replay_quantities(
        frame, targets(frame), policy(cash=Decimal(50)), blocked_sessions=blocked
    )
    assert result["fill_count"] == 0
    assert Decimal(result["fee_total"]) == 0
    # A target that cannot purchase even one lot remains flat; no invented fill.
    assert all(Decimal(row["quantity"]) == 0 for row in result["periods"])
    bigger = replay_quantities(
        frame, targets(frame), policy(), blocked_sessions=blocked
    )
    assert bigger["orders"][0]["reason"] == "OPEN_UNQUALIFIED"
    assert bigger["orders"][0]["filled"] == "0"


def test_fractional_or_boolean_signals_do_not_change_binary_contract() -> None:
    frame = bars(12)
    with pytest.raises(ValueError):
        replay_quantities(
            frame, targets(frame).with_columns(pl.lit(0.5).alias("value")), policy()
        )
    with pytest.raises(ValueError):
        replay_quantities(
            frame, targets(frame).with_columns(pl.lit(True).alias("value")), policy()
        )


def test_replay_is_independent_of_ambient_decimal_precision() -> None:
    frame = bars(12)
    expected = replay_quantities(frame, targets(frame), policy())
    with localcontext() as context:
        context.prec = 6
        assert replay_quantities(frame, targets(frame), policy()) == expected
