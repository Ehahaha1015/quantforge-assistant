from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantforge.product.pilot.data import (
    PilotDataError,
    corpus_snapshot,
    extend_bars_csv,
    import_bars_csv,
    list_snapshots,
    read_snapshot,
)
from tests.scale.test_corpus import SYMBOLS, _bind_universe, _write_archive

AS_OF = datetime(2026, 10, 10, tzinfo=UTC)

CSV_HEADER = "date,open,high,low,close,volume\n"


def _write_csv(tmp_path: Path, text: str, name: str = "bars.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _import(tmp_path: Path, text: str, symbol: str = "SH512170"):
    return import_bars_csv(
        tmp_path / "root",
        _write_csv(tmp_path, text),
        symbol,
        as_of=AS_OF,
        declared_adjustment="operator declared: raw unadjusted CNY quotes",
    )


GOOD_ROWS = "".join(
    f"2025-01-0{day},0.8{day},0.9{day},0.7{day},0.8{day},100000\n"
    for day in range(1, 6)
)


def test_import_bars_csv_accepts_well_formed_operator_data_and_is_idempotent(
    tmp_path: Path,
) -> None:
    (tmp_path / "root").mkdir()
    document = _import(tmp_path, CSV_HEADER + GOOD_ROWS)
    assert document["schema"] == "cn-pilot-snapshot-v1"
    assert document["symbol"] == "SH512170"
    assert document["rows"] == 5
    assert document["min_session"] == "2025-01-01"
    assert document["max_session"] == "2025-01-05"
    assert document["source"]["kind"] == "operator-csv"
    assert document["source"]["declared_adjustment"].startswith("operator declared")
    assert len(document["content_hash"]) == 64
    again = _import(tmp_path, CSV_HEADER + GOOD_ROWS)
    assert again == document
    frame, stored = read_snapshot(tmp_path / "root", document["snapshot_id"])
    assert stored == document
    assert frame.height == 5
    assert Decimal(str(frame["close"][0])) == Decimal("0.81")
    assert [item["snapshot_id"] for item in list_snapshots(tmp_path / "root")] == [
        document["snapshot_id"]
    ]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            "2025-01-01,2.00,2.10,1.90,2.00,1000\n2025-01-01,2.00,2.10,1.90,2.00,1000\n2025-01-02,2.00,2.10,1.90,2.00,1000\n",
            "ascending",
        ),
        (
            "2025-01-02,2.00,2.10,1.90,2.00,1000\n2025-01-01,2.00,2.10,1.90,2.00,1000\n2025-01-03,2.00,2.10,1.90,2.00,1000\n",
            "ascending",
        ),
        (
            "2025-01-01,2.00,2.10,1.90,2.0001,1000\n2025-01-02,2.00,2.10,1.90,2.00,1000\n2025-01-03,2.00,2.10,1.90,2.00,1000\n",
            "tick",
        ),
        (
            "2025-01-01,2.00,2.10,1.90,-2.00,1000\n2025-01-02,2.00,2.10,1.90,2.00,1000\n2025-01-03,2.00,2.10,1.90,2.00,1000\n",
            "positive",
        ),
        (
            "01/01/2025,2.00,2.10,1.90,2.00,1000\n2025-01-02,2.00,2.10,1.90,2.00,1000\n2025-01-03,2.00,2.10,1.90,2.00,1000\n",
            "ISO",
        ),
        (
            "2025-01-01,2.00,2.10,1.90,2.00,1000\n2025-01-02,2.00,2.10,1.90,2.00,1000\n",
            "at least 3",
        ),
    ],
)
def test_import_bars_csv_fails_closed_on_malformed_rows(
    tmp_path: Path, rows: str, message: str
) -> None:
    (tmp_path / "root").mkdir()
    with pytest.raises(PilotDataError, match=message):
        _import(tmp_path, CSV_HEADER + rows)


def test_import_bars_csv_rejects_wrong_columns_and_symbol_mismatch(
    tmp_path: Path,
) -> None:
    (tmp_path / "root").mkdir()
    with pytest.raises(PilotDataError, match="columns"):
        _import(
            tmp_path,
            "date,open,close\n2025-01-01,2.00,2.00\n2025-01-02,2.00,2.00\n2025-01-03,2.00,2.00\n",
        )
    with pytest.raises(PilotDataError, match="symbol"):
        _import(
            tmp_path,
            "date,open,high,low,close,symbol\n2025-01-01,2.00,2.10,1.90,2.00,SH999999\n2025-01-02,2.00,2.10,1.90,2.00,SH999999\n2025-01-03,2.00,2.10,1.90,2.00,SH999999\n",
        )


