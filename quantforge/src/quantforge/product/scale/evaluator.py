"""Pure causal research evaluation; no data acquisition or benchmark capability."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import polars as pl

from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.data import canonicalize_bars, validate_bars
from quantforge.experiments import canonical_json
from quantforge.strategies import (
    MovingAverageCrossoverConfig,
    generate_moving_average_signals,
)
from quantforge.validation import WalkForwardConfig, run_walk_forward_validation
from quantforge.validation.metrics import oos_metrics


def strategy(proposal: dict[str, Any]) -> MovingAverageCrossoverConfig:
    if not isinstance(proposal, dict) or set(proposal) != {
        "short_window",
        "long_window",
    }:
        raise ValueError("SCALE_RECIPE_SCHEMA")
    return MovingAverageCrossoverConfig(**proposal)


def synchronized_frames(
    frames: dict[str, pl.DataFrame], *, end_bars: int | None = None
) -> dict[str, pl.DataFrame]:
    """Physically slice first: later rows never enter validation/calculation."""
    if not isinstance(frames, dict) or not frames:
        raise ValueError("SCALE_EMPTY_UNIVERSE")
    if end_bars is not None and (type(end_bars) is not int or end_bars < 1):
        raise ValueError("SCALE_PREFIX_BOUNDARY")
    result: dict[str, pl.DataFrame] = {}
    common: list[int] | None = None
    for symbol in sorted(frames):
        if (
            not isinstance(symbol, str)
            or not symbol
            or not isinstance(frames[symbol], pl.DataFrame)
        ):
            raise ValueError("SCALE_FRAME_SCHEMA")
        raw = frames[symbol]
        if end_bars is not None:
            if raw.height < end_bars:
                raise ValueError("SCALE_INCOMPLETE_PREFIX")
            raw = raw.head(end_bars)
        frame = canonicalize_bars(raw)
        if frame.is_empty() or frame["symbol"].unique().to_list() != [symbol]:
            raise ValueError("SCALE_SYMBOL_BINDING")
        if not frame["timestamp"].equals(raw["timestamp"]):
            raise ValueError("SCALE_INPUT_NOT_TIME_ORDERED")
        times = frame["timestamp"].cast(pl.Int64).to_list()
        moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            microseconds=int(times[-1])
        )
        validate_bars(frame, as_of=moment)
        if len({int(v) // 86_400_000_000 for v in times}) != len(times):
            raise ValueError("SCALE_ONE_SESSION_PER_DATE")
        if common is not None and common != times:
            raise ValueError("SCALE_UNSYNCHRONIZED_UNIVERSE")
        common = times
        result[symbol] = frame
    return result


def _timestamps(frame: pl.DataFrame) -> list[str]:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return [
        (epoch + timedelta(microseconds=int(value))).isoformat()
        for value in frame["timestamp"].cast(pl.Int64)
    ]


def _basket(periods: dict[str, pl.DataFrame], config: BacktestConfig) -> dict[str, Any]:
    symbols = sorted(periods)
    count = len(symbols)
    n = periods[symbols[0]].height
    times = _timestamps(periods[symbols[0]])
    for symbol in symbols:
        if periods[symbol].height != n or _timestamps(periods[symbol]) != times:
            raise ValueError("SCALE_PERIOD_ALIGNMENT")
    returns = [
        math.fsum(float(periods[s]["strategy_return"][i]) for s in symbols) / count
        for i in range(n)
    ]
    turnover = [
        math.fsum(float(periods[s]["turnover"][i]) for s in symbols) / count
        for i in range(n)
    ]
    return {
        "returns": returns,
        "timestamps": times,
        "metrics": asdict(oos_metrics(returns, turnover, backtest_config=config)),
        "turnover": turnover,
        "weights": {s: 1.0 / count for s in symbols},
        "construction": "equal_weight_daily_synthetic_return_research_basket",
    }


def _scenario(
    frames: dict[str, pl.DataFrame],
    recipe: MovingAverageCrossoverConfig,
    history: int,
    *,
    fee: float,
    delay: int = 0,
) -> dict[str, Any]:
    config = BacktestConfig(initial_cash=100000.0, fee_bps=fee, periods_per_year=252)
    periods: dict[str, pl.DataFrame] = {}
    for symbol, frame in frames.items():
        signals = generate_moving_average_signals(frame, recipe)
        if delay:
            signals = signals.with_columns(pl.col("value").shift(delay).fill_null(0.0))
        periods[symbol] = run_vectorized_backtest(frame, signals, config).periods.slice(
            history
        )
    basket = _basket(periods, config)
    return {"metrics": basket["metrics"], "observations": len(basket["returns"])}


def evaluate(
    frames: dict[str, pl.DataFrame], proposal: dict[str, Any], protocol: dict[str, Any]
) -> dict[str, Any]:
    """Fixed trailing SMA, one-bar returns and chronological expanding windows.

    ``end_bars`` slices raw inputs before canonicalization and computation.
    The caller must additionally enforce the physically separate prefix/vault
    authority boundary. This pure function accepts explicit supplied frames.
    """
    recipe = strategy(proposal)
    required = {"initial_history_bars", "window_bars"}
    allowed = required | {"end_bars", "label_horizon_bars", "partition_id", "fee_bps"}
    if not isinstance(protocol, dict) or not required <= set(protocol) <= allowed:
        raise ValueError("SCALE_PROTOCOL_SCHEMA")
    history, width = protocol["initial_history_bars"], protocol["window_bars"]
    if (
        type(history) is not int
        or type(width) is not int
        or history < max(2, recipe.long_window)
        or width < 2
    ):
        raise ValueError("SCALE_HISTORY_WINDOW")
    horizon = protocol.get("label_horizon_bars", 1)
    if type(horizon) is not int or horizon != 1:
        raise ValueError("SCALE_UNSUPPORTED_LABEL_HORIZON")
    fee = protocol.get("fee_bps", 10.0)
    if isinstance(fee, bool) or not isinstance(fee, (int, float)) or fee != 10.0:
        raise ValueError("SCALE_FIXED_BASE_FEE")
    if "partition_id" in protocol and (
        not isinstance(protocol["partition_id"], str) or not protocol["partition_id"]
    ):
        raise ValueError("SCALE_PARTITION_ID")
    selected = synchronized_frames(frames, end_bars=protocol.get("end_bars"))
    n = next(iter(selected.values())).height
    if n - history < 4:
        raise ValueError("SCALE_INSUFFICIENT_VALIDATION")
    config = BacktestConfig(initial_cash=100000.0, fee_bps=10.0, periods_per_year=252)
    periods: dict[str, pl.DataFrame] = {}
    symbols: dict[str, Any] = {}
    windows: list[dict[str, Any]] = []
    bound_partition = {
        "protocol": protocol,
        "timestamps": _timestamps(next(iter(selected.values()))),
        "symbols": sorted(selected),
    }
    partition_hash = hashlib.sha256(
        canonical_json(bound_partition).encode()
    ).hexdigest()
    for symbol, frame in selected.items():
        result = run_walk_forward_validation(
            frame,
            recipe,
            config,
            WalkForwardConfig(initial_history_bars=history, oos_window_bars=width),
        )
        periods[symbol] = result.aggregate.periods
        symbols[symbol] = {
            "metrics": asdict(result.aggregate.metrics),
            "returns": result.aggregate.periods["strategy_return"].to_list(),
            "exposure_fraction": float(
                cast(float, result.aggregate.periods["position_after_open"].mean())
            ),
            "mean_position_switch_cost_fraction": float(
                cast(float, result.aggregate.periods["cost_fraction"].mean())
            ),
            "windows": [
                {"index": w.index, "metrics": asdict(w.result.metrics)}
                for w in result.windows
            ],
        }
        if not windows:
            windows = [
                {
                    "window_id": hashlib.sha256(
                        canonical_json(
                            {
                                "partition": partition_hash,
                                "start": w.oos_start_index,
                                "end": w.oos_end_index,
                            }
                        ).encode()
                    ).hexdigest(),
                    "start_index": w.oos_start_index,
                    "end_index": w.oos_end_index,
                    "history_bars": w.history_bars,
                    "start_timestamp": w.result.start_timestamp.isoformat(),
                    "end_timestamp": w.result.end_timestamp.isoformat(),
                }
                for w in result.windows
            ]
    basket = _basket(periods, config)
    returns = basket["returns"]
    split = max(1, len(returns) // 2)
    subperiods = [
        {
            "name": name,
            "observations": len(r),
            "metrics": asdict(oos_metrics(r, t, backtest_config=config)),
        }
        for name, r, t in (
            ("first_half", returns[:split], basket["turnover"][:split]),
            ("second_half", returns[split:], basket["turnover"][split:]),
        )
    ]
    gap_at = history + (n - history) // 2
    gapped = {
        s: f.with_row_index("row")
        .with_columns(
            [
                pl.when(pl.col("row") >= gap_at)
                .then(pl.col(p) * 0.9)
                .otherwise(pl.col(p))
                .alias(p)
                for p in ("open", "high", "low", "close")
            ]
        )
        .drop("row")
        for s, f in selected.items()
    }
    missing = {
        s: f.with_row_index("row")
        .filter((pl.col("row") < history) | ((pl.col("row") - history) % 20 != 19))
        .drop("row")
        for s, f in selected.items()
    }
    neighbors = []
    for short, long in sorted(
        {
            (recipe.short_window - 1, recipe.long_window),
            (recipe.short_window + 1, recipe.long_window),
            (recipe.short_window, recipe.long_window - 1),
            (recipe.short_window, recipe.long_window + 1),
        }
    ):
        if 0 < short < long <= min(history, 50):
            neighbors.append(
                {
                    "short_window": short,
                    "long_window": long,
                    **_scenario(
                        selected,
                        MovingAverageCrossoverConfig(short, long),
                        history,
                        fee=10.0,
                    ),
                }
            )
    tail_count = max(1, math.ceil(len(returns) * 0.05))
    regime_returns: dict[str, list[float]] = {
        "prior_absolute_return_below_2pct": [],
        "prior_absolute_return_at_least_2pct": [],
    }
    representative = next(iter(selected.values()))
    closes = representative["close"].to_list()
    for i, value in enumerate(returns, start=history):
        prior_move = abs(float(closes[i - 1]) / float(closes[i - 2]) - 1.0)
        label = (
            "prior_absolute_return_at_least_2pct"
            if prior_move >= 0.02
            else "prior_absolute_return_below_2pct"
        )
        regime_returns[label].append(value)
    return {
        "schema_version": "scale-evaluation-v1",
        "proposal": dict(proposal),
        "protocol": dict(protocol),
        "partition_hash": partition_hash,
        **basket,
        "symbols": symbols,
        "windows": windows,
        "validation": {
            "scheme": "expanding_chronological_fixed_recipe_no_fitting",
            "initial_history_bars": history,
            "window_bars": width,
            "information_lookback_bars": recipe.long_window,
            "label_horizon_bars": 1,
            "feature_end": "previous_close_for_current_open",
            "purging": "NOT_REQUIRED_NO_FITTED_OR_OVERLAPPING_TRAINING_LABELS",
            "embargo": "NOT_REQUIRED_STRICT_FORWARD_FIXED_RECIPE",
            "minimum_scored_observations": 4,
        },
        "robustness": {
            "cost_scenarios": {
                str(fee): _scenario(selected, recipe, history, fee=fee)
                for fee in (25.0, 50.0)
            },
            "cost_scenario_semantics": "position_switch_fee_including_assumed_spread_slippage",
            "extra_bar_delay": _scenario(selected, recipe, history, fee=10.0, delay=1),
            "missing_bar_every_20th": _scenario(missing, recipe, history, fee=10.0),
            "persistent_10pct_price_gap": {
                "gap_index": gap_at,
                **_scenario(gapped, recipe, history, fee=10.0),
            },
            "parameter_neighborhood": neighbors,
            "subperiods": subperiods,
            "tail": {
                "worst_daily_return": min(returns),
                "empirical_worst_5pct_mean": math.fsum(sorted(returns)[:tail_count])
                / tail_count,
                "tail_observations": tail_count,
            },
            "regime": {
                "reference_symbol": min(selected),
                "definition": "previous_one_bar_absolute_close_return_2pct_threshold",
                "groups": {
                    label: {
                        "observations": len(values),
                        "mean_basket_return": math.fsum(values) / len(values)
                        if values
                        else None,
                    }
                    for label, values in regime_returns.items()
                },
            },
            "liquidity": {
                s: {
                    "minimum_observed_volume": float(cast(float, f["volume"].min())),
                    "median_observed_volume": float(cast(float, f["volume"].median())),
                    "zero_volume_bars": int((f["volume"] == 0).sum()),
                    "capacity": "NOT_IDENTIFIABLE_NORMALIZED_UNITS",
                }
                for s, f in selected.items()
            },
            "concentration": {"maximum_basket_weight": 1.0 / len(selected)},
        },
        "assumptions": [
            "Adjusted normalized OHLCV are treated as tradable proportional prices; corporate actions/units are provider declarations.",
            "252 periods/year, zero risk-free rate, prior-close signal at next open, no terminal liquidation.",
            "Daily equal-weight research return basket is synthetic; per-symbol switch fees do not establish portfolio rebalancing costs.",
            "25/50 bps switch fees are conservative spread/slippage scenarios; no calibrated market impact.",
        ],
        "limitations": [
            "No fitted model here; purge/embargo applicability must be re-evaluated for training or overlapping multi-bar labels.",
            "Missing rows compress the observed session calendar; no interpolated price or independent market calendar claim.",
            "Normalized volume provides a liquidity quality proxy, not monetary capacity or institutional execution precision.",
            "Parameter-neighbor stress computes extra variants; the host must predeclare/count these deterministic probes before exposure.",
            "Regime groups and empirical tail means are descriptive development diagnostics, not significance claims.",
        ],
        "validated_alpha": False,
        "live_eligible": False,
    }
