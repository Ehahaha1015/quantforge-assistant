from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import polars as pl
import pytest
from test_chart_quality import sessions
from test_daily_replay import snapshots

from quantforge.strategies.daily_experiments import (
    block_uncertainty,
    diagnostic_summary,
    forward_eligible,
    relative_confirmation,
)
from quantforge.strategies.daily_replay import chandelier_stop, replay


def router(direction="BULLISH"):
    return {
        "policy": "REGIME_ROUTER",
        "direction": direction,
        "context_relation": "SINGLE_PERIOD",
        "risk_flags": [],
        "abstention_reason": None,
    }


@pytest.mark.parametrize("direction,sign", [("BULLISH", 1), ("BEARISH", -1)])
def test_relative_filter_requires_two_confirmations_and_preserves_parent(
    direction, sign
):
    parent = router(direction)
    before = deepcopy(parent)
    features = {
        "relative_return5": str(sign * 0.01),
        "relative_return20": str(sign * 0.02),
    }
    assert relative_confirmation(parent, features, [])["direction"] == direction
    for value in (None, "0", str(sign * -0.02)):
        features["relative_return20"] = value
        result = relative_confirmation(parent, features, [])
        assert result["direction"] == "UNCLEAR"
        assert result["abstention_reason"].startswith("RELATIVE_")
    assert parent == before
    assert relative_confirmation(router("UNCLEAR"), {}, [])["direction"] == "UNCLEAR"
    with pytest.raises(ValueError):
        relative_confirmation(parent, {"relative_return5": "NaN"}, [])


def test_chandelier_never_loosens_and_uses_only_recent_completed_position_highs():
    assert chandelier_stop(Decimal(96), [Decimal(105)], Decimal(2)) == Decimal(99)
    assert chandelier_stop(Decimal(99), [Decimal(105)], Decimal(10)) == Decimal(99)
    assert chandelier_stop(
        Decimal(96), [Decimal(200)] + [Decimal(101)] * 20, Decimal(2)
    ) == Decimal(96)
    with localcontext() as context:
        context.prec = 3
        assert chandelier_stop(
            Decimal("96.1234"), [Decimal("105.5678")], Decimal("2.1234")
        ) == Decimal("99.1976")
    with pytest.raises(ValueError):
        chandelier_stop(Decimal(96), [], Decimal(2))


def test_updated_stop_cannot_fill_earlier_in_same_bar_and_next_bar_uses_it():
    frame = sessions(84).with_columns(
        pl.when(pl.int_range(pl.len()) == 80)
        .then(105.5)
        .otherwise(pl.col("high"))
        .alias("high"),
        pl.when(pl.int_range(pl.len()) == 80)
        .then(97.0)
        .otherwise(pl.col("low"))
        .alias("low"),
    )
    data = snapshots(sessions(84))
    original = deepcopy(data)
    candidate = replay(frame.slice(79, 3), data, chandelier=True)
    parent = replay(frame.slice(79, 3), data)
    updates = [r for r in candidate["events"] if r["kind"] == "CHANDELIER_UPDATE"]
    assert (
        updates and updates[0]["effective_after"] == frame["timestamp"][80].isoformat()
    )
    assert not any(r["kind"] == "EXIT_BLOCKED_T1" for r in candidate["events"])
    assert candidate["trades"][0]["exit_at"] == frame["timestamp"][81].isoformat()
    assert candidate["trades"][0]["reason"] == "STOP_REFERENCE"
    assert parent["closed_trades"] == 0
    assert data == original
    short = replay(frame.slice(79, 2), data, chandelier=True)
    assert short["closed_trades"] == 0
    assert short["events"] == [
        r for r in candidate["events"] if r["at"] <= frame["timestamp"][80].isoformat()
    ]
    with pytest.raises(ValueError):
        replay(frame.slice(79), data, chandelier=True, mode="BUY_AND_HOLD")


