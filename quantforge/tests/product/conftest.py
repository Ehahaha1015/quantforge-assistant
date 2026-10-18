from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from quantforge.data import BAR_SCHEMA
from quantforge.product.context import code_context
from quantforge.product.runner import ExperimentRunner, _timestamps
from quantforge.research import ResearchRequest, admit_research_request


@pytest.fixture
def research_host(tmp_path: Path) -> Any:
    runner = ExperimentRunner(tmp_path)
    start = datetime(2020, 1, 1, tzinfo=UTC)
    count = 126
    prices = [100.0 + i * 0.15 + (i % 11) * 0.3 for i in range(count)]
    frame = pl.DataFrame(
        {
            "symbol": ["SPY"] * count,
            "timestamp": [start + timedelta(days=i) for i in range(count)],
            "open": prices,
            "high": [p + 2 for p in prices],
            "low": [p - 2 for p in prices],
            "close": [p + 0.5 for p in prices],
            "volume": [1000.0] * count,
        },
        schema=BAR_SCHEMA,
    )
    snapshot = runner.catalog.write(
        "synthetic-spy",
        frame,
        source="synthetic-test",
        schema_version="bars-v1",
        as_of=start + timedelta(days=count),
    )
    proposal = {
        "schema_version": "sma-proposal-v1",
        "hypothesis": {
            "statement": "Fixed SMA test",
            "rationale": "Test causal trends",
            "falsification": "Reject if the declared screen fails",
        },
        "strategy": {
            "name": "moving_average_crossover",
            "short_window": 3,
            "long_window": 10,
            "position_rule": "long_when_short_above_long_else_flat",
        },
        "execution": {"timing": "next_bar_open", "terminal_liquidation": False},
        "backtest": {
            "initial_cash": 100000.0,
            "fee_bps": 10.0,
            "periods_per_year": 252,
        },
        "costs": {
            "model": "position_switch_fee_bps",
            "slippage": "not_modelled",
            "dividends": "not_modelled",
        },
        "split": {
            "observations": count,
            "warmup_bars": 12,
            "development_bars": 70,
            "validation_bars": 28,
            "test_bars": 28,
            "validation_window_bars": 7,
        },
        "parameter_optimization": "none",
        "random_seed": 0,
    }
    moments = _timestamps(frame)
    times = dict(
        zip(
            (
                "development_start",
                "development_end",
                "validation_start",
                "validation_end",
                "test_start",
                "test_end",
            ),
            [moments[i].isoformat() for i in (0, 69, 70, 97, 98, 125)],
            strict=True,
        )
    )
    request = ResearchRequest(
        schema_version="research-request-v1",
        experiment_id="dev-001",
        goal_text="Test a fixed moving-average baseline",
        submitted_at=start,
        symbol="SPY",
        frequency="daily",
        constraints={"max_drawdown_loss_magnitude": 0.15},
        provenance={"source": "scripted", "reference": "unit-fixture"},
    )
    spec = admit_research_request(
        request,
        proposal=proposal,
        dataset_snapshot=snapshot,
        data_conventions={
            "timestamp": "UTC date",
            "session": "synthetic daily",
            "adjustment": "none",
            "vintage": "synthetic fixture",
        },
        code_context=code_context(),
        split_timestamps=times,
    )
    yield runner, spec, frame
    runner.close()
