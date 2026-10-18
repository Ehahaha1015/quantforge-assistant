"""Explicit simulation adapters; no external broker network exists."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quantforge.domain import Fill, OrderIntent
from quantforge.execution.paper import execute_paper_order
from quantforge.risk import RiskDecision

from .storage import PlatformError


@dataclass(frozen=True)
class SimulationBroker:
    account_id: str

    def submit(
        self,
        intent: OrderIntent,
        risk: RiskDecision,
        price: Decimal,
        fee: Decimal,
        at: datetime,
    ) -> Fill:
        if self.account_id not in {"sim-a", "sim-b"}:
            raise PlatformError("LIVE_BROKER_NOT_CONNECTED")
        return execute_paper_order(intent, risk, price, at, fee)
