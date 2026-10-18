import math
from datetime import UTC, datetime, timedelta, timezone

import polars as pl
import pytest

from quantforge.data import BAR_COLUMNS, BAR_SCHEMA, canonicalize_bars, prepare_bars

AS_OF = datetime(2025, 1, 3, tzinfo=UTC)


def bar_frame(**overrides: object) -> pl.DataFrame:
    data: dict[str, object] = {
        "close": [21.0, 11.0],
        "symbol": [" MSFT ", "AAPL"],
        "volume": [200.0, 100.0],
        "timestamp": [
            datetime(2025, 1, 2, tzinfo=UTC),
            datetime(2025, 1, 1, tzinfo=UTC),
        ],
        "low": [19.0, 9.0],
        "open": [20.0, 10.0],
        "high": [22.0, 12.0],
    }
    data.update(overrides)
    return pl.DataFrame(data)


def test_valid_frame_has_canonical_columns_types_order_and_symbols() -> None:
    result = prepare_bars(bar_frame(), as_of=AS_OF)
    assert result.columns == list(BAR_COLUMNS)
    assert result.schema == BAR_SCHEMA
    assert result["symbol"].to_list() == ["AAPL", "MSFT"]
    assert result["timestamp"].cast(pl.Int64).to_list() == [
        int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1_000_000),
        int(datetime(2025, 1, 2, tzinfo=UTC).timestamp() * 1_000_000),
    ]


@pytest.mark.parametrize("column", BAR_COLUMNS)
def test_missing_column_rejected(column: str) -> None:
    with pytest.raises(ValueError, match="missing"):
        canonicalize_bars(bar_frame().drop(column))


def test_extra_column_rejected() -> None:
    with pytest.raises(ValueError, match="extra"):
        canonicalize_bars(bar_frame().with_columns(pl.lit(1).alias("extra")))


def test_empty_trimmed_symbol_rejected() -> None:
    with pytest.raises(ValueError, match="symbol"):
        prepare_bars(bar_frame(symbol=[" ", "AAPL"]), as_of=AS_OF)


def test_non_string_symbol_rejected() -> None:
    with pytest.raises(ValueError, match="String"):
        canonicalize_bars(bar_frame(symbol=[1, 2]))


def test_naive_timestamp_column_rejected() -> None:
    timestamps = [datetime.fromisoformat("2025-01-02"), datetime.fromisoformat("2025-01-01")]
    with pytest.raises(ValueError, match="timezone-aware"):
        canonicalize_bars(bar_frame(timestamp=timestamps))


def test_non_utc_timestamp_normalizes_to_utc() -> None:
    timestamps = pl.Series("timestamp", [datetime.fromisoformat("2025-01-02T08:00:00"), datetime.fromisoformat("2025-01-01T08:00:00")]).dt.replace_time_zone(
        "Asia/Shanghai"
    )
    result = canonicalize_bars(bar_frame(timestamp=timestamps))
    assert result.schema["timestamp"] == pl.Datetime("us", "UTC")
    expected = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1_000_000)
    assert result["timestamp"].cast(pl.Int64).to_list()[0] == expected


@pytest.mark.parametrize("time_unit", ["ns", "ms"])
def test_non_microsecond_timestamp_unit_rejected(time_unit: str) -> None:
    timestamps = pl.Series(
        "timestamp",
        [datetime(2025, 1, 3, tzinfo=UTC), datetime(2025, 1, 2, tzinfo=UTC)],
        dtype=pl.Datetime(time_unit, "UTC"),
    )
    with pytest.raises(ValueError, match="microsecond"):
        canonicalize_bars(bar_frame(timestamp=timestamps))


def test_nanosecond_future_timestamp_cannot_pass_as_of() -> None:
    timestamp = pl.Series(
        "timestamp",
        [1735862400000000500, 1735776000000000000],
        dtype=pl.Datetime("ns", "UTC"),
    )
    with pytest.raises(ValueError, match="microsecond"):
        prepare_bars(bar_frame(timestamp=timestamp), as_of=AS_OF)


def test_negative_zero_canonicalizes_to_positive_zero() -> None:
    result = prepare_bars(bar_frame(volume=[-0.0, 0.0]), as_of=AS_OF)
    assert all(math.copysign(1.0, value) == 1.0 for value in result["volume"])


def test_boolean_numeric_data_rejected() -> None:
    with pytest.raises(ValueError, match="non-boolean"):
        canonicalize_bars(bar_frame(volume=[True, False]))


def test_integer_not_exactly_representable_as_float64_rejected() -> None:
    with pytest.raises(ValueError, match="exactly"):
        canonicalize_bars(bar_frame(volume=[2**53 + 1, 1]))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_value_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        prepare_bars(bar_frame(volume=[value, 1.0]), as_of=AS_OF)


def test_null_rejected() -> None:
    with pytest.raises(ValueError, match="null"):
        prepare_bars(bar_frame(volume=[None, 1.0]), as_of=AS_OF)


@pytest.mark.parametrize("column", ["open", "high", "low", "close"])
@pytest.mark.parametrize("value", [0.0, -1.0])
def test_non_positive_price_rejected(column: str, value: float) -> None:
    with pytest.raises(ValueError, match="prices"):
        prepare_bars(bar_frame(**{column: [value, 10.0]}), as_of=AS_OF)


def test_negative_volume_rejected() -> None:
    with pytest.raises(ValueError, match="volume"):
        prepare_bars(bar_frame(volume=[-1.0, 1.0]), as_of=AS_OF)


@pytest.mark.parametrize(
    ("column", "value"),
    [("low", 23.0), ("open", 18.0), ("open", 23.0), ("close", 18.0), ("close", 23.0)],
)
def test_malformed_ohlc_rejected(column: str, value: float) -> None:
    with pytest.raises(ValueError, match="OHLC"):
        prepare_bars(bar_frame(**{column: [value, 10.0]}), as_of=AS_OF)


def test_duplicate_market_key_rejected() -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="duplicate"):
        prepare_bars(bar_frame(symbol=["AAPL", "AAPL"], timestamp=[timestamp, timestamp]), as_of=AS_OF)


def test_same_timestamp_for_different_symbols_accepted() -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    result = prepare_bars(bar_frame(timestamp=[timestamp, timestamp]), as_of=AS_OF)
    assert result.height == 2


def test_timestamp_after_as_of_rejected() -> None:
    with pytest.raises(ValueError, match="after as_of"):
        prepare_bars(bar_frame(), as_of=datetime(2025, 1, 1, tzinfo=UTC))


def test_timestamp_equal_to_as_of_accepted() -> None:
    result = prepare_bars(bar_frame(), as_of=datetime(2025, 1, 2, tzinfo=UTC))
    assert result.height == 2


def test_naive_as_of_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        prepare_bars(bar_frame(), as_of=datetime.fromisoformat("2025-01-03"))


def test_non_utc_as_of_normalizes_for_comparison() -> None:
    as_of = datetime(2025, 1, 2, 8, tzinfo=timezone(timedelta(hours=8)))
    assert prepare_bars(bar_frame(), as_of=as_of).height == 2


def test_timestamp_gaps_are_allowed() -> None:
    timestamps = [datetime(2025, 1, 3, tzinfo=UTC), datetime(2020, 1, 1, tzinfo=UTC)]
    assert prepare_bars(bar_frame(timestamp=timestamps), as_of=AS_OF).height == 2
