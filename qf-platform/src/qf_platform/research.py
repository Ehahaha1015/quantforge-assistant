"""Bounded Lab proposals and Core causal signals on explicit synthetic data.

This fixture qualifies plumbing only. It is never an EvidencePack, OOS test,
validated alpha or live admission. The gateway has no execution tools.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
from qf_lab.gateway import FakeProvider, ModelGateway
from quantforge.strategies.models import (
    config_from_strategy,
    generate_strategy_signals,
    strategy_from_parameters,
)

from .storage import digest


def fixture(cycle: int) -> tuple[pl.DataFrame, Decimal]:
    # Consecutive synthetic sessions; rising then falling to exercise both sides.
    rising = cycle % 2 == 0
    start = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=cycle * 81)
    closes = [
        Decimal("0.800") + Decimal("0.004") * i
        if rising
        else Decimal("1.400") - Decimal("0.004") * i
        for i in range(80)
    ]
    rows = [
        {
            "symbol": "SIM-ETF",
            "timestamp": start + timedelta(days=i),
            "open": float(price),
            "high": float(price + Decimal("0.002")),
            "low": float(price - Decimal("0.002")),
            "close": float(price),
            "volume": 100000.0,
        }
        for i, price in enumerate(closes)
    ]
    price = closes[-1] + (Decimal("0.006") if rising else Decimal("-0.004"))
    return pl.DataFrame(rows), price


def run_research(root: Path, request_id: str, cycle: int) -> dict[str, Any]:
    bars, execution_price = fixture(cycle)
    directory = root / "research" / request_id
    directory.mkdir(parents=True, exist_ok=False)
    gateway = ModelGateway(directory / "calls.sqlite", FakeProvider(), max_calls=3)
    evidence: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    try:
        for phase in ("first", "next", "final"):
            call_id, decision = gateway.call(
                {
                    "phase": phase,
                    "goal": "Verify synthetic research-to-simulation plumbing only",
                    "max_long_window": 40,
                    "allowed_families": ["moving_average_crossover"],
                    "allowed_actions": ["freeze", "reject"]
                    if phase == "final"
                    else ["propose"]
                    if phase == "first"
                    else ["modify"],
                    "evidence": evidence,
                    "mode": "SIMULATION",
                }
            )
            decisions.append(
                {
                    "call_id": call_id,
                    "phase": phase,
                    "decision": decision.model_dump(mode="json"),
                }
            )
            if phase == "final":
                continue
            strategy = strategy_from_parameters(decision.family, decision.parameters)
            signals = generate_strategy_signals(bars, config_from_strategy(strategy))
            projection = {
                "strategy": decision.strategy,
                "target": str(int(signals["value"][-1])),
                "signal_count": signals.height,
                "scope": "SYNTHETIC_SIGNAL_PROJECTION_ONLY",
            }
            evidence.append({**projection, "evidence_id": digest(projection)})
        final = decisions[-1]["decision"]
        selected = next(
            item
            for item in evidence
            if item["strategy"]
            == {
                "family": final["family"],
                "parameters": {
                    "short_window": final["short_window"],
                    "long_window": final["long_window"],
                },
            }
        )
        return {
            "request_id": request_id,
            "cycle": cycle,
            "provider": "deterministic-fake",
            "real_model_call": False,
            "paid_model_calls": 0,
            "decisions": decisions,
            "evidence": evidence,
            "critic_verdict": final["action"],
            "target": selected["target"],
            "execution_price": str(execution_price),
            "simulation_session": (
                bars["timestamp"][-1] + timedelta(days=1)
            ).isoformat(),
            "alpha_status": "NOT_VALIDATED",
            "source": "Lab FakeProvider + Core causal signal; synthetic replay",
        }
    finally:
        gateway.close()
