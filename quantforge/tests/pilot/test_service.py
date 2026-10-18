from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantforge.experiments import canonical_json
from quantforge.product.pilot.data import extend_bars_csv, import_bars_csv
from quantforge.product.pilot.fees import CN_FEE_PROFILES, CnTradability
from quantforge.product.pilot.service import (
    PilotDeclaration,
    PilotService,
    PilotServiceError,
)
from quantforge.risk import RiskLimits

AS_OF = datetime(2026, 10, 10, tzinfo=UTC)
SYMBOL = "SH512170"

CSV = (
    "date,open,high,low,close,volume\n"
    "2025-01-01,2.00,2.10,1.90,2.00,100000\n"
    "2025-01-02,2.00,2.10,1.90,2.00,100000\n"
    "2025-01-03,2.10,2.20,2.00,2.10,100000\n"
    "2025-01-04,2.20,2.30,2.10,2.20,100000\n"
    "2025-01-05,2.30,2.40,2.20,2.30,100000\n"
    "2025-01-06,2.40,2.50,2.30,2.40,100000\n"
    "2025-01-07,1.90,2.00,1.80,1.90,100000\n"
    "2025-01-08,1.80,1.90,1.70,1.80,100000\n"
)


def _declaration(snapshot_id: str, capital: str = "500") -> PilotDeclaration:
    return PilotDeclaration(
        program_id="pilot-test",
        purpose="unit lifecycle exercise",
        snapshot_id=snapshot_id,
        symbol=SYMBOL,
        short_window=1,
        long_window=2,
        initial_cash=capital,
        sizing="all_in",
        allocation_fraction="1",
        max_lots_per_order=None,
        fee_profile=CN_FEE_PROFILES["stock-default"],
        tradability=CnTradability(),
        risk_limits=RiskLimits(
            max_order_notional=capital,
            max_position_notional=capital,
            max_gross_exposure=capital,
        ),
    )


def _prepare(tmp_path: Path, capital: str = "500") -> tuple[PilotService, str]:
    root = tmp_path / "pilot"
    root.mkdir()
    csv_path = tmp_path / "bars.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    document = import_bars_csv(
        root,
        csv_path,
        SYMBOL,
        as_of=AS_OF,
        declared_adjustment="operator declared: raw unadjusted CNY quotes",
    )
    service = PilotService(root)
    service.declare(_declaration(document["snapshot_id"], capital))
    return service, document["snapshot_id"]


def test_full_lifecycle_buy_hold_sell_reconcile_and_restart(tmp_path: Path) -> None:
    service, _ = _prepare(tmp_path)
    try:
        final = service.run(steps=0)
        assert final["status"] == "RUNNING"
        assert final["stream_status"] == "WAITING_FOR_DATA"
        assert final["cursor"] == 8
        assert final["fills"] == 2

        tickets = service.tickets()
        assert [ticket["status"] for ticket in tickets] == [
            "PAPER_FILLED",
            "PAPER_FILLED",
        ]
        assert tickets[0]["side"] == "BUY"
        assert Decimal(tickets[0]["quantity"]) == Decimal(200)
        assert tickets[0]["estimated_fees"]["commission"] == "5.00"
        assert tickets[1]["side"] == "SELL"

        report = service.reconcile()
        assert report["status"] == "CONSISTENT"
        assert report["bars"] == 8
        assert Decimal(report["cash"]) == Decimal("409.82")
        assert Decimal(report["cumulative_fees"]) == Decimal("10.18")
        assert Decimal(report["held_quantity"]) == Decimal(0)
        assert report["ticket_statuses"] == {"PAPER_FILLED": 2}

        stopped = service.stop()
        assert stopped["status"] == "STOPPED"
        assert service.status()["status"] == "STOPPED"
        resumed = service.run(steps=0)
        assert resumed["status"] == "RUNNING"
        assert resumed["stream_status"] == "WAITING_FOR_DATA"
        kinds = [item["kind"] for item in service.history()]
        assert kinds[0] == "START" and kinds[-1] == "RESUME"
    finally:
        service.close()


