"""Synthetic declarations only: no catalog, bars, executing code or OOS seal."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from quantforge.data import DatasetSnapshot
from quantforge.research import ResearchRequest


def request_fields() -> dict[str, Any]:
    return {
        "schema_version": "research-request-v1",
        "experiment_id": "research-SPY-sma20_50.001",
        "goal_text": "Propose a daily moving average baseline.",
        "submitted_at": datetime(2026, 10, 8, 1, 2, 3, tzinfo=UTC),
        "symbol": "SPY",
        "frequency": "daily",
        "constraints": {"max_drawdown_loss_magnitude": 0.15},
        "provenance": {"source": "llm", "reference": "synthetic-proposal-001"},
    }


def complete_inputs() -> dict[str, Any]:
    # Six declared endpoints deliberately do not prove 1,260 actual daily bars.
    timestamps = dict(
        zip(
            (
                "development_start",
                "development_end",
                "validation_start",
                "validation_end",
                "test_start",
                "test_end",
            ),
            (
                "2020-01-02T14:30:00Z",
                "2020-01-03T14:30:00Z",
                "2020-01-06T14:30:00Z",
                "2020-01-07T14:30:00Z",
                "2020-01-08T14:30:00Z",
                "2020-01-09T14:30:00Z",
            ),
            strict=True,
        )
    )
    return {
        "request": ResearchRequest(**request_fields()),
        "proposal": {
            "schema_version": "sma-proposal-v1",
            "hypothesis": {
                "statement": "SMA20/50 may identify persistent trends.",
                "rationale": "A fixed trailing comparison can be tested later.",
                "falsification": "The fixed screen may fail in later evaluation.",
            },
            "strategy": {
                "name": "moving_average_crossover",
                "short_window": 20,
                "long_window": 50,
                "position_rule": "long_when_short_above_long_else_flat",
            },
            "execution": {"timing": "next_bar_open", "terminal_liquidation": False},
            "backtest": {
                "initial_cash": 100_000.0,
                "fee_bps": 10.0,
                "periods_per_year": 252,
            },
            "costs": {
                "model": "position_switch_fee_bps",
                "slippage": "not_modelled",
                "dividends": "not_modelled",
            },
            "split": {
                "observations": 1260,
                "warmup_bars": 50,
                "development_bars": 756,
                "validation_bars": 252,
                "test_bars": 252,
                "validation_window_bars": 63,
            },
            "parameter_optimization": "none",
            "random_seed": 0,
        },
        "dataset_snapshot": DatasetSnapshot(
            dataset_id="synthetic-spy-daily",
            source="host-declared-synthetic",
            schema_version="bars-v1",
            row_count=1260,
            min_timestamp=datetime.fromisoformat(timestamps["development_start"]),
            max_timestamp=datetime.fromisoformat(timestamps["test_end"]),
            as_of=datetime(2020, 1, 10, 14, 30, tzinfo=UTC),
            content_hash="b" * 64,
            relative_path="research-not-a-real-dataset/bars.parquet",
        ),
        "data_conventions": {
            "timestamp": "Declared session open in UTC",
            "session": "Host-declared US regular session",
            "adjustment": "Host-declared unadjusted prices",
            "vintage": "Host-declared synthetic vintage",
        },
        "code_context": {
            "git_commit": "a" * 40,
            "lockfile_sha256": "c" * 64,
            "runtime": {
                "python": "3.12.10",
                "polars": "1.44.2",
                "pydantic": "2.12.5",
                "duckdb": "1.5.5",
            },
        },
        "split_timestamps": timestamps,
    }


def set_field(payload: dict[str, Any], path: str, value: Any) -> None:
    """Replace one declared field in a fresh test payload."""
    keys = path.split(".")
    container = payload
    for key in keys[:-1]:
        container = container[key]
    container[keys[-1]] = value


def utc_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(parsed)
    return parsed.astimezone(UTC)