def test_chandelier_retains_purchase_day_t1_and_gap_stop_execution():
    frame = sessions(84).with_columns(
        pl.when(pl.int_range(pl.len()) == 80)
        .then(95.0)
        .otherwise(pl.col("low"))
        .alias("low"),
    )
    result = replay(frame.slice(79), snapshots(sessions(84)), chandelier=True)
    assert any(r["kind"] == "EXIT_BLOCKED_T1" for r in result["events"])
    assert result["trades"][0]["entry_at"][:10] < result["trades"][0]["exit_at"][:10]
    gap = sessions(84).with_columns(
        *[
            pl.when(pl.int_range(pl.len()) == 81)
            .then(value)
            .otherwise(pl.col(col))
            .alias(col)
            for col, value in (
                ("open", 94.0),
                ("high", 96.0),
                ("low", 93.0),
                ("close", 95.0),
            )
        ]
    )
    report = replay(gap.slice(79, 3), snapshots(sessions(84)), chandelier=True)
    assert Decimal(report["trades"][0]["exit_price"]) < 94


def uncertainty_rows():
    return [
        {
            "origin": (
                datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
            ).isoformat(),
            "symbol": "A",
            "actual": "BULLISH" if i < 30 else "RANGE",
            "predictions": {
                "REGIME_ROUTER": "RANGE",
                "RELATIVE_CONFIRMATION": "BULLISH",
            },
        }
        for i in range(60)
    ]


def test_date_blocks_keep_correlated_symbols_together_and_are_reproducible():
    rows = uncertainty_rows()
    one = block_uncertainty(rows, "RELATIVE_CONFIRMATION", "RANGE", repetitions=30)
    two = block_uncertainty(
        rows + [{**r, "symbol": "B"} for r in rows],
        "RELATIVE_CONFIRMATION",
        "RANGE",
        repetitions=30,
    )
    assert one["intervals"] == two["intervals"]
    assert one == block_uncertainty(
        rows, "RELATIVE_CONFIRMATION", "RANGE", repetitions=30
    )
    few = block_uncertainty(rows[:20], "RELATIVE_CONFIRMATION", "RANGE")
    assert few["status"] == "INSUFFICIENT_DATE_BLOCKS"
    assert all(v["percentile_95"] is None for v in few["intervals"].values())


def test_forward_evidence_requires_new_origin_not_just_new_label_end():
    frozen = datetime(2026, 10, 18, tzinfo=UTC)
    seen = frozen - timedelta(days=2)
    assert not forward_eligible(seen, frozen + timedelta(days=5), frozen, seen)
    assert not forward_eligible(frozen, frozen + timedelta(days=5), frozen, seen)
    assert forward_eligible(
        frozen + timedelta(days=1), frozen + timedelta(days=8), frozen, seen
    )
    with pytest.raises(ValueError):
        forward_eligible(
            frozen.replace(tzinfo=None), frozen + timedelta(days=8), frozen, seen
        )


def test_bullish_diagnostics_distinguish_direction_miss_from_entry_rejection():
    common = {
        "actual": "BULLISH",
        "market_state": "TREND",
        "context_relation": "CONFLICT",
        "primary_group": "TREND",
        "risk_flags": [],
        "abstention_reason": "NONE",
    }
    rows = [
        {
            **common,
            "prediction": "UNCLEAR",
            "entry_action": "WAIT",
            "entry_reason": "NO_DIRECTION",
        },
        {
            **common,
            "prediction": "BULLISH",
            "entry_action": "WAIT",
            "entry_reason": "BACKGROUND_CONFLICT",
        },
    ]
    funnel = diagnostic_summary(rows)["bullish_funnel"]
    assert funnel["actual_bullish"] == 2 and funnel["predicted_bullish"] == 1
    assert funnel["entry_eligible"] == 0
    assert funnel["bullish_but_entry_blocked"] == {"BACKGROUND_CONFLICT": 1}
