"""Restart-safe historical paper stream, reusing frozen risk/fill/accounting.

Every simulated intent, risk decision, fill, cursor and accounting state is
committed in one append-only SQLite transaction. There is no broker transport
or external side effect to redispatch after a crash.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any, Literal

from quantforge.data import DatasetSnapshot
from quantforge.domain import OrderIntent
from quantforge.execution.paper import execute_paper_order
from quantforge.experiments import canonical_json
from quantforge.portfolio import (
    PortfolioState,
    apply_fill,
    create_portfolio,
    value_portfolio,
)
from quantforge.risk import RiskLimits, evaluate_order
from quantforge.strategies import (
    MovingAverageCrossoverConfig,
    generate_moving_average_signals,
)
from quantforge.strategies.models import (
    config_from_strategy,
    generate_strategy_signals,
    required_observations,
)

from .context import source_identity
from .evidence import evidence_id
from .lockbox import inspect_final
from .policy import read_freeze
from .runner import ExperimentRunError, ExperimentRunner, _identifier, _timestamps
from .storage import local_root, writer_lock


class PaperRuntime:
    def __init__(self, runner: ExperimentRunner, program_id: str) -> None:
        self.runner, self.program_id = runner, _identifier(program_id)
        root = runner.root / "paper"
        root.mkdir(exist_ok=True)
        local_root(root)
        self.path = root / (program_id + ".sqlite")
        if self.path.is_symlink():
            raise ExperimentRunError("redirected paper store")
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, previous_hash TEXT NOT NULL, payload TEXT NOT NULL, event_hash TEXT NOT NULL)"
        )
        for operation in ("UPDATE", "DELETE"):
            self.connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS no_{operation.lower()} BEFORE {operation} ON events BEGIN SELECT RAISE(ABORT,'append-only'); END"
            )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def _history(self) -> list[dict[str, Any]]:
        previous = "0" * 64
        result: list[dict[str, Any]] = []
        for seq, before, payload, digest in self.connection.execute(
            "SELECT seq,previous_hash,payload,event_hash FROM events ORDER BY seq"
        ):
            if (
                seq != len(result) + 1
                or before != previous
                or hashlib.sha256((previous + payload).encode()).hexdigest() != digest
            ):
                raise ExperimentRunError("paper history integrity mismatch")
            item = json.loads(payload)
            if canonical_json(item) != payload:
                raise ExperimentRunError("noncanonical paper event")
            result.append({"seq": seq, "event_hash": digest, **item})
            previous = digest
        return result

    def _append(self, item: dict[str, Any]) -> None:
        history = self._history()
        previous = history[-1]["event_hash"] if history else "0" * 64
        payload = canonical_json(item)
        digest = hashlib.sha256((previous + payload).encode()).hexdigest()
        with self.connection:
            self.connection.execute(
                "INSERT INTO events(previous_hash,payload,event_hash) VALUES(?,?,?)",
                (previous, payload, digest),
            )

    def start(self) -> dict[str, Any]:
        with writer_lock(self.runner.root):
            frozen = read_freeze(self.runner, self.program_id)
            final = inspect_final(self.runner, self.program_id)
            if not final["paper_eligible"]:
                raise ExperimentRunError("final loss screen blocks paper promotion")
            evidence = self.runner.inspect(frozen["run_id"])
            facts = evidence["FACT"]
            if facts["source_sha256"] != source_identity():
                raise ExperimentRunError(
                    "paper code differs from selected strategy version"
                )
            config = {
                "schema_version": "paper-runtime-v1",
                "mode": "HISTORICAL_PAPER_ONLY",
                "program_id": self.program_id,
                "selected_run": frozen["run_id"],
                "evidence_id": evidence_id(evidence),
                "final_evidence_id": evidence_id(final["evidence"]),
                "freeze_sha256": frozen["freeze_sha256"],
                "source_sha256": facts["source_sha256"],
                "dataset_snapshot": facts["specification"]["recipe"][
                    "dataset_snapshot"
                ],
                "strategy": facts["specification"]["recipe"]["strategy"],
                **(
                    {
                        "strategy_spec_version": "experiment-spec-v2",
                        "families": facts["families"],
                    }
                    if facts["specification"]["schema_version"] == "experiment-spec-v2"
                    else {}
                ),
                "fee_bps": "10",
                "allocation_fraction": "0.1",
                "risk_limits": {
                    "max_order_notional": "20000",
                    "max_position_notional": "50000",
                    "max_gross_exposure": "50000",
                },
                "real_transport": "LOCKED",
                "validated_alpha": False,
            }
            history = self._history()
            if history:
                if history[0]["config"] != config:
                    raise ExperimentRunError("paper strategy/provenance changed")
                if self.status()["status"] == "STOPPED":
                    self._append({"kind": "RESUME"})
            else:
                self._append(
                    {
                        "kind": "START",
                        "config": config,
                        "state": create_portfolio(Decimal(100000)).model_dump(
                            mode="json"
                        ),
                        "cursor": 0,
                    }
                )
            return self.status()

    def status(self) -> dict[str, Any]:
        history = self._history()
        if not history:
            return {
                "status": "NOT_STARTED",
                "program_id": self.program_id,
                "real_transport": "LOCKED",
            }
        active, cursor, state = True, 0, history[0]["state"]
        for item in history:
            if item["kind"] == "BAR":
                if item["cursor"] != cursor + 1:
                    raise ExperimentRunError("paper cursor discontinuity")
                cursor, state = item["cursor"], item["state"]
            elif item["kind"] == "STOP":
                active = False
            elif item["kind"] == "RESUME":
                active = True
        return {
            "status": "RUNNING" if active else "STOPPED",
            "program_id": self.program_id,
            "cursor": cursor,
            "state": state,
            "config": history[0]["config"],
            "events": len(history),
            "fills": sum(item.get("fill") is not None for item in history),
            "real_transport": "LOCKED",
        }

    def stop(self) -> dict[str, Any]:
        with writer_lock(self.runner.root):
            if self.status()["status"] == "RUNNING":
                self._append({"kind": "STOP"})
            return self.status()

    def step(self) -> dict[str, Any]:
        with writer_lock(self.runner.root):
            current = self.status()
            if current["status"] != "RUNNING":
                return current
            config = current["config"]
            if source_identity() != config["source_sha256"]:
                raise ExperimentRunError("paper executing source changed")
            snapshot = self.runner.catalog.verify(
                config["dataset_snapshot"]["dataset_id"]
            )
            if snapshot != DatasetSnapshot.model_validate(config["dataset_snapshot"]):
                raise ExperimentRunError("paper dataset changed")
            frame = self.runner.catalog.read(snapshot.dataset_id)
            cursor = current["cursor"]
            if cursor >= frame.height:
                return {
                    **current,
                    "stream_status": "WAITING_FOR_DATA",
                    "limitation": "Immutable historical replay stream exhausted; no live feed configured",
                }
            moment = _timestamps(frame.slice(cursor, 1))[0]
            symbol = str(frame["symbol"][cursor])
            price = Decimal(str(frame["open"][cursor]))
            mark = Decimal(str(frame["close"][cursor]))
            state = PortfolioState.model_validate(current["state"])
            strategy = config_from_strategy(
                config["strategy"],
                legacy=config.get("strategy_spec_version", "experiment-spec-v1")
                == "experiment-spec-v1",
            )
            desired = 0
            if cursor >= required_observations(strategy):
                # Prior observation's close alone determines next-open intent.
                signals = (
                    generate_moving_average_signals(frame.head(cursor), strategy)
                    if isinstance(strategy, MovingAverageCrossoverConfig)
                    else generate_strategy_signals(frame.head(cursor), strategy)
                )
                desired = int(signals["value"][-1])
            held = next(
                (p.quantity for p in state.positions if p.symbol == symbol), Decimal(0)
            )
            intent = decision = fill = None
            with localcontext() as arithmetic:
                arithmetic.prec = 50
                quantity = Decimal(0)
                side: Literal["BUY", "SELL"] = "BUY"
                if desired and held == 0:
                    quantity = (
                        state.cash * Decimal(config["allocation_fraction"]) / price
                    ).to_integral_value(rounding=ROUND_FLOOR)
                elif not desired and held > 0:
                    quantity, side = held, "SELL"
                if quantity > 0:
                    order = OrderIntent(
                        strategy_id=config["evidence_id"],
                        symbol=symbol,
                        timestamp=moment,
                        side=side,
                        quantity=quantity,
                    )
                    fee = quantity * price * Decimal(config["fee_bps"]) / Decimal(10000)
                    risk = evaluate_order(
                        order,
                        state,
                        {symbol: price},
                        fee,
                        RiskLimits(**config["risk_limits"]),
                    )
                    intent, decision = (
                        order.model_dump(mode="json"),
                        risk.model_dump(mode="json"),
                    )
                    if risk.approved:
                        filled = execute_paper_order(order, risk, price, moment, fee)
                        state = apply_fill(state, filled)
                        fill = filled.model_dump(mode="json")
                marks = {symbol: mark} if state.positions else {}
                valuation = value_portfolio(state, marks, moment).model_dump(
                    mode="json"
                )
            self._append(
                {
                    "kind": "BAR",
                    "cursor": cursor + 1,
                    "timestamp": moment.isoformat(),
                    "desired_position": desired,
                    "order_intent": intent,
                    "risk": decision,
                    "fill": fill,
                    "state": state.model_dump(mode="json"),
                    "valuation": valuation,
                }
            )
            return self.status()

    def run(self, *, steps: int = 0, watch: bool = False) -> dict[str, Any]:
        if steps < 0:
            raise ValueError("steps must be nonnegative")
        self.start()
        count = 0
        while steps == 0 or count < steps:
            status = self.step()
            if status["status"] != "RUNNING":
                return status
            if status.get("stream_status") == "WAITING_FOR_DATA":
                if not watch:
                    return status
                time.sleep(1)
            else:
                count += 1
        return self.status()

    def history(self) -> list[dict[str, Any]]:
        return self._history()
