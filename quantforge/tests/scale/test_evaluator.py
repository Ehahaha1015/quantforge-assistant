from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from quantforge.data import BAR_SCHEMA
from quantforge.product.scale.evaluator import evaluate


def bars(symbol: str, *, n: int = 44, multiplier: float = 1.0) -> pl.DataFrame:
    closes = [
        multiplier * (100.0 + i * 0.4 + 2.0 * math.sin(i / 2.0)) for i in range(n)
    ]
    opens = [multiplier * (99.8 + i * 0.4 + math.sin(i / 3.0)) for i in range(n)]
    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "timestamp": [
                datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i) for i in range(n)
            ],
            "open": opens,
            "close": closes,
            "high": [
                max(o, c) + multiplier for o, c in zip(opens, closes, strict=True)
            ],
            "low": [min(o, c) - multiplier for o, c in zip(opens, closes, strict=True)],
            "volume": [1000.0 + i for i in range(n)],
        },
        schema=BAR_SCHEMA,
    )


def oracle(frame: pl.DataFrame, short: int, long: int, fee: float) -> list[float]:
    """Independent Python causal calculation, including old-position overnight."""
    closes, opens = frame["close"].to_list(), frame["open"].to_list()
    values: list[float] = []
    old_position = 0.0
    for index in range(len(closes)):
        if index < long:
            position = 0.0
        else:
            a = math.fsum(closes[index - short : index]) / short
            b = math.fsum(closes[index - long : index]) / long
            position = float(a > b)
        if index == 0:
            values.append(0.0)
        else:
            overnight = opens[index] / closes[index - 1] - 1.0
            intraday = closes[index] / opens[index] - 1.0
            switch = abs(position - old_position)
            values.append(
                (1 + old_position * overnight)
                * (1 - switch * fee / 10000)
                * (1 + position * intraday)
                - 1
            )
        old_position = position
    return values


RECIPE = {"short_window": 3, "long_window": 7}
PROTOCOL = {
    "initial_history_bars": 12,
    "window_bars": 8,
    "label_horizon_bars": 1,
    "fee_bps": 10,
    "partition_id": "declared-test",
}


def test_causal_returns_and_metrics_against_independent_reference() -> None:
    frames = {"BBB": bars("BBB", multiplier=2), "AAA": bars("AAA")}
    pack = evaluate(frames, RECIPE, PROTOCOL)
    expected = [
        math.fsum(pair) / 2
        for pair in zip(
            oracle(frames["AAA"], 3, 7, 10)[12:],
            oracle(frames["BBB"], 3, 7, 10)[12:],
            strict=True,
        )
    ]
    assert pack["returns"] == pytest.approx(expected, abs=1e-14)
    wealth = 100000.0
    peak = wealth
    drawdown = 0.0
    for value in expected:
        wealth *= 1 + value
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1)
    mean = math.fsum(expected) / len(expected)
    std = math.sqrt(math.fsum((v - mean) ** 2 for v in expected) / (len(expected) - 1))
    assert pack["metrics"]["total_return"] == pytest.approx(wealth / 100000 - 1)
    assert pack["metrics"]["max_drawdown"] == pytest.approx(drawdown)
    assert pack["metrics"]["sharpe"] == pytest.approx(mean / std * math.sqrt(252))
    assert pack["weights"] == {"AAA": 0.5, "BBB": 0.5}
    assert pack == evaluate(dict(reversed(list(frames.items()))), RECIPE, PROTOCOL)


def test_future_tail_cannot_enter_explicit_prefix_even_if_invalid() -> None:
    original = bars("AAA")
    altered = (
        original.with_row_index("row")
        .with_columns(
            [
                pl.when(pl.col("row") >= 28)
                .then(float("nan"))
                .otherwise(pl.col(p))
                .alias(p)
                for p in ("open", "high", "low", "close", "volume")
            ]
        )
        .drop("row")
    )
    protocol = {**PROTOCOL, "end_bars": 28}
    assert evaluate({"AAA": original}, RECIPE, protocol) == evaluate(
        {"AAA": altered}, RECIPE, protocol
    )


