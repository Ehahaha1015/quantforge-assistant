import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from test_chart_suite import bars

from quantforge.strategies.chart_evaluation import (
    assess_observation,
    evaluate_history,
    freeze_target,
    label_return,
    label_threshold,
    purged_phase,
)


def test_labels_and_boundary_purging_are_frozen_and_timezone_aware():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    a, b = start + timedelta(days=100), start + timedelta(days=150)
    assert purged_phase(start, a - timedelta(seconds=1), a, b) == "TRAIN"
    assert purged_phase(start, a, a, b) is None
    assert purged_phase(a, b, a, b) is None
    with pytest.raises(ValueError):
        purged_phase(start.replace(tzinfo=None), a, a, b)
    assert label_threshold(Decimal(100), Decimal(2), 4) == Decimal("0.02")
    assert label_return(Decimal("0.02"), Decimal("0.02")) == "RANGE"


def test_test_prices_cannot_change_training_results_or_training_majority():
    values = [100 + i * 0.05 + math.sin(i * 0.5) for i in range(180)]
    first = evaluate_history({"software-fixture": bars(values)}, "1d")
    changed = bars(values[:144] + [x + 25 for x in values[144:]])
    second = evaluate_history({"software-fixture": changed}, "1d")
    assert first["split_boundaries"] == second["split_boundaries"]
    for horizon in ("1", "5", "20"):
        assert (
            first["summaries"][horizon]["TRAIN"]
            == second["summaries"][horizon]["TRAIN"]
        )
        for row in first["samples"]:
            if row["phase"] == "TRAIN":
                assert row["label_end"] < first["split_boundaries"][0]
    assert first["purged_boundary_samples"]["20"] == 40
    metrics = first["summaries"]["5"]["TEST"]["LEGACY_ENTRY_ONLY"]
    assert metrics["samples"] == metrics["covered"] + metrics["abstained"]
    assert {x["fee_bps"] for x in first["replays"]} == {10, 30}
    assert (
        first["forecast_validated"] is False and first["execution_qualified"] is False
    )


def test_observation_maturity_retroactivity_revisions_and_ambiguous_touches():
    frame = bars([100.0] * 85)
    origin = frame["timestamp"][79]
    target = freeze_target(
        direction="RANGE",
        timeframe="1d",
        horizon=5,
        origin=origin,
        reference=Decimal(100),
        atr=Decimal(2),
        frozen_at=origin,
    )
    pending = assess_observation(target, frame.head(83), frame["timestamp"][-1])
    assert pending["status"] == "PENDING" and pending["observed_bars"] == 3
    final = assess_observation(
        target,
        frame,
        frame["timestamp"][-1],
        {"stop_reference": "99", "take_profit_reference": "101"},
    )
    assert (
        final["status"] == "MATURED"
        and final["kind"] == "FORWARD_FROZEN"
        and final["correct"]
    )
    assert final["first_reference_touch"] == "BOTH_AMBIGUOUS_STOP_FIRST"
    assert (
        assess_observation(
            target,
            frame,
            frame["timestamp"][-1].astimezone(ZoneInfo("Asia/Shanghai")),
            {"stop_reference": "99", "take_profit_reference": "101"},
        )
        == final
    )
    retro = freeze_target(
        direction="RANGE",
        timeframe="1d",
        horizon=5,
        origin=origin,
        reference=Decimal(100),
        atr=Decimal(2),
        frozen_at=frame["timestamp"][-1],
    )
    assert (
        assess_observation(retro, frame, frame["timestamp"][-1])["kind"]
        == "RETROSPECTIVE"
    )
    altered = frame.with_columns(pl.lit(100.1).alias("close"))
    assert (
        assess_observation(target, altered, frame["timestamp"][-1])["status"]
        == "ORIGIN_REVISED"
    )
    assert (
        assess_observation(target, frame.tail(5), frame["timestamp"][-1])["status"]
        == "ORIGIN_MISSING"
    )