def test_one_hundred_yuan_profile_records_honest_insufficient_tickets(
    tmp_path: Path,
) -> None:
    service, _ = _prepare(tmp_path, capital="100")
    try:
        final = service.run(steps=0)
        assert final["cursor"] == 8
        assert final["fills"] == 0
        statuses = [ticket["status"] for ticket in service.tickets()]
        assert (
            statuses == ["INSUFFICIENT_CASH_FOR_ONE_LOT"] * 4
        )  # every long session retries one lot
        report = service.reconcile()
        assert report["status"] == "CONSISTENT"
        assert Decimal(report["cash"]) == Decimal(100)
    finally:
        service.close()


def test_declaration_is_frozen_before_any_step(tmp_path: Path) -> None:
    service, snapshot_id = _prepare(tmp_path)
    try:
        with pytest.raises(PilotServiceError, match="PILOT_ALREADY_DECLARED"):
            service.declare(_declaration(snapshot_id, capital="600"))
        with pytest.raises(PilotServiceError):
            PilotDeclaration(
                program_id="x",
                purpose="bad",
                snapshot_id=snapshot_id,
                symbol=SYMBOL,
                short_window=1,
                long_window=2,
                initial_cash="not-a-number",
                fee_profile=CN_FEE_PROFILES["stock-default"],
                risk_limits=RiskLimits(
                    max_order_notional="100",
                    max_position_notional="100",
                    max_gross_exposure="100",
                ),
            )
        service.run(steps=2)
        declaration_path = service.root / "declaration.json"
        forged = json.loads(declaration_path.read_text(encoding="utf-8"))
        forged["initial_cash"] = "999"
        declaration_path.write_text(canonical_json(forged), encoding="utf-8")
        with pytest.raises(PilotServiceError, match="PILOT_DECLARATION_CHANGED"):
            service.step()
    finally:
        service.close()


def test_append_only_journal_rejects_mutation_and_detects_forgery(
    tmp_path: Path,
) -> None:
    service, _ = _prepare(tmp_path)
    try:
        service.run(steps=2)
        connection = service.journal.connection
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE events SET payload='{}' WHERE seq=1")
        connection.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM events WHERE seq=1")
        connection.rollback()
        connection.execute(
            "INSERT INTO events(previous,payload,hash) VALUES(?,?,?)",
            ("0" * 64, "{}", "forged"),
        )
        connection.commit()
        with pytest.raises(ValueError):
            service.journal.history()
    finally:
        service.close()


def test_daily_extension_continues_the_frozen_declaration(tmp_path: Path) -> None:
    service, snapshot_id = _prepare(tmp_path)
    try:
        first = service.run(steps=0)
        assert first["cursor"] == 8 and first["fills"] == 2
        extra = (
            "2025-01-09,1.70,1.80,1.60,1.70,100000\n"
            "2025-01-10,1.75,1.85,1.65,1.75,100000\n"
            "2025-01-11,1.85,1.95,1.75,1.85,100000\n"
        )
        extra_path = tmp_path / "extra.csv"
        extra_path.write_text(
            "date,open,high,low,close,volume\n" + extra, encoding="utf-8"
        )
        updated = extend_bars_csv(
            service.root,
            snapshot_id,
            extra_path,
            SYMBOL,
            as_of=AS_OF,
            declared_adjustment="operator declared: raw unadjusted CNY quotes",
        )
        assert updated["rows"] == 11 and updated["extensions"] == 1
        resumed = service.run(steps=0)
        assert resumed["cursor"] == 11
        assert resumed["status"] == "RUNNING"
        assert resumed["stream_status"] == "WAITING_FOR_DATA"
        assert resumed["fills"] == 3  # the new trailing session buys again
        report = service.reconcile()
        assert report["status"] == "CONSISTENT"
        assert Decimal(report["cash"]) == Decimal("34.82")
        statuses = [ticket["status"] for ticket in service.tickets()]
        assert statuses[-1] == "PAPER_FILLED"
    finally:
        service.close()
