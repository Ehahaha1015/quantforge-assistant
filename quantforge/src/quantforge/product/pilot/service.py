"""Pilot host: declaration, append-only journal, steps, tickets, reconciliation.

The declaration fixes symbol, snapshot, capital, strategy and declared CN
frictions before any step; every intent, ticket, risk decision, fill, cursor
and accounting state is committed in one append-only hash-chained journal.
There is no broker transport, no live feed and no external side effect to
redispatch. Reconciliation is an internal paper-consistency check only.
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from quantforge.risk import RiskLimits

from ..scale.storage import Journal, digest, publish, read_json
from ..storage import local_root, writer_lock
from .data import read_snapshot
from .engine import PilotConfig, initial_state, pilot_step
from .fees import CnFeeProfile, CnTradability

__all__ = ["PilotDeclaration", "PilotService", "PilotServiceError"]


class PilotServiceError(ValueError):
    """Sanitized pilot host precondition failure."""


class PilotDeclaration(BaseModel):
    """Immutable operator declaration; paper-only and explicitly non-live."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["cn-pilot-declaration-v1"] = "cn-pilot-declaration-v1"
    program_id: str
    purpose: str
    snapshot_id: str
    symbol: str
    short_window: int
    long_window: int
    initial_cash: Decimal
    strategy: Literal["sma", "canary_hold"] = "sma"
    sizing: Literal["all_in", "fraction", "one_lot"] = "all_in"
    allocation_fraction: Decimal = Decimal(1)
    max_lots_per_order: int | None = None
    fee_profile: CnFeeProfile
    tradability: CnTradability = CnTradability()
    risk_limits: RiskLimits
    research_endorsement: Literal[False] = False
    validated_alpha: Literal[False] = False
    real_transport: Literal["LOCKED"] = "LOCKED"

    @field_validator("program_id", "purpose", "snapshot_id", "symbol")
    @classmethod
    def _text(cls, value: str, info: Any) -> str:
        if not value or value.strip() != value or len(value) > 200:
            raise PilotServiceError(f"{info.field_name} must be trimmed nonempty text")
        return value

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise PilotServiceError(f"pilot declaration is invalid: {exc}") from exc

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_config(self) -> PilotConfig:
        return PilotConfig(
            symbol=self.symbol,
            short_window=self.short_window,
            long_window=self.long_window,
            initial_cash=self.initial_cash,
            strategy=self.strategy,
            sizing=self.sizing,
            allocation_fraction=self.allocation_fraction,
            max_lots_per_order=self.max_lots_per_order,
            fee_profile=self.fee_profile,
            tradability=self.tradability,
            risk_limits=self.risk_limits,
        )