def _provider_with_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored_close: float
):
    """Synthetic authenticated provider whose SH600008 close recovers to stored/factor."""
    import struct

    from quantforge.product.scale import corpus

    provider = tmp_path / "cn_data"
    (provider / "calendars").mkdir(parents=True)
    (provider / "instruments").mkdir()
    days = [
        "2025-12-24",
        "2025-12-25",
        "2025-12-26",
        "2025-12-29",
        "2025-12-30",
        "2025-12-31",
        "2026-09-15",
    ]
    calendar = ("\n".join(days) + "\n").encode()
    instruments = "".join(
        f"{symbol}\t2000-01-04\t2026-09-15\n" for symbol in SYMBOLS
    ).encode()
    (provider / "calendars/day.txt").write_bytes(calendar)
    (provider / "instruments/all.txt").write_bytes(instruments)
    for symbol in SYMBOLS:
        directory = provider / "features" / symbol.lower()
        directory.mkdir(parents=True)
        for field in corpus._FIELDS:
            if symbol == "SH600008" and field in ("open", "high", "low", "close"):
                value = {
                    "open": stored_close,
                    "high": stored_close + 0.01,
                    "low": stored_close - 0.01,
                    "close": stored_close,
                }[field]
                values = [value] * len(days)
            elif symbol == "SH600008" and field == "factor":
                values = [0.5] * len(days)
            elif field == "high":
                values = [101.0 + i for i in range(len(days))]
            elif field == "low":
                values = [99.0 + i for i in range(len(days))]
            elif field == "volume":
                values = [1000.0] * len(days)
            elif field == "factor":
                values = [0.1] * len(days)
            else:
                values = [100.0 + i for i in range(len(days))]
            (directory / f"{field}.day.bin").write_bytes(
                struct.pack("<" + "f" * (len(values) + 1), 0, *values)
            )
    members = [
        {"symbol": symbol, "source_start": "2000-01-04", "source_end": "2026-09-15"}
        for symbol in SYMBOLS
    ]
    universe = _bind_universe(
        {
            "schema": "research-scale-universe-precommit-v1",
            "program_id": "pilot-fixture",
            "observation_date": "2026-10-10",
            "provider_snapshot": corpus._SNAPSHOT,
            "membership_source": "cn_data/instruments/all.txt",
            "membership_source_sha256": hashlib.sha256(instruments).hexdigest(),
            "calendar_source_sha256": hashlib.sha256(calendar).hexdigest(),
            "start": "2025-12-24",
            "end": "2025-12-31",
            "selection_count": 5,
            "membership_rule": "fixed metadata-only lexicographic five A shares",
            "selection_channel_count": 2,
            "supersedes_metadata_only_proposal": "indices rejected before values",
            "replacement_policy": "no replacement",
            "strategy_class": "single-asset search fixed universe",
            "pit_classification": "RETROSPECTIVE_NOT_INSTITUTIONAL_PIT",
            "survivorship_classification": "AVAILABILITY_SELECTED_SURVIVOR_SUBSET",
            "limitations": ["survivorship/PIT unavailable"],
            "members": members,
        }
    )
    _write_archive(tmp_path, monkeypatch)
    return provider, universe


def test_corpus_bridge_recovers_two_decimal_cny_levels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, universe = _provider_with_prices(
        tmp_path, monkeypatch, stored_close=1.665
    )
    root = tmp_path / "pilot"
    root.mkdir()
    document = corpus_snapshot(root, provider, universe, "SH600008", as_of=AS_OF)
    assert document["source"]["kind"] == "corpus-raw-scale"
    assert document["source"]["universe_id"] == universe["universe_id"]
    assert document["rows"] == 6
    assert document["tick"] == "0.01"
    frame, stored = read_snapshot(root, document["snapshot_id"])
    assert stored == document
    assert Decimal(str(frame["close"][0])) == Decimal("3.33")  # 1.665 / 0.5
    assert Decimal(str(frame["high"][0])) == Decimal("3.35")
    assert Decimal(str(frame["low"][0])) == Decimal("3.31")
    assert document["min_session"] == "2025-12-24"
    assert document["max_session"] == "2025-12-31"


