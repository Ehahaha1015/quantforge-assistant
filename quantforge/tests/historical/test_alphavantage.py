from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from quantforge.data import ParquetDataCatalog
from quantforge.historical.alphavantage import (
    HistoricalDataError,
    download_snapshot,
    parse_public_demo,
)


def document() -> dict[str, Any]:
    latest = datetime(2025, 10, 8, tzinfo=UTC)
    return {
        "Meta Data": {
            "2. Symbol": "IBM",
            "3. Last Refreshed": "2025-10-08",
            "4. Output Size": "Compact",
            "5. Time Zone": "US/Eastern",
        },
        "Time Series (Daily)": {
            (latest - timedelta(days=i)).date().isoformat(): {
                "1. open": "100",
                "2. high": "102",
                "3. low": "99",
                "4. close": "101",
                "5. volume": "1000",
            }
            for i in range(100)
        },
    }


def test_provider_conversion_qualifies_and_reopens_immutable_snapshot(
    tmp_path: Path,
) -> None:
    raw = json.dumps(document()).encode()
    at = datetime(2025, 10, 10, tzinfo=UTC)
    frame, qualification = parse_public_demo(raw, retrieved_at=at)
    assert frame.height == 100
    assert frame["timestamp"].cast(int).is_sorted()
    assert qualification["adjustment"].startswith("raw as-traded")
    assert "not an observed intraday timestamp" in qualification["timestamp_semantics"]
    snapshot = download_snapshot(tmp_path, retrieved_at=at, fetcher=lambda: raw)
    assert (
        ParquetDataCatalog(tmp_path / "catalog").verify(snapshot.dataset_id) == snapshot
    )
    preserved = tmp_path / "qualifications" / snapshot.dataset_id
    assert (preserved / "provider-response.json").read_bytes() == raw
    assert (
        json.loads((preserved / "qualification.json").read_text())["dataset_snapshot"][
            "content_hash"
        ]
        == snapshot.content_hash
    )
    with pytest.raises(HistoricalDataError, match="retained"):
        download_snapshot(tmp_path, retrieved_at=at, fetcher=lambda: raw)


@pytest.mark.parametrize(
    "alteration",
    [
        "provider_error",
        "wrong_symbol",
        "short_series",
        "unordered",
        "nan",
        "bad_ohlc",
        "wrong_refresh",
    ],
)
def test_rejects_unqualified_provider_response(alteration: str) -> None:
    value = document()
    series = value["Time Series (Daily)"]
    if alteration == "provider_error":
        value = {"Information": "quota exceeded"}
    elif alteration == "wrong_symbol":
        value["Meta Data"]["2. Symbol"] = "FAKE"
    elif alteration == "short_series":
        series.pop(next(iter(series)))
    elif alteration == "unordered":
        value["Time Series (Daily)"] = dict(reversed(list(series.items())))
    elif alteration == "nan":
        series[next(iter(series))]["4. close"] = "NaN"
    elif alteration == "bad_ohlc":
        series[next(iter(series))]["2. high"] = "90"
    elif alteration == "wrong_refresh":
        value["Meta Data"]["3. Last Refreshed"] = "2025-10-07"
    with pytest.raises(HistoricalDataError):
        parse_public_demo(
            json.dumps(value).encode(), retrieved_at=datetime(2025, 10, 10, tzinfo=UTC)
        )


def test_rejects_duplicate_provider_json_and_incomplete_current_session() -> None:
    raw = json.dumps(document()).encode()
    duplicated = raw.replace(
        b'"2. Symbol": "IBM"', b'"2. Symbol": "IBM", "2. Symbol": "IBM"'
    )
    with pytest.raises(HistoricalDataError, match="duplicate"):
        parse_public_demo(duplicated, retrieved_at=datetime(2025, 10, 10, tzinfo=UTC))
    with pytest.raises(HistoricalDataError, match="incomplete"):
        parse_public_demo(raw, retrieved_at=datetime(2025, 10, 8, 23, tzinfo=UTC))
