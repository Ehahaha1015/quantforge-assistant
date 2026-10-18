from datetime import UTC, datetime

import polars as pl
import pytest

from quantforge.data import (
    ParquetDataCatalog,
    canonicalize_bars,
    logical_content_hash,
    prepare_bars,
)

AS_OF = datetime(2025, 1, 3, tzinfo=UTC)


def bar_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["MSFT", "AAPL"],
            "timestamp": [datetime(2025, 1, 2, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)],
            "open": [20.0, 10.0],
            "high": [22.0, 12.0],
            "low": [19.0, 9.0],
            "close": [21.0, 11.0],
            "volume": [200.0, 100.0],
        }
    )


def canonical(frame: pl.DataFrame) -> pl.DataFrame:
    return prepare_bars(frame, as_of=AS_OF)


def test_same_data_and_shuffled_rows_have_same_hash() -> None:
    original = canonical(bar_frame())
    shuffled = original.reverse()
    assert logical_content_hash(original) == logical_content_hash(original)
    assert logical_content_hash(original) == logical_content_hash(shuffled)


def test_dataset_id_and_filesystem_path_do_not_affect_hash(tmp_path: object) -> None:
    first = ParquetDataCatalog(tmp_path / "one").write(
        "first", bar_frame(), source="test", schema_version="1", as_of=AS_OF
    )
    second = ParquetDataCatalog(tmp_path / "two").write(
        "second", bar_frame().reverse(), source="test", schema_version="1", as_of=AS_OF
    )
    assert first.content_hash == second.content_hash


def test_changed_price_changes_hash() -> None:
    changed = bar_frame().with_columns(
        pl.when(pl.col("symbol") == "AAPL").then(pl.col("close") + 0.25).otherwise(pl.col("close")).alias("close")
    )
    assert logical_content_hash(canonical(changed)) != logical_content_hash(canonical(bar_frame()))


def test_changed_timestamp_changes_hash() -> None:
    changed = bar_frame().with_columns(
        pl.when(pl.col("symbol") == "AAPL")
        .then(pl.col("timestamp") + pl.duration(hours=1))
        .otherwise(pl.col("timestamp"))
        .alias("timestamp")
    )
    assert logical_content_hash(canonical(changed)) != logical_content_hash(canonical(bar_frame()))


def test_negative_and_positive_zero_have_same_hash() -> None:
    negative = bar_frame().with_columns(pl.lit(-0.0).alias("volume"))
    positive = bar_frame().with_columns(pl.lit(0.0).alias("volume"))
    assert logical_content_hash(canonical(negative)) == logical_content_hash(canonical(positive))


def test_duplicate_market_keys_are_rejected_before_hashing() -> None:
    duplicate = canonicalize_bars(
        bar_frame().with_columns(
            pl.when(pl.col("symbol") == "MSFT").then(pl.lit("AAPL")).otherwise(pl.col("symbol")).alias("symbol"),
            pl.when(pl.col("symbol") == "MSFT")
            .then(pl.lit(datetime(2025, 1, 1, tzinfo=UTC)))
            .otherwise(pl.col("timestamp"))
            .alias("timestamp"),
        )
    )
    with pytest.raises(ValueError, match="duplicate"):
        logical_content_hash(duplicate)
