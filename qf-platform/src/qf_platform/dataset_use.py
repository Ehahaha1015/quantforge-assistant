"""Project-owned usage registry; model tools cannot select or reset its root."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl
from quantforge.data import logical_content_hash, prepare_bars
from quantforge.product.data_usage import DataUsageRegistry, DataWindow
from quantforge.product.storage import local_root

WORKSPACE = Path(__file__).resolve().parents[3]
REGISTRY_ROOT = WORKSPACE / "local-smoke/research-governance/data-usage"


def window_of(frame: pl.DataFrame) -> DataWindow:
    canonical = prepare_bars(frame, as_of=datetime.now(UTC))
    symbols = canonical["symbol"].unique().to_list()
    if len(symbols) != 1 or canonical.is_empty():
        raise ValueError("single-symbol source snapshot required")
    symbol = str(symbols[0]).strip().upper()
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    dates = [
        (epoch + timedelta(microseconds=int(value))).date()
        for value in canonical["timestamp"].cast(pl.Int64)
    ]
    return DataWindow(
        "CN:" + symbol
        if symbol.isdigit() and len(symbol) == 6
        else "DECLARED:" + symbol,
        dates[0],
        dates[-1],
        logical_content_hash(canonical),
        cohort="CN_EQUITY_ETF_DECLARED" if symbol.isdigit() else "UNVERIFIED",
    )


def register_research_use(
    frame: pl.DataFrame,
    *,
    program: str,
    reference_hash: str,
    purpose: Literal["DEVELOPMENT", "SELECTION"] = "DEVELOPMENT",
    registry_root: Path | None = None,
) -> dict[str, Any]:
    root = registry_root if registry_root is not None else REGISTRY_ROOT
    root.mkdir(parents=True, exist_ok=True)
    registry = DataUsageRegistry(local_root(root))
    return registry.record_use(
        window_of(frame),
        purpose=purpose,
        program=program,
        reference_hash=reference_hash,
    )