def test_corpus_bridge_rejects_prices_off_the_declared_stock_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, universe = _provider_with_prices(
        tmp_path, monkeypatch, stored_close=1.6625
    )
    root = tmp_path / "pilot"
    root.mkdir()
    with pytest.raises(PilotDataError, match="tick"):
        corpus_snapshot(root, provider, universe, "SH600008", as_of=AS_OF)


def test_corpus_bridge_rejects_undeclared_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, universe = _provider_with_prices(
        tmp_path, monkeypatch, stored_close=1.665
    )
    root = tmp_path / "pilot"
    root.mkdir()
    with pytest.raises(PilotDataError, match="universe"):
        corpus_snapshot(root, provider, universe, "SH999999", as_of=AS_OF)


def _rows(days: str, price: str = "0.8") -> str:
    return "".join(
        f"2025-01-{day},{price}0,0.99,0.69,0.8{day % 10},100000\n" for day in days
    )


def test_extend_bars_csv_appends_later_sessions_with_audited_revision(
    tmp_path: Path,
) -> None:
    (tmp_path / "root").mkdir()
    document = _import(tmp_path, CSV_HEADER + GOOD_ROWS)
    extra = (
        "2025-01-06,0.86,0.88,0.85,0.86,100000\n"
        "2025-01-07,0.87,0.89,0.86,0.87,100000\n"
        "2025-01-08,0.88,0.90,0.87,0.88,100000\n"
    )
    updated = extend_bars_csv(
        tmp_path / "root",
        document["snapshot_id"],
        _write_csv(tmp_path, CSV_HEADER + extra, "more.csv"),
        "SH512170",
        as_of=AS_OF,
        declared_adjustment="operator declared: raw unadjusted CNY quotes",
    )
    assert updated["rows"] == 8
    assert updated["max_session"] == "2025-01-08"
    assert updated["extensions"] == 1
    frame, stored = read_snapshot(tmp_path / "root", document["snapshot_id"])
    assert frame.height == 8
    assert stored["rows"] == 8 and stored["extensions"] == 1
    assert Decimal(str(frame["close"][7])) == Decimal("0.88")
    # A second extension builds on the first revision.
    second = extend_bars_csv(
        tmp_path / "root",
        document["snapshot_id"],
        _write_csv(
            tmp_path,
            CSV_HEADER
            + "2025-01-09,0.89,0.91,0.88,0.89,100000\n2025-01-10,0.90,0.92,0.89,0.90,100000\n2025-01-11,0.91,0.93,0.90,0.91,100000\n",
            "more2.csv",
        ),
        "SH512170",
        as_of=AS_OF,
        declared_adjustment="operator declared: raw unadjusted CNY quotes",
    )
    assert second["rows"] == 11 and second["extensions"] == 2


def test_extend_rejects_overlapping_or_earlier_rows(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    document = _import(tmp_path, CSV_HEADER + GOOD_ROWS)
    overlap = (
        "2025-01-05,0.85,0.87,0.84,0.85,100000\n"
        "2025-01-06,0.86,0.88,0.85,0.86,100000\n"
        "2025-01-07,0.87,0.89,0.86,0.87,100000\n"
    )
    with pytest.raises(PilotDataError, match="strictly later"):
        extend_bars_csv(
            tmp_path / "root",
            document["snapshot_id"],
            _write_csv(tmp_path, CSV_HEADER + overlap, "overlap.csv"),
            "SH512170",
            as_of=AS_OF,
            declared_adjustment="operator declared: raw unadjusted CNY quotes",
        )


def test_snapshot_prefix_tamper_after_extension_is_detected(tmp_path: Path) -> None:
    import polars as pl

    (tmp_path / "root").mkdir()
    document = _import(tmp_path, CSV_HEADER + GOOD_ROWS)
    extra = (
        "2025-01-06,0.86,0.88,0.85,0.86,100000\n"
        "2025-01-07,0.87,0.89,0.86,0.87,100000\n"
        "2025-01-08,0.88,0.90,0.87,0.88,100000\n"
    )
    extend_bars_csv(
        tmp_path / "root",
        document["snapshot_id"],
        _write_csv(tmp_path, CSV_HEADER + extra, "more.csv"),
        "SH512170",
        as_of=AS_OF,
        declared_adjustment="operator declared: raw unadjusted CNY quotes",
    )
    parquet = tmp_path / "root" / "data" / document["snapshot_id"] / "bars.parquet"
    frame = pl.read_parquet(parquet)
    forged = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit(9.99))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    forged.write_parquet(parquet)
    with pytest.raises(PilotDataError, match="prefix"):
        read_snapshot(tmp_path / "root", document["snapshot_id"])
