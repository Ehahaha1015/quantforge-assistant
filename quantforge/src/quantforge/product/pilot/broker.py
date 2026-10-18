"""Operator-declared broker ledger and paper-vs-broker reconciliation (T4).

This is the bookkeeping half of the semi-automatic loop. The operator executes
the pilot's order tickets in their own broker app and then reports the actual
fills here (typed from an app screenshot, export or statement). Everything in
this module is therefore a DECLARED operator observation: nothing contacts a
broker, nothing is broker-certified, and no order can be placed from here.
Differences between the declared broker fills and the paper journal are
expected and are displayed, never smoothed over.
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..scale.storage import Journal
from ..storage import local_root, writer_lock

__all__ = [
    "BrokerError",
    "broker_reconcile",
    "broker_status",
    "import_broker_fills",
    "open_broker_account",
]

_MAX_CSV_BYTES = 4 * 1024 * 1024
_MAX_ROWS = 2_000
_PRECISION = 50
_COLUMNS = {"date", "side", "quantity", "price", "fee"}
_OPTIONAL = {"note", "symbol", "trade_id"}


class BrokerError(ValueError):
    """Sanitized broker-ledger precondition failure."""


def _decimal(text: str, *, name: str, row: int, allow_zero: bool) -> Decimal:
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ArithmeticError, ValueError) as exc:
        raise BrokerError(f"row {row}: {name} is not a decimal number") from exc
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        raise BrokerError(
            f"row {row}: {name} must be finite and {'nonnegative' if allow_zero else 'positive'}"
        )
    return value


def _ledger(root: Path, *, read_only: bool = False) -> Journal:
    return Journal(local_root(root), "broker.sqlite", read_only=read_only)


def _events(root: Path) -> list[dict[str, Any]]:
    root = local_root(root)
    if not (root / "broker.sqlite").is_file():
        raise BrokerError("BROKER_NOT_OPENED")
    journal = _ledger(root, read_only=True)
    try:
        return journal.history()
    finally:
        journal.close()


def open_broker_account(
    root: Path, *, cash: str, declared_source: str
) -> dict[str, Any]:
    """Record the one declared deposit that funds the broker-side ledger."""
    if not declared_source.strip():
        raise BrokerError("declared_source must state where the deposit came from")
    amount = _decimal(cash, name="cash", row=0, allow_zero=False)
    root = local_root(root)
    with writer_lock(root):
        journal = _ledger(root)
        try:
            events = journal.history()
        finally:
            journal.close()
        opens = [event for event in events if event.get("kind") == "BROKER_OPEN"]
        if opens:
            recorded = opens[0]
            if (
                recorded["cash"] == str(amount)
                and recorded["declared_source"] == declared_source.strip()
            ):
                return recorded
            raise BrokerError("BROKER_ALREADY_OPEN")
        journal = _ledger(root)
        try:
            return journal.append(
                "BROKER_OPEN", cash=str(amount), declared_source=declared_source.strip()
            )
        finally:
            journal.close()


def import_broker_fills(
    root: Path, csv_path: Path, *, declared_source: str, as_of: datetime
) -> dict[str, Any]:
    """Append operator-declared broker fills after validating the report rows."""
    if not declared_source.strip():
        raise BrokerError("declared_source must state where the fills came from")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise BrokerError("as_of must be timezone-aware")
    path = Path(csv_path)
    if path.is_symlink() or not path.is_file():
        raise BrokerError("csv must be an existing regular local file")
    raw = path.read_bytes()
    if len(raw) > _MAX_CSV_BYTES:
        raise BrokerError("csv exceeds the bounded import size")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BrokerError("csv must be utf-8 text") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [column.strip().lower() for column in (reader.fieldnames or [])]
    if (
        len(header) != len(set(header))
        or not _COLUMNS <= set(header)
        or not set(header) <= _COLUMNS | _OPTIONAL
    ):
        raise BrokerError(
            f"csv columns must contain {sorted(_COLUMNS)} plus optional {sorted(_OPTIONAL)}"
        )
    rows: list[dict[str, str]] = []
    previous: date | None = None
    reader.fieldnames = header
    for row_number, row in enumerate(reader, start=2):
        if row_number - 1 > _MAX_ROWS:
            raise BrokerError("csv exceeds the bounded row count")
        if None in row or any(value is None for value in row.values()):
            raise BrokerError(f"row {row_number}: csv field count differs from header")
        values = {key: value.strip() for key, value in row.items() if value is not None}
        try:
            day = date.fromisoformat(values["date"])
        except (KeyError, ValueError) as exc:
            raise BrokerError(f"row {row_number}: date must be ISO YYYY-MM-DD") from exc
        if previous is not None and day < previous:
            raise BrokerError("csv dates must be nondecreasing")
        if day > as_of.astimezone(ZoneInfo("Asia/Shanghai")).date():
            raise BrokerError(f"row {row_number}: fill session is after as_of")
        previous = day
        side = values.get("side", "").upper()
        if side not in ("BUY", "SELL"):
            raise BrokerError(f"row {row_number}: side must be BUY or SELL")
        rows.append(
            {
                "session": day.isoformat(),
                "side": side,
                "quantity": str(
                    _decimal(
                        values.get("quantity", ""),
                        name="quantity",
                        row=row_number,
                        allow_zero=False,
                    )
                ),
                "price": str(
                    _decimal(
                        values.get("price", ""),
                        name="price",
                        row=row_number,
                        allow_zero=False,
                    )
                ),
                "fee": str(
                    _decimal(
                        values.get("fee", ""),
                        name="fee",
                        row=row_number,
                        allow_zero=True,
                    )
                ),
                "symbol": values.get("symbol", ""),
                "note": values.get("note", ""),
                "trade_id": values.get("trade_id", ""),
            }
        )
    if not rows:
        raise BrokerError("csv must contain at least one fill")
    file_sha256 = hashlib.sha256(raw).hexdigest()
    root = local_root(root)
    with writer_lock(root):
        journal = _ledger(root)
        try:
            events = journal.history()
            existing = [event for event in events if event.get("kind") == "BROKER_FILL"]
            if not any(event.get("kind") == "BROKER_OPEN" for event in events):
                raise BrokerError("BROKER_NOT_OPENED")
            recorded = {_fill_identity(event) for event in existing}
            # Validate the whole batch before appending anything so a duplicate
            # never commits a partial import.
            for row in rows:
                key = _fill_identity(row)
                if key in recorded:
                    raise BrokerError(
                        "BROKER_FILL_DUPLICATE: this fill is already recorded; "
                        "correct the source file instead of re-importing"
                    )
                recorded.add(key)
            if existing and rows[0]["session"] < existing[-1]["session"]:
                raise BrokerError("csv dates precede the retained fill history")
            journal.append_many(
                [
                    (
                        "BROKER_FILL",
                        {
                            **row,
                            "declared_source": declared_source.strip(),
                            "file_sha256": file_sha256,
                            "as_of": as_of.isoformat(),
                        },
                    )
                    for row in rows
                ]
            )
            return {"appended": len(rows), "file_sha256": file_sha256}
        finally:
            journal.close()


def _fill_identity(fill: dict[str, Any]) -> tuple[Any, ...]:
    # Numeric equality catches re-exports with different Decimal spellings.
    # A supplied broker trade ID distinguishes otherwise identical executions.
    if fill.get("trade_id"):
        return ("trade_id", fill["trade_id"])
    return (
        "observation",
        fill["session"],
        fill["side"],
        fill.get("symbol", ""),
        Decimal(fill["quantity"]),
        Decimal(fill["price"]),
        Decimal(fill["fee"]),
    )


def _broker_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    with localcontext() as arithmetic:
        arithmetic.prec = _PRECISION
        return _broker_state_exact(events)


def _broker_state_exact(events: list[dict[str, Any]]) -> dict[str, Any]:
    cash = Decimal(0)
    fees = Decimal(0)
    positions: dict[str, Decimal] = {}
    anomalies: list[str] = []
    opens = [event for event in events if event.get("kind") == "BROKER_OPEN"]
    if not opens:
        raise BrokerError("BROKER_NOT_OPENED")
    cash = Decimal(opens[0]["cash"])
    fills = [event for event in events if event.get("kind") == "BROKER_FILL"]
    for fill in fills:
        quantity = Decimal(fill["quantity"])
        price = Decimal(fill["price"])
        fee = Decimal(fill["fee"])
        symbol = fill.get("symbol") or "UNSPECIFIED"
        if fill["side"] == "BUY":
            cash -= quantity * price + fee
            positions[symbol] = positions.get(symbol, Decimal(0)) + quantity
        else:
            cash += quantity * price - fee
            positions[symbol] = positions.get(symbol, Decimal(0)) - quantity
        fees += fee
        if cash < 0 and "NEGATIVE_CASH" not in anomalies:
            anomalies.append("NEGATIVE_CASH")
        if positions[symbol] < 0 and "NEGATIVE_POSITION" not in anomalies:
            anomalies.append("NEGATIVE_POSITION")
    return {
        "opened_cash": opens[0]["cash"],
        "declared_source": opens[0]["declared_source"],
        "cash": cash,
        "cumulative_fees": fees,
        "positions": {
            symbol: quantity for symbol, quantity in positions.items() if quantity != 0
        },
        "fills": fills,
        "accounting_anomalies": anomalies,
    }


def broker_status(root: Path) -> dict[str, Any]:
    events = _events(root)
    state = _broker_state(events)
    anomalies = list(state["accounting_anomalies"])
    if any(not fill.get("symbol") for fill in state["fills"]):
        anomalies.append("UNSPECIFIED_SYMBOL")
    with localcontext() as arithmetic:
        arithmetic.prec = _PRECISION
        closed_pnl = (
            str(state["cash"] - Decimal(state["opened_cash"]))
            if state["fills"] and not state["positions"] and not anomalies
            else None
        )
    return {
        "scope": "operator-declared broker facts; no broker connection exists and nothing here is broker-certified",
        "opened_cash": state["opened_cash"],
        "declared_source": state["declared_source"],
        "cash": str(state["cash"]),
        "cumulative_fees": str(state["cumulative_fees"]),
        "positions": {
            symbol: str(quantity) for symbol, quantity in state["positions"].items()
        },
        "fills": len(state["fills"]),
        "accounting_anomalies": anomalies,
        "closed_cash_pnl_cny": closed_pnl,
        "ledger_events": len(events),
        "ledger_last_event_hash": events[-1]["event_hash"],
    }


def broker_reconcile(root: Path) -> dict[str, Any]:
    """Display paper-journal facts next to declared broker facts; never smooth.

    Differences (prices, fees, dates, unexecuted tickets) are expected and are
    reported explicitly. This is not a broker statement and grants nothing.
    """
    root = local_root(root)
    broker = _broker_state(_events(root))
    if not (root / "pilot.sqlite").is_file():
        raise BrokerError("pilot journal has no START event")
    pilot = Journal(root, "pilot.sqlite", read_only=True)
    try:
        pilot_events = pilot.history()
    finally:
        pilot.close()
    if not pilot_events or pilot_events[0].get("kind") != "START":
        raise BrokerError("pilot journal has no START event")
    declaration = pilot_events[0]["declaration"]
    paper_fills = []
    for event in pilot_events:
        if event.get("kind") != "BAR":
            continue
        fill = event.get("fill")
        if fill is not None:
            paper_fills.append(
                {
                    "session": event["session"],
                    "side": fill["side"],
                    "quantity": fill["quantity"],
                    "price": fill["price"],
                    "fee": fill["fee"],
                    "symbol": pilot_events[0]["declaration"]["symbol"],
                }
            )
    differences: list[dict[str, Any]] = []
    with localcontext() as arithmetic:
        arithmetic.prec = _PRECISION
        for side in ("BUY", "SELL"):
            paper_side = [fill for fill in paper_fills if fill["side"] == side]
            broker_side = [fill for fill in broker["fills"] if fill["side"] == side]
            paper_quantity = sum(
                (Decimal(fill["quantity"]) for fill in paper_side), Decimal(0)
            )
            broker_quantity = sum(
                (Decimal(fill["quantity"]) for fill in broker_side), Decimal(0)
            )
            paper_fees = sum((Decimal(fill["fee"]) for fill in paper_side), Decimal(0))
            broker_fees = sum(
                (Decimal(fill["fee"]) for fill in broker_side), Decimal(0)
            )
            if len(paper_side) != len(broker_side) or paper_quantity != broker_quantity:
                differences.append(
                    {
                        "kind": "SIDE_MISMATCH",
                        "side": side,
                        "paper": f"{len(paper_side)} fills / {paper_quantity} shares",
                        "broker": f"{len(broker_side)} fills / {broker_quantity} shares",
                    }
                )
            if paper_fees != broker_fees:
                differences.append(
                    {
                        "kind": "FEE_DELTA",
                        "side": side,
                        "paper": str(paper_fees),
                        "broker": str(broker_fees),
                        "delta": str(broker_fees - paper_fees),
                    }
                )
            for index, (paper_fill, broker_fill) in enumerate(
                zip(paper_side, broker_side, strict=False)
            ):
                for field, kind in (
                    ("session", "SESSION_DELTA"),
                    ("symbol", "SYMBOL_DELTA"),
                ):
                    if paper_fill[field] != broker_fill.get(field, ""):
                        differences.append(
                            {
                                "kind": kind,
                                "side": side,
                                "index": index + 1,
                                "paper": paper_fill[field],
                                "broker": broker_fill.get(field, ""),
                            }
                        )
                # Numeric comparison: pydantic serializes Decimal("100") as
                # "1E+2", which must not read as a quantity difference.
                if Decimal(paper_fill["price"]) != Decimal(broker_fill["price"]):
                    differences.append(
                        {
                            "kind": "PRICE_DELTA",
                            "side": side,
                            "index": index + 1,
                            "paper": paper_fill["price"],
                            "broker": broker_fill["price"],
                            "delta": str(
                                Decimal(broker_fill["price"])
                                - Decimal(paper_fill["price"])
                            ),
                        }
                    )
                if Decimal(paper_fill["quantity"]) != Decimal(broker_fill["quantity"]):
                    differences.append(
                        {
                            "kind": "QUANTITY_DELTA",
                            "side": side,
                            "index": index + 1,
                            "paper": paper_fill["quantity"],
                            "broker": broker_fill["quantity"],
                        }
                    )
    paper_cash = None
    final = next(
        (event["state"] for event in reversed(pilot_events) if "state" in event), None
    )
    if final is not None:
        paper_cash = final["portfolio"]["cash"]
    symbol = declaration["symbol"]
    paper_positions = {}
    if final is not None:
        for position in final["portfolio"]["positions"]:
            paper_positions[position["symbol"]] = position["quantity"]
    broker_positions = {
        position_symbol: str(quantity)
        for position_symbol, quantity in broker["positions"].items()
    }
    return {
        "scope": "operator-declared broker facts vs internal paper journal; differences are expected and displayed, never smoothed; not a broker statement",
        "program_id": declaration["program_id"],
        "symbol": symbol,
        "paper": {
            "fills": len(paper_fills),
            "cash": paper_cash,
            "positions": paper_positions,
        },
        "broker": {
            "fills": len(broker["fills"]),
            "cash": str(broker["cash"]),
            "positions": broker_positions,
        },
        "cash_delta": (
            str(Decimal(broker["cash"]) - Decimal(paper_cash))
            if paper_cash is not None
            else None
        ),
        "differences": differences,
    }
