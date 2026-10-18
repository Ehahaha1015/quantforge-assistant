"""Fixed causal trend ablations, exposure diagnostics and cost/latency stress."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Literal

import polars as pl

from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.data import canonicalize_bars, logical_content_hash, validate_bars
from quantforge.strategies import MovingAverageCrossoverConfig
from quantforge.strategies.models import StrategyConfig, generate_strategy_signals
from quantforge.strategies.time_series_momentum import TimeSeriesMomentumConfig
from quantforge.validation.metrics import oos_metrics


@dataclass(frozen=True)
class BufferedTrendConfig:
    family: Literal["moving_average_crossover", "time_series_momentum"]
    buffer_pct: Decimal
    trend_window: int = 0

    def __post_init__(self) -> None:
        if self.family not in {"moving_average_crossover", "time_series_momentum"}:
            raise ValueError("supported trend family required")
        if not isinstance(self.buffer_pct, Decimal):
            raise TypeError("buffer_pct must be an exact Decimal percent")
        if not self.buffer_pct.is_finite() or not 0 < self.buffer_pct <= 10:
            raise ValueError("finite buffer between zero and ten percent required")
        if type(self.trend_window) is not int or self.trend_window not in {0, 120}:
            raise ValueError("trend window must be 0 or 120")


def _frame(bars: pl.DataFrame) -> pl.DataFrame:
    frame = canonicalize_bars(bars)
    if frame.is_empty() or frame["symbol"].n_unique() != 1:
        raise ValueError("nonempty single-symbol bars required")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    as_of = epoch + timedelta(microseconds=int(frame["timestamp"].cast(pl.Int64)[-1]))
    validate_bars(frame, as_of=as_of)
    return frame


def buffered_signals(bars: pl.DataFrame, config: BufferedTrendConfig) -> pl.DataFrame:
    if not isinstance(config, BufferedTrendConfig):
        raise TypeError("BufferedTrendConfig required")
    frame = _frame(bars)
    closes = [Decimal(str(value)) for value in frame["close"]]
    values = []
    held = False
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        for index, close in enumerate(closes):
            if config.family == "moving_average_crossover":
                if index < 59:
                    values.append(0.0)
                    continue
                short = sum(closes[index - 19 : index + 1]) / Decimal(20)
                long = sum(closes[index - 59 : index + 1]) / Decimal(60)
                gap = (short / long - 1) * 100
            else:
                if index < 60:
                    values.append(0.0)
                    continue
                gap = (close / closes[index - 60] - 1) * 100
            trend_ok = True
            if config.trend_window:
                if index < config.trend_window - 1:
                    values.append(0.0)
                    continue
                trend = sum(
                    closes[index - config.trend_window + 1 : index + 1]
                ) / Decimal(config.trend_window)
                trend_ok = close > trend
            if not trend_ok or gap <= -config.buffer_pct:
                held = False
            elif not held and gap > config.buffer_pct:
                held = True
            values.append(float(held))
    return frame.select("symbol", "timestamp").with_columns(
        pl.Series("value", values, dtype=pl.Float64)
    )


def refinement_manifest() -> list[dict[str, Any]]:
    return [
        {
            "id": "baseline_sma_5_20",
            "family": "moving_average_crossover",
            "parameters": {"short_window": 5, "long_window": 20},
        },
        {
            "id": "baseline_momentum_20",
            "family": "time_series_momentum",
            "parameters": {"lookback": 20, "threshold_pct": "0"},
        },
        {
            "id": "slow_sma_20_60",
            "family": "moving_average_crossover",
            "parameters": {"short_window": 20, "long_window": 60},
        },
        {
            "id": "slow_momentum_60",
            "family": "time_series_momentum",
            "parameters": {"lookback": 60, "threshold_pct": "0"},
        },
        {
            "id": "buffered_sma",
            "family": "moving_average_crossover",
            "parameters": {
                "short_window": 20,
                "long_window": 60,
                "buffer_pct": "0.5",
                "trend_window": 0,
            },
        },
        {
            "id": "buffered_momentum",
            "family": "time_series_momentum",
            "parameters": {"lookback": 60, "buffer_pct": "1", "trend_window": 0},
        },
        {
            "id": "filtered_sma",
            "family": "moving_average_crossover",
            "parameters": {
                "short_window": 20,
                "long_window": 60,
                "buffer_pct": "0.5",
                "trend_window": 120,
            },
        },
        {
            "id": "filtered_momentum",
            "family": "time_series_momentum",
            "parameters": {"lookback": 60, "buffer_pct": "1", "trend_window": 120},
        },
        {"id": "buy_and_hold", "family": "benchmark", "parameters": {"target": 1}},
        {"id": "cash", "family": "benchmark", "parameters": {"target": 0}},
    ]


def _signals(frame: pl.DataFrame, name: str) -> pl.DataFrame:
    configs: dict[str, StrategyConfig] = {
        "baseline_sma_5_20": MovingAverageCrossoverConfig(5, 20),
        "baseline_momentum_20": TimeSeriesMomentumConfig(20),
        "slow_sma_20_60": MovingAverageCrossoverConfig(20, 60),
        "slow_momentum_60": TimeSeriesMomentumConfig(60),
    }
    if name in configs:
        return generate_strategy_signals(frame, configs[name])
    if name in {"cash", "buy_and_hold"}:
        return frame.select("symbol", "timestamp").with_columns(
            pl.lit(float(name == "buy_and_hold")).alias("value")
        )
    family: Literal["moving_average_crossover", "time_series_momentum"] = (
        "moving_average_crossover" if name.endswith("sma") else "time_series_momentum"
    )
    config = BufferedTrendConfig(
        family=family,
        buffer_pct=Decimal("0.5" if name.endswith("sma") else "1"),
        trend_window=120 if name.startswith("filtered") else 0,
    )
    return buffered_signals(frame, config)


def evaluate_refinements(
    bars: pl.DataFrame, *, history_bars: int = 504
) -> dict[str, Any]:
    frame = _frame(bars)
    if type(history_bars) is not int or history_bars < 120:
        raise ValueError("history must cover the largest frozen window")
    if frame.height < history_bars + 20 or frame.height > 20000:
        raise ValueError("bounded data must leave twenty evaluation rows")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    times = [
        (epoch + timedelta(microseconds=int(value))).isoformat()
        for value in frame["timestamp"].cast(pl.Int64)
    ]
    prices = frame["close"].to_list()
    up_days = [
        prices[index] > prices[index - 1] for index in range(history_bars, frame.height)
    ]
    targets = {}
    for variant in refinement_manifest():
        name = variant["id"]
        signals = _signals(frame, name)
        values = [0.0] * (history_bars - 1) + signals["value"].slice(
            history_bars - 1
        ).to_list()
        targets[name] = signals.with_columns(
            pl.Series("value", values, dtype=pl.Float64)
        )
    scenarios = []
    for fee, delay in ((0, 0), (10, 0), (30, 0), (30, 1)):
        config = BacktestConfig(initial_cash=100000.0, fee_bps=fee)
        rows = []
        for name, targets_frame in targets.items():
            signals = targets_frame.with_columns(
                pl.col("value").shift(delay).fill_null(0.0)
            )
            periods = run_vectorized_backtest(frame, signals, config).periods.slice(
                history_bars
            )
            metrics = oos_metrics(
                periods["strategy_return"].to_list(),
                periods["turnover"].to_list(),
                backtest_config=config,
            )
            cash_on_up_days = sum(
                up and before == 0 and after == 0
                for up, before, after in zip(
                    up_days,
                    periods["position_before_open"],
                    periods["position_after_open"],
                    strict=True,
                )
            )
            rows.append(
                {
                    "strategy": name,
                    "metrics": asdict(metrics),
                    "exposure_fraction": sum(periods["position_after_open"])
                    / periods.height,
                    "market_up_days": sum(up_days),
                    "flat_up_day_fraction": cash_on_up_days / sum(up_days)
                    if any(up_days)
                    else None,
                    "periods": [
                        {
                            "timestamp": times[index + history_bars],
                            "strategy_return": row["strategy_return"],
                            "turnover": row["turnover"],
                            "equity": row["equity"],
                            "position_before_open": row["position_before_open"],
                            "position_after_open": row["position_after_open"],
                        }
                        for index, row in enumerate(periods.iter_rows(named=True))
                    ],
                }
            )
        scenarios.append(
            {"fee_bps": fee, "extra_delay_bars": delay, "strategies": rows}
        )
    return {
        "schema_version": "fixed-trend-refinement-v1",
        "symbol": str(frame["symbol"][0]),
        "dataset_hash": logical_content_hash(frame),
        "history_bars": history_bars,
        "observations": frame.height,
        "evaluation_start": times[history_bars],
        "evaluation_end": times[-1],
        "frozen_variants": refinement_manifest(),
        "scenarios": scenarios,
        "variant_evaluations": 10,
        "scenario_evaluations": 40,
        "validated_alpha": False,
        "live_eligible": False,
        "limitations": [
            "Fixed retrospective ablations; no parameter selection based on these outputs.",
            "Buffer rules retain state inside the band; no forced holding or minimum exposure.",
            "Only price returns, with operator-declared adjustment and no dividend ledger.",
            "Position-switch fees and one-bar delay are research stress, not calibrated execution.",
            "Cross-ETF observations can be correlated; no independent-alpha qualification.",
            "Terminal holdings marked to close without forced liquidation.",
        ],
    }
