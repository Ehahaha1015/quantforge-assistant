import json
from datetime import UTC, datetime

import polars as pl
import pytest
from pydantic import ValidationError

from quantforge.data import DatasetSnapshot, ParquetDataCatalog

AS_OF = datetime(2025, 1, 4, tzinfo=UTC)


def bar_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["MSFT", "AAPL", "AAPL"],
            "timestamp": [
                datetime(2025, 1, 2, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
                datetime(2025, 1, 1, tzinfo=UTC),
            ],
            "open": [20.0, 11.0, 10.0],
            "high": [22.0, 13.0, 12.0],
            "low": [19.0, 10.0, 9.0],
            "close": [21.0, 12.0, 11.0],
            "volume": [200.0, 110.0, 100.0],
        }
    )


def write_dataset(catalog: ParquetDataCatalog, dataset_id: str = "bars.001", **kwargs: object) -> DatasetSnapshot:
    arguments = {"source": "unit-test", "schema_version": "1", "as_of": AS_OF}
    arguments.update(kwargs)
    return catalog.write(dataset_id, bar_frame(), **arguments)


def test_write_creates_parquet_manifest_and_read_round_trip(tmp_path: object) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    snapshot = write_dataset(catalog)
    assert (tmp_path / "bars.001" / "bars.parquet").is_file()
    assert (tmp_path / "bars.001" / "manifest.json").is_file()
    assert catalog.read("bars.001").equals(bar_frame().sort(["symbol", "timestamp"]))
    assert catalog.read_snapshot("bars.001") == snapshot
    assert DatasetSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    assert snapshot.relative_path == "bars.001/bars.parquet"
    assert snapshot.row_count == 3
    assert snapshot.min_timestamp == datetime(2025, 1, 1, tzinfo=UTC)
    assert snapshot.max_timestamp == datetime(2025, 1, 2, tzinfo=UTC)


def test_manifest_contains_no_absolute_path(tmp_path: object) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    manifest = json.loads((tmp_path / "bars.001" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["relative_path"] == "bars.001/bars.parquet"
    assert str(tmp_path) not in json.dumps(manifest)


def test_snapshot_is_frozen_and_forbids_extra_fields(tmp_path: object) -> None:
    snapshot = write_dataset(ParquetDataCatalog(tmp_path))
    with pytest.raises(ValidationError):
        snapshot.row_count = 5
    with pytest.raises(ValidationError):
        DatasetSnapshot.model_validate({**snapshot.model_dump(), "extra": True})


def test_existing_dataset_requires_explicit_overwrite(tmp_path: object) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    first = write_dataset(catalog)
    with pytest.raises(FileExistsError):
        write_dataset(catalog)
    changed = bar_frame().with_columns(pl.col("close") + 0.5)
    second = catalog.write(
        "bars.001",
        changed,
        source="unit-test",
        schema_version="1",
        as_of=AS_OF,
        overwrite=True,
    )
    assert second.content_hash != first.content_hash
    assert catalog.read("bars.001")["close"].to_list() == [11.5, 12.5, 21.5]


@pytest.mark.parametrize("dataset_id", ["", "../secret", "a/b", "a\\b", "C:\\data", "..", "é"])
def test_invalid_dataset_ids_rejected(tmp_path: object, dataset_id: str) -> None:
    with pytest.raises(ValueError, match="dataset_id"):
        write_dataset(ParquetDataCatalog(tmp_path), dataset_id)


@pytest.mark.parametrize("dataset_id", ["aapl-daily-v1", "us_equities_2025", "bars.001"])
def test_valid_dataset_ids_accepted(tmp_path: object, dataset_id: str) -> None:
    assert write_dataset(ParquetDataCatalog(tmp_path), dataset_id).dataset_id == dataset_id


def test_query_symbol_and_multi_symbol_filters(tmp_path: object) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    one = catalog.query("bars.001", symbols=["AAPL"])
    both = catalog.query("bars.001", symbols=["MSFT", "AAPL"])
    assert one["symbol"].to_list() == ["AAPL", "AAPL"]
    assert both.height == 3


def test_query_start_end_and_projection(tmp_path: object) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    result = catalog.query(
        "bars.001",
        start=datetime(2025, 1, 2, tzinfo=UTC),
        end=datetime(2025, 1, 2, tzinfo=UTC),
        columns=["symbol", "close"],
    )
    assert isinstance(result, pl.DataFrame)
    assert result.columns == ["symbol", "close"]
    assert result.rows() == [("AAPL", 12.0), ("MSFT", 21.0)]


def test_query_invalid_projection_rejected(tmp_path: object) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    with pytest.raises(ValueError, match="invalid requested"):
        catalog.query("bars.001", columns=["symbol", "not_a_column"])


def test_query_values_are_parameterized(tmp_path: object) -> None:
    frame = bar_frame().with_columns(
        pl.when(pl.col("symbol") == "MSFT").then(pl.lit("O'Reilly")).otherwise(pl.col("symbol")).alias("symbol")
    )
    catalog = ParquetDataCatalog(tmp_path)
    catalog.write("quoted", frame, source="test", schema_version="1", as_of=AS_OF)
    assert catalog.query("quoted", symbols=["O'Reilly"])["symbol"].to_list() == ["O'Reilly"]


def test_query_result_order_is_deterministic(tmp_path: object) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    result = catalog.query("bars.001")
    keys = result.select("symbol", pl.col("timestamp").cast(pl.Int64)).rows()
    assert keys == sorted(keys)


def test_query_rejects_naive_filter_timestamp(tmp_path: object) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    with pytest.raises(ValueError, match="timezone-aware"):
        catalog.query("bars.001", start=datetime.fromisoformat("2025-01-01"))


def test_query_empty_symbol_filter_returns_typed_empty_frame(tmp_path: object) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    result = catalog.query("bars.001", symbols=[])
    assert result.is_empty()
    assert result.schema == catalog.read("bars.001").schema
