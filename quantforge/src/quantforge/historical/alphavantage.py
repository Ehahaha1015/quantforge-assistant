"""Small official public-demo path: 100 raw IBM daily observations.

This deliberately supports one documented public endpoint, without private
credentials or a universal provider framework. It proves authentic historical
ingestion; the short, single-name sample cannot establish alpha.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import polars as pl

from quantforge.data import (
    BAR_SCHEMA,
    DatasetSnapshot,
    ParquetDataCatalog,
    prepare_bars,
)
from quantforge.experiments import canonical_json
from quantforge.product.storage import local_root, write_exclusive, writer_lock

_URL = "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM&apikey=demo"
_MAX_BYTES = 2_000_000
_DOCUMENTATION = "https://www.alphavantage.co/documentation/#daily"


class HistoricalDataError(ValueError):
    """The external response does not establish a qualified snapshot."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HistoricalDataError("duplicate provider JSON key")
        value[key] = item
    return value


def parse_public_demo(
    raw: bytes, *, retrieved_at: datetime
) -> tuple[pl.DataFrame, dict[str, Any]]:
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise HistoricalDataError("aware retrieval timestamp required")
    retrieved_at = retrieved_at.astimezone(UTC)
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_BYTES:
        raise HistoricalDataError("invalid provider response size")
    try:
        payload = json.loads(raw, object_pairs_hook=_pairs)
        if not isinstance(payload, dict) or set(payload) != {
            "Meta Data",
            "Time Series (Daily)",
        }:
            # A Note/Information/Error Message is a failure, never market data.
            raise HistoricalDataError("provider did not return daily observations")
        meta = payload["Meta Data"]
        if (
            meta["2. Symbol"] != "IBM"
            or meta["4. Output Size"] != "Compact"
            or meta["5. Time Zone"] not in ("US/Eastern", "America/New_York")
        ):
            raise HistoricalDataError("provider identity/timezone mismatch")
        series = payload["Time Series (Daily)"]
        if not isinstance(series, dict) or len(series) != 100:
            raise HistoricalDataError(
                "documented compact response requires 100 observations"
            )
        days = [date.fromisoformat(key) for key in series]
        if any(a <= b for a, b in pairwise(days)):
            raise HistoricalDataError("provider dates must be strictly descending")
        if meta["3. Last Refreshed"] != max(series):
            raise HistoricalDataError("provider refreshed-date mismatch")
        # Require at least a full UTC day to have elapsed after each session label.
        # This conservatively excludes a possibly incomplete current-date bar.
        if max(days) >= retrieved_at.date():
            raise HistoricalDataError("possibly incomplete current/future session")
        records: list[dict[str, Any]] = []
        for key, values in series.items():
            fields = {"1. open", "2. high", "3. low", "4. close", "5. volume"}
            if not isinstance(values, dict) or set(values) != fields:
                raise HistoricalDataError("provider OHLCV schema mismatch")
            numbers = {
                name: float(values[f"{i}. {name}"])
                for i, name in enumerate(
                    ("open", "high", "low", "close", "volume"), start=1
                )
            }
            if any(not math.isfinite(v) for v in numbers.values()):
                raise HistoricalDataError("nonfinite provider observation")
            records.append(
                {
                    "symbol": "IBM",
                    "timestamp": datetime.combine(
                        date.fromisoformat(key), datetime.min.time(), tzinfo=UTC
                    ),
                    **numbers,
                }
            )
        frame = prepare_bars(
            pl.DataFrame(records, schema=BAR_SCHEMA), as_of=retrieved_at
        )
        observed = set(days)
        cursor, last = min(days), max(days)
        absent_weekdays: list[str] = []
        while cursor <= last:
            if cursor.weekday() < 5 and cursor not in observed:
                absent_weekdays.append(cursor.isoformat())
            cursor += timedelta(days=1)
        qualification = {
            "schema_version": "historical-qualification-v1",
            "provider": "Alpha Vantage",
            "provider_tier": "documented public IBM demo",
            "documentation": _DOCUMENTATION,
            "request": {
                "function": "TIME_SERIES_DAILY",
                "symbol": "IBM",
                "outputsize": "compact (provider default)",
                "credential": "public documentation demo token",
            },
            "retrieved_at": retrieved_at.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "row_count": frame.height,
            "time_bounds": {
                "start": min(days).isoformat(),
                "end": max(days).isoformat(),
            },
            "provider_timezone": meta["5. Time Zone"],
            "timestamp_semantics": "US session date encoded as UTC 00:00 label, not an observed intraday timestamp",
            "adjustment": "raw as-traded daily OHLCV per official TIME_SERIES_DAILY documentation",
            "pagination": "single complete compact response, 100 bars; no pagination for this request",
            "checks": {
                "schema": "PASS",
                "finite_ohlcv": "PASS",
                "duplicate_dates": "NONE",
                "provider_monotonicity": "STRICT_DESCENDING",
                "canonical_monotonicity": "STRICT_ASCENDING",
                "possibly_missing_weekdays": absent_weekdays,
            },
            "limitations": [
                "100 provider-supplied historical observations for IBM only; no survivorship-free or point-in-time universe claim.",
                "Weekday gaps may be exchange holidays or missing data; completeness is not certified against an exchange calendar.",
                "Raw prices may contain corporate-action discontinuities; dividends and calibrated trading costs are not modelled.",
                "Short sample, one vendor vintage; unsuitable for profitability/production-validation claims.",
                "Public documentation demo access observed; redistribution/commercial entitlements are not established. Data artifacts are local only.",
            ],
        }
        return frame, qualification
    except HistoricalDataError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise HistoricalDataError("invalid historical provider document") from None


def download_snapshot(
    root: str | Path,
    *,
    retrieved_at: datetime,
    fetcher: Callable[[], bytes],
) -> DatasetSnapshot:
    """Retain response and qualification with an immutable native data snapshot."""
    root = local_root(root)
    raw = fetcher()
    frame, qualification = parse_public_demo(raw, retrieved_at=retrieved_at)
    dataset_id = "av-ibm-" + qualification["response_sha256"][:24]
    for name in ("catalog", "qualifications"):
        (root / name).mkdir(exist_ok=True)
        local_root(root / name)
    with writer_lock(root):
        catalog = ParquetDataCatalog(root / "catalog")
        artifact = root / "qualifications" / dataset_id
        if artifact.exists() or (catalog.root / dataset_id).exists():
            raise HistoricalDataError(
                "snapshot already retained; use its immutable identifier"
            )
        artifact.mkdir()
        with (artifact / "provider-response.json").open("xb") as stream:
            stream.write(raw)
            stream.flush()
            import os

            os.fsync(stream.fileno())
        snapshot = catalog.write(
            dataset_id,
            frame,
            source="alphavantage-public-demo-TIME_SERIES_DAILY",
            schema_version="bars-v1",
            as_of=retrieved_at,
        )
        qualification["dataset_snapshot"] = snapshot.model_dump(mode="json")
        write_exclusive(artifact / "qualification.json", qualification)
        if catalog.verify(dataset_id) != snapshot:
            raise HistoricalDataError("retained snapshot readback mismatch")
        canonical_json(qualification)  # validates finite machine ABI
        return snapshot