class PilotService:
    def __init__(self, root: str | Path) -> None:
        self.root = local_root(root)
        self.journal = Journal(self.root, "pilot.sqlite")

    def close(self) -> None:
        self.journal.close()

    # -- declaration ------------------------------------------------------

    def declare(self, declaration: PilotDeclaration) -> dict[str, Any]:
        if not isinstance(declaration, PilotDeclaration):
            raise PilotServiceError("declare requires a PilotDeclaration")
        with writer_lock(self.root):
            path = self.root / "declaration.json"
            frame, _ = read_snapshot(self.root, declaration.snapshot_id)
            if frame["symbol"][0] != declaration.symbol:
                raise PilotServiceError("snapshot symbol differs from declaration")
            if path.exists() or path.is_symlink():
                existing = read_json(path)
                if existing != declaration.to_json():
                    raise PilotServiceError("PILOT_ALREADY_DECLARED")
                return existing
            publish(path, declaration.to_json())
            return declaration.to_json()

    def _declaration(self) -> PilotDeclaration:
        path = self.root / "declaration.json"
        if not path.exists() or path.is_symlink():
            raise PilotServiceError("pilot declaration is missing")
        return PilotDeclaration(**read_json(path))

    # -- lifecycle --------------------------------------------------------

    def start(self) -> dict[str, Any]:
        with writer_lock(self.root):
            declaration = self._declaration()
            declaration_digest = digest(declaration.to_json())
            _frame, document = read_snapshot(self.root, declaration.snapshot_id)
            if document["symbol"] != declaration.symbol:
                raise PilotServiceError("snapshot symbol differs from declaration")
            history = self.journal.history()
            if history:
                first = history[0]
                if (
                    first["kind"] != "START"
                    or first["declaration_digest"] != declaration_digest
                ):
                    raise PilotServiceError("PILOT_DECLARATION_CHANGED")
                if self.status()["status"] == "STOPPED":
                    self.journal.append("RESUME")
            else:
                self.journal.append(
                    "START",
                    declaration=declaration.to_json(),
                    declaration_digest=declaration_digest,
                    state=initial_state(declaration.to_config()),
                    cursor=0,
                )
            return self.status()

    def status(self) -> dict[str, Any]:
        history = self.journal.history()
        if not history:
            return {"status": "NOT_STARTED", "real_transport": "LOCKED"}
        active, cursor, state = True, 0, history[0]["state"]
        fills = tickets = 0
        for item in history:
            if item["kind"] == "BAR":
                if item["cursor"] != cursor + 1:
                    raise PilotServiceError("pilot cursor discontinuity")
                cursor, state = item["cursor"], item["state"]
                if item["ticket"] is not None:
                    tickets += 1
                if item["fill"] is not None:
                    fills += 1
            elif item["kind"] == "STOP":
                active = False
            elif item["kind"] == "RESUME":
                active = True
        return {
            "status": "RUNNING" if active else "STOPPED",
            "cursor": cursor,
            "state": state,
            "declaration": history[0]["declaration"],
            "events": len(history),
            "fills": fills,
            "tickets": tickets,
            "real_transport": "LOCKED",
        }

    def stop(self) -> dict[str, Any]:
        with writer_lock(self.root):
            if self.status()["status"] == "RUNNING":
                self.journal.append("STOP")
            return self.status()

    def step(self) -> dict[str, Any]:
        with writer_lock(self.root):
            current = self.status()
            if current["status"] != "RUNNING":
                return current
            declaration = self._declaration()
            if (
                digest(declaration.to_json())
                != self.journal.history()[0]["declaration_digest"]
            ):
                raise PilotServiceError("PILOT_DECLARATION_CHANGED")
            frame, _ = read_snapshot(self.root, declaration.snapshot_id)
            cursor = current["cursor"]
            if cursor >= frame.height:
                return {
                    **current,
                    "stream_status": "WAITING_FOR_DATA",
                    "limitation": "immutable pilot snapshot exhausted; no live feed configured",
                }
            event = pilot_step(frame, declaration.to_config(), cursor, current["state"])
            self.journal.append("BAR", **event)
            return self.status()

    def run(self, *, steps: int = 0, watch: bool = False) -> dict[str, Any]:
        """Compute bounded steps in memory and commit them as one atomic batch.

        ``pilot_step`` is a pure function of durable inputs, so a crash before
        the batch append loses only the uncommitted computation, which resumes
        and recomputes identically. A concurrent writer changing the cursor is
        detected under the host lock and never silently interleaved.
        """
        if steps < 0:
            raise PilotServiceError("steps must be nonnegative")
        self.start()
        current = self.status()
        if current["status"] != "RUNNING":
            return current
        declaration = self._declaration()
        frame, _ = read_snapshot(self.root, declaration.snapshot_id)
        config = declaration.to_config()
        cursor, state = current["cursor"], current["state"]
        events: list[dict[str, Any]] = []
        while (steps == 0 or len(events) < steps) and cursor < frame.height:
            event = pilot_step(frame, config, cursor, state)
            state, cursor = event["state"], event["cursor"]
            events.append(event)
        if events:
            with writer_lock(self.root):
                if self.status()["cursor"] != current["cursor"]:
                    raise PilotServiceError("PILOT_CONCURRENT_WRITE")
                self.journal.append_many([("BAR", event) for event in events])
        result = self.status()
        if cursor >= frame.height:
            result = {
                **result,
                "stream_status": "WAITING_FOR_DATA",
                "limitation": "immutable pilot snapshot exhausted; no live feed configured",
            }
            if watch:
                while self.status()["status"] == "RUNNING":
                    time.sleep(1)
                result = self.status()
        return result

    # -- operator views ---------------------------------------------------

    def history(self) -> list[dict[str, Any]]:
        return self.journal.history()

    def tickets(self) -> list[dict[str, Any]]:
        """Every recorded order ticket in order; the P2 manual-execution view."""
        result = []
        for item in self.journal.history():
            if item["kind"] == "BAR" and item["ticket"] is not None:
                result.append(
                    {
                        "cursor": item["cursor"],
                        "session": item["session"],
                        **item["ticket"],
                    }
                )
        return result

    def reconcile(self) -> dict[str, Any]:
        """Fail-closed internal paper consistency check; no broker statement exists."""
        history = self.journal.history()
        if not history:
            raise PilotServiceError("pilot has no recorded history")
        declaration = PilotDeclaration(**history[0]["declaration"])
        cash = declaration.initial_cash
        fees = Decimal(0)
        held = Decimal(0)
        order_ids: list[str] = []
        bars = 0
        statuses: dict[str, int] = {}
        for item in history:
            if item["kind"] != "BAR":
                continue
            bars += 1
            if item["cursor"] != bars:
                raise PilotServiceError("PILOT_RECONCILIATION_MISMATCH: cursor")
            ticket = item["ticket"]
            if ticket is not None:
                statuses[ticket["status"]] = statuses.get(ticket["status"], 0) + 1
            fill = item["fill"]
            if fill is None:
                continue
            risk = item["risk"]
            if not risk or risk.get("approved") is not True:
                raise PilotServiceError(
                    "PILOT_RECONCILIATION_MISMATCH: unapproved fill"
                )
            quantity = Decimal(fill["quantity"])
            price = Decimal(fill["price"])
            fee = Decimal(fill["fee"])
            if fill["side"] == "BUY":
                cash -= quantity * price + fee
                held += quantity
            else:
                cash += quantity * price - fee
                held -= quantity
            fees += fee
            order_ids.append(fill["order_id"])
        if len(set(order_ids)) != len(order_ids):
            raise PilotServiceError("PILOT_RECONCILIATION_MISMATCH: duplicate fill")
        final_state = history[-1]["state"] if history[-1]["kind"] == "BAR" else None
        if final_state is None:
            final_state = next(
                item["state"] for item in reversed(history) if "state" in item
            )
        portfolio = final_state["portfolio"]
        recorded_held = sum(
            Decimal(p["quantity"])
            for p in portfolio["positions"]
            if p["symbol"] == declaration.symbol
        )
        if (
            Decimal(portfolio["cash"]) != cash
            or Decimal(portfolio["cumulative_fees"]) != fees
            or recorded_held != held
        ):
            raise PilotServiceError("PILOT_RECONCILIATION_MISMATCH: accounting")
        if final_state.get("processed_order_ids") != order_ids:
            raise PilotServiceError("PILOT_RECONCILIATION_MISMATCH: order history")
        return {
            "status": "CONSISTENT",
            "scope": "internal paper consistency only; no broker statement or external fill exists",
            "program_id": declaration.program_id,
            "bars": bars,
            "cash": str(cash),
            "cumulative_fees": str(fees),
            "held_quantity": str(held),
            "order_ids": order_ids,
            "ticket_statuses": statuses,
            "real_transport": "LOCKED",
        }
