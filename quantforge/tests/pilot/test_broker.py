from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_service import SYMBOL, _prepare

from quantforge.product.pilot.broker import (
    BrokerError,
    broker_reconcile,
    broker_status,
    import_broker_fills,
    open_broker_account,
)

AS_OF = datetime(2026, 10, 11, tzinfo=UTC)

FILL_HEADER = "date,side,quantity,price,fee,symbol,note\n"
FILL_ROWS = (
    f"2025-01-04,BUY,200,2.21,5.02,{SYMBOL},app screenshot\n"
    f"2025-01-08,SELL,200,1.79,5.20,{SYMBOL},app screenshot\n"
)


def _csv(tmp_path: Path, rows: str, name: str = "fills.csv") -> Path:
    path = tmp_path / name
    path.write_text(FILL_HEADER + rows, encoding="utf-8")
    return path


def _paper_root(tmp_path: Path) -> Path:
    service, _ = _prepare(tmp_path)
    service.run(steps=0)
    service.close()
    return tmp_path / "pilot"


def test_broker_ledger_requires_open_and_rejects_duplicates(tmp_path: Path) -> None:
    root = _paper_root(tmp_path)
    with pytest.raises(BrokerError, match="BROKER_NOT_OPENED"):
        import_broker_fills(
            root, _csv(tmp_path, FILL_ROWS), declared_source="demo", as_of=AS_OF
        )
    with pytest.raises(BrokerError, match="BROKER_NOT_OPENED"):
        broker_status(root)
    opened = open_broker_account(root, cash="500.00", declared_source="bank transfer")
    assert opened["cash"] == "500.00"
    again = open_broker_account(root, cash="500.00", declared_source="bank transfer")
    assert again == opened
    with pytest.raises(BrokerError, match="BROKER_ALREADY_OPEN"):
        open_broker_account(root, cash="600.00", declared_source="bank transfer")
    imported = import_broker_fills(
        root,
        _csv(tmp_path, FILL_ROWS),
        declared_source="broker app screenshot",
        as_of=AS_OF,
    )
    assert imported["appended"] == 2
    with pytest.raises(BrokerError, match="BROKER_FILL_DUPLICATE"):
        import_broker_fills(
            root,
            _csv(tmp_path, FILL_ROWS, "again.csv"),
            declared_source="dup",
            as_of=AS_OF,
        )
    status = broker_status(root)
    assert Decimal(status["cash"]) == Decimal("405.78")
    assert Decimal(status["cumulative_fees"]) == Decimal("10.22")
    assert status["positions"] == {}
    assert status["fills"] == 2


def test_broker_import_rejects_malformed_reports(tmp_path: Path) -> None:
    root = _paper_root(tmp_path)
    open_broker_account(root, cash="500.00", declared_source="bank transfer")
    for rows, message in (
        ("2025-01-04,HOLD,200,2.21,5.02,,\n", "side"),
        ("2025-01-04,BUY,0,2.21,5.02,,\n", "positive"),
        ("2025-01-04,BUY,200,-1,5.02,,\n", "positive"),
        ("04/01/2025,BUY,200,2.21,5.02,,\n", "ISO"),
        (
            "2025-01-08,BUY,200,2.21,5.02,,\n2025-01-04,BUY,200,2.21,5.02,,\n",
            "nondecreasing",
        ),
    ):
        with pytest.raises(BrokerError, match=message):
            import_broker_fills(
                root,
                _csv(tmp_path, rows, "bad.csv"),
                declared_source="bad",
                as_of=AS_OF,
            )


def test_reconcile_displays_every_difference_without_smoothing(tmp_path: Path) -> None:
    root = _paper_root(tmp_path)
    open_broker_account(root, cash="500.00", declared_source="bank transfer")
    import_broker_fills(
        root,
        _csv(tmp_path, FILL_ROWS),
        declared_source="broker app screenshot",
        as_of=AS_OF,
    )
    report = broker_reconcile(root)
    assert report["program_id"] == "pilot-test"
    assert report["paper"]["fills"] == 2
    assert Decimal(report["paper"]["cash"]) == Decimal("409.82")
    assert Decimal(report["broker"]["cash"]) == Decimal("405.78")
    assert Decimal(report["cash_delta"]) == Decimal("-4.04")
    kinds = [(entry["kind"], entry["side"]) for entry in report["differences"]]
    assert kinds.count(("FEE_DELTA", "BUY")) == 1
    assert kinds.count(("FEE_DELTA", "SELL")) == 1
    assert kinds.count(("PRICE_DELTA", "BUY")) == 1
    assert kinds.count(("PRICE_DELTA", "SELL")) == 1
    # Numerically equal quantities ("2E+2" vs "200") are never a difference.
    assert all(entry["kind"] != "QUANTITY_DELTA" for entry in report["differences"])
    assert "never smoothed" in report["scope"]


def test_reconcile_flags_an_unexecuted_paper_side(tmp_path: Path) -> None:
    root = _paper_root(tmp_path)
    open_broker_account(root, cash="500.00", declared_source="bank transfer")
    only_buy = f"2025-01-04,BUY,200,2.21,5.02,{SYMBOL},partial\n"
    import_broker_fills(
        root,
        _csv(tmp_path, only_buy, "partial.csv"),
        declared_source="partial",
        as_of=AS_OF,
    )
    report = broker_reconcile(root)
    mismatch = [
        entry for entry in report["differences"] if entry["kind"] == "SIDE_MISMATCH"
    ]
    assert {entry["side"] for entry in mismatch} == {"SELL"}
    assert report["broker"]["positions"] == {SYMBOL: "200"}


def test_broker_journal_is_append_only_and_canonical(tmp_path: Path) -> None:
    root = _paper_root(tmp_path)
    open_broker_account(root, cash="500.00", declared_source="bank transfer")
    import_broker_fills(
        root,
        _csv(tmp_path, FILL_ROWS),
        declared_source="broker app screenshot",
        as_of=AS_OF,
    )
    import sqlite3

    connection = sqlite3.connect(root / "broker.sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("UPDATE events SET payload='{}' WHERE seq=1")
    connection.close()
    status = broker_status(root)
    assert status["declared_source"] == "bank transfer"
