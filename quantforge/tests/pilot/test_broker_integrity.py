from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import localcontext
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


def _csv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "observations.csv"
    path.write_text(text, encoding="utf-8")
    return path


def _import(root: Path, path: Path) -> None:
    import_broker_fills(root, path, declared_source="test observation", as_of=AS_OF)


def test_numeric_reexport_duplicate_is_rejected(tmp_path: Path) -> None:
    open_broker_account(tmp_path, cash="100.00", declared_source="test")
    _import(
        tmp_path,
        _csv(
            tmp_path,
            f"date,side,quantity,price,fee,symbol\n2026-10-09,BUY,100,0.9,5,{SYMBOL}\n",
        ),
    )
    with pytest.raises(BrokerError, match="DUPLICATE"):
        _import(
            tmp_path,
            _csv(
                tmp_path,
                f"date,side,quantity,price,fee,symbol\n2026-10-09,BUY,1E+2,0.900,5.00,{SYMBOL}\n",
            ),
        )
    assert broker_status(tmp_path)["fills"] == 1


def test_trade_ids_distinguish_identical_fills_but_cannot_be_reused(
    tmp_path: Path,
) -> None:
    open_broker_account(tmp_path, cash="500.00", declared_source="test")
    header = "date,side,quantity,price,fee,symbol,trade_id\n"
    row = f"2026-10-09,BUY,100,0.9,5,{SYMBOL},"
    _import(tmp_path, _csv(tmp_path, header + row + "A\n" + row + "B\n"))
    assert broker_status(tmp_path)["fills"] == 2
    with pytest.raises(BrokerError, match="DUPLICATE"):
        _import(tmp_path, _csv(tmp_path, header + row + "A\n"))


@pytest.mark.parametrize(
    "text",
    [
        "date,side,quantity,price,fee,fee\n2026-10-09,BUY,100,0.9,5,5\n",
        "date,side,quantity,price,fee\n2026-10-09,BUY,100,0.9,5,EXTRA\n",
        "date,side,quantity,price,fee\n2026-10-09,BUY,100,0.9\n",
        "date,side,quantity,price,fee\n2026-10-12,BUY,100,0.9,5\n",
    ],
)
def test_malformed_or_future_rows_commit_nothing(tmp_path: Path, text: str) -> None:
    open_broker_account(tmp_path, cash="100.00", declared_source="test")
    with pytest.raises(BrokerError):
        _import(tmp_path, _csv(tmp_path, text))
    assert broker_status(tmp_path)["fills"] == 0


def test_database_failure_rolls_back_entire_csv(tmp_path: Path) -> None:
    open_broker_account(tmp_path, cash="500.00", declared_source="test")
    with sqlite3.connect(tmp_path / "broker.sqlite") as connection:
        connection.execute(
            "CREATE TRIGGER inject_failure BEFORE INSERT ON events WHEN json_extract(NEW.payload,'$.side')='SELL' BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    rows = f"date,side,quantity,price,fee,symbol\n2026-10-08,BUY,100,0.9,5,{SYMBOL}\n2026-10-09,SELL,100,0.9,5,{SYMBOL}\n"
    with pytest.raises(sqlite3.DatabaseError, match="injected failure"):
        _import(tmp_path, _csv(tmp_path, rows))
    assert broker_status(tmp_path)["fills"] == 0


def test_reconcile_detects_date_and_symbol_differences(tmp_path: Path) -> None:
    service, _ = _prepare(tmp_path)
    service.run()
    service.close()
    root = tmp_path / "pilot"
    open_broker_account(root, cash="500.00", declared_source="test")
    rows = "date,side,quantity,price,fee,symbol\n2025-01-05,BUY,200,2.21,5.02,WRONG\n2025-01-08,SELL,200,1.79,5.20,WRONG\n"
    _import(root, _csv(tmp_path, rows))
    report = broker_reconcile(root)
    assert {"SESSION_DELTA", "SYMBOL_DELTA"} <= {
        row["kind"] for row in report["differences"]
    }


def test_accounting_is_exact_and_anomalies_remain_visible(tmp_path: Path) -> None:
    open_broker_account(tmp_path, cash="100.00", declared_source="test")
    _import(
        tmp_path,
        _csv(
            tmp_path,
            "date,side,quantity,price,fee,symbol\n2026-10-09,SELL,100,0.891,5,512170\n",
        ),
    )
    expected = broker_status(tmp_path)
    assert expected["cash"] == "184.100"
    assert expected["accounting_anomalies"] == ["NEGATIVE_POSITION"]
    with localcontext() as context:
        context.prec = 2
        assert broker_status(tmp_path) == expected


def test_closed_pnl_does_not_hide_an_impossible_cash_history(tmp_path: Path) -> None:
    open_broker_account(tmp_path, cash="100.00", declared_source="test")
    _import(tmp_path, _csv(tmp_path, "date,side,quantity,price,fee,symbol\n2026-10-08,BUY,100,0.96,5,512170\n2026-10-09,SELL,100,0.96,5,512170\n"))
    result = broker_status(tmp_path)
    assert result["positions"] == {} and result["cash"] == "90.00"
    assert result["accounting_anomalies"] == ["NEGATIVE_CASH"]
    assert result["closed_cash_pnl_cny"] is None


def test_broker_inspection_cannot_mutate_journal_or_initialize_empty_root(tmp_path: Path) -> None:
    with pytest.raises(BrokerError, match="NOT_OPENED"):
        broker_status(tmp_path)
    assert not (tmp_path / "broker.sqlite").exists()
    open_broker_account(tmp_path, cash="100.00", declared_source="test")
    before = (tmp_path / "broker.sqlite").read_bytes()
    broker_status(tmp_path)
    assert (tmp_path / "broker.sqlite").read_bytes() == before