def test_walkforward_window_cannot_use_later_price() -> None:
    original = bars("AAA")
    altered = (
        original.with_row_index("row")
        .with_columns(
            [
                pl.when(pl.col("row") >= 20)
                .then(pl.col(p) * 4)
                .otherwise(pl.col(p))
                .alias(p)
                for p in ("open", "high", "low", "close")
            ]
        )
        .drop("row")
    )
    a, b = (
        evaluate({"AAA": original}, RECIPE, PROTOCOL),
        evaluate({"AAA": altered}, RECIPE, PROTOCOL),
    )
    assert a["returns"][:8] == b["returns"][:8]
    assert a["symbols"]["AAA"]["windows"][0] == b["symbols"]["AAA"]["windows"][0]


def test_cost_delay_stress_tail_and_neighbor_probes_are_explicit() -> None:
    frame = bars("AAA")
    pack = evaluate({"AAA": frame}, RECIPE, PROTOCOL)
    robustness = pack["robustness"]
    assert set(robustness["cost_scenarios"]) == {"25.0", "50.0"}
    assert (
        robustness["cost_scenarios"]["50.0"]["metrics"]["total_return"]
        <= robustness["cost_scenarios"]["25.0"]["metrics"]["total_return"]
        <= pack["metrics"]["total_return"]
    )
    assert robustness["extra_bar_delay"]["metrics"] != pack["metrics"]
    assert (
        robustness["persistent_10pct_price_gap"]["metrics"]["total_return"]
        < pack["metrics"]["total_return"]
    )
    assert len(robustness["parameter_neighborhood"]) == 4
    assert robustness["missing_bar_every_20th"]["observations"] == 31
    assert robustness["tail"]["worst_daily_return"] == min(pack["returns"])
    assert (
        robustness["liquidity"]["AAA"]["capacity"]
        == "NOT_IDENTIFIABLE_NORMALIZED_UNITS"
    )
    assert pack["validation"]["purging"].startswith("NOT_REQUIRED")
    assert not pack["validated_alpha"] and not pack["live_eligible"]


@pytest.mark.parametrize(
    "change",
    [
        {"initial_history_bars": True},
        {"window_bars": 1},
        {"label_horizon_bars": 2},
        {"fee_bps": 0},
        {"fee_bps": 25},
        {"secret": "not allowed"},
        {"end_bars": 15},
    ],
)
def test_invalid_protocols_fail_closed(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        evaluate({"AAA": bars("AAA")}, RECIPE, {**PROTOCOL, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"short_window": True, "long_window": 7},
        {"short_window": 7, "long_window": 3},
        {"short_window": 3, "long_window": 7, "metrics": {}},
    ],
)
def test_no_fact_or_invalid_parameter_admission(change: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        evaluate({"AAA": bars("AAA")}, change, PROTOCOL)


def test_frame_alignment_identity_corruption_and_order_fail() -> None:
    with pytest.raises(ValueError, match="UNSYNCHRONIZED"):
        evaluate({"AAA": bars("AAA"), "BBB": bars("BBB").slice(1)}, RECIPE, PROTOCOL)
    with pytest.raises(ValueError, match="SYMBOL_BINDING"):
        evaluate({"AAA": bars("WRONG")}, RECIPE, PROTOCOL)
    with pytest.raises(ValueError, match="TIME_ORDERED"):
        evaluate({"AAA": bars("AAA").reverse()}, RECIPE, PROTOCOL)
    duplicate = pl.concat([bars("AAA").head(1), bars("AAA")])
    with pytest.raises(ValueError):
        evaluate({"AAA": duplicate}, RECIPE, PROTOCOL)


def test_degenerate_constant_prices_have_finite_zero_metrics() -> None:
    frame = bars("AAA").with_columns(
        [pl.lit(100.0).alias(p) for p in ("open", "high", "low", "close")]
    )
    pack = evaluate({"AAA": frame}, RECIPE, PROTOCOL)
    assert pack["returns"] == [0.0] * 32
    assert all(v == 0.0 for v in pack["metrics"].values())


def test_partition_and_windows_change_when_protocol_changes() -> None:
    a = evaluate({"AAA": bars("AAA")}, RECIPE, PROTOCOL)
    b = evaluate({"AAA": bars("AAA")}, RECIPE, {**PROTOCOL, "window_bars": 7})
    assert a["partition_hash"] != b["partition_hash"]
    assert a["windows"][0]["window_id"] != b["windows"][0]["window_id"]


def test_parameter_probe_stays_within_frozen_maximum50() -> None:
    result = evaluate({"AAA": bars("AAA", n=80)}, {"short_window": 5, "long_window": 50},
                      {"initial_history_bars": 55, "window_bars": 10})
    assert all(p["long_window"] <= 50 for p in result["robustness"]["parameter_neighborhood"])
