from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import polars as pl
import pytest

from quantforge.product.execution_replay import (
    WeightRisk,
    replay_quantities,
    replay_weights,
)
from tests.product.test_execution_replay import policy, targets
from tests.product.test_strategy_optimization import bars


def weights(frame: pl.DataFrame, value: str) -> pl.DataFrame:
    return frame.select("symbol", "timestamp").with_columns(
        pl.lit(value).alias("weight")
    )


def test_fractional_weights_use_new_version_and_post_fee_buy_cap() -> None:
    frame = bars(20, trend=True).with_columns(
        pl.lit(1_000_000.0).alias("volume"),
        (pl.col("open") * 1.6).alias("close"),
        (pl.col("open") * 1.6 + 1).alias("high"),
    )
    result = replay_weights(
        frame,
        weights(frame, "0.5000"),
        policy(),
        WeightRisk(Decimal("0.5"), Decimal("0.02")),
    )
    assert result["schema_version"] == "weighted-quantity-replay-v2"
    assert result["weight_risk"]["max_weight"] == "0.5"
    assert all(Decimal(row["cash"]) >= 0 for row in result["periods"])
    assert result["fill_count"] > 0
    assert all(
        Decimal(row["weight_after_open"]) <= Decimal("0.5")
        for row in result["orders"]
        if row["side"] == "BUY" and row["status"] != "REJECTED"
    )
    assert result["market_weight_breach_days"] > 0
    assert all(
        Decimal(row["target"]) == Decimal("0.5") for row in result["periods"][1:]
    )
    with pytest.raises(ValueError):
        replay_quantities(
            frame, targets(frame).with_columns(pl.lit(0.5).alias("value")), policy()
        )


@pytest.mark.parametrize("value", ["-0.1", "1.1", "0.50001", "NaN", "0.6"])
def test_invalid_or_over_cap_weights_are_rejected(value: str) -> None:
    frame = bars(20)
    with pytest.raises(ValueError):
        replay_weights(
            frame,
            weights(frame, value),
            policy(),
            WeightRisk(Decimal("0.5")),
        )


def test_numerical_weight_schema_cannot_silently_coerce_float() -> None:
    frame = bars(20)
    with pytest.raises(ValueError):
        replay_weights(
            frame,
            weights(frame, "0.5").with_columns(pl.lit(0.5).alias("weight")),
            policy(),
            WeightRisk(),
        )


def test_rebalance_band_does_not_keep_cash_target_in_market() -> None:
    frame = bars(60, trend=True).with_columns(pl.lit(10_000_000.0).alias("volume"))
    signal = frame.select("symbol", "timestamp").with_columns(
        pl.Series("weight", ["0.50"] * 10 + ["0"] * 50, dtype=pl.String)
    )
    result = replay_weights(
        frame,
        signal,
        policy(),
        WeightRisk(Decimal(1), Decimal("0.02")),
    )
    assert Decimal(result["periods"][-1]["quantity"]) == 0


def test_band_reduces_rebalancing_without_suppressing_limit_reduction() -> None:
    frame = bars(60, trend=True).with_columns(pl.lit(10_000_000.0).alias("volume"))
    fine_lots = replace(policy(), lot_size=1)
    exact = replay_weights(
        frame, weights(frame, "0.5"), fine_lots, WeightRisk(Decimal(1), Decimal(0))
    )
    band = replay_weights(
        frame, weights(frame, "0.5"), fine_lots, WeightRisk(Decimal(1), Decimal("0.02"))
    )
    assert band["fill_count"] < exact["fill_count"]
    capped = replay_weights(
        frame,
        weights(frame, "0.5"),
        fine_lots,
        WeightRisk(Decimal("0.5"), Decimal("0.20")),
    )
    assert any(row["side"] == "SELL" for row in capped["orders"])


def test_flat_prices_post_fee_weight_and_cash_reconcile_exactly() -> None:
    frame = bars(12).with_columns(
        *(pl.lit(100.0).alias(name) for name in ("open", "high", "low", "close")),
        pl.lit(100_000_000.0).alias("volume"),
    )
    result = replay_weights(
        frame, weights(frame, "0.5"), policy(), WeightRisk(Decimal("0.5"))
    )
    assert result["market_weight_breach_days"] == 0
    assert all(
        Decimal(row["weight_at_close"]) <= Decimal("0.5") for row in result["periods"]
    )
    assert all(
        Decimal(row["equity"]) == Decimal(row["cash"]) + Decimal(row["quantity"]) * 100
        for row in result["periods"]
    )
    assert (
        Decimal(result["periods"][-1]["cash"])
        + Decimal(result["periods"][-1]["quantity"]) * 100
        + Decimal(result["fee_total"])
        == policy().initial_cash
    )


def test_future_weight_and_price_changes_do_not_revise_prefix() -> None:
    frame = bars(60).with_columns(pl.lit(10_000_000.0).alias("volume"))
    changed = (
        frame.with_row_index("index")
        .with_columns(
            *(
                pl.when(pl.col("index") >= 30)
                .then(pl.col(name) * 2)
                .otherwise(pl.col(name))
                .alias(name)
                for name in ("open", "high", "low", "close")
            )
        )
        .drop("index")
    )
    original = replay_weights(frame, weights(frame, "0.5"), policy(), WeightRisk())
    target = (
        weights(changed, "0.5")
        .with_row_index("index")
        .with_columns(
            pl.when(pl.col("index") >= 30)
            .then(pl.lit("0"))
            .otherwise(pl.col("weight"))
            .alias("weight")
        )
        .drop("index")
    )
    revised = replay_weights(changed, target, policy(), WeightRisk())
    assert original["periods"][:30] == revised["periods"][:30]
    cutoff = original["periods"][30]["session"]
    assert [row for row in original["orders"] if row["session"] < cutoff] == [
        row for row in revised["orders"] if row["session"] < cutoff
    ]


@pytest.mark.parametrize(
    "cap,band",
    [
        (0.5, Decimal(0)),
        (Decimal(1), True),
        (Decimal(0), Decimal(0)),
        (Decimal("NaN"), Decimal(0)),
        (Decimal(1), Decimal("0.21")),
    ],
)
def test_risk_values_require_exact_bounded_decimals(cap: object, band: object) -> None:
    with pytest.raises(ValueError):
        WeightRisk(cap, band)  # type: ignore[arg-type]
