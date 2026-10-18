"""Deterministic simulation funds, execution and event projections."""

from __future__ import annotations

import copy
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Literal

from quantforge.domain import OrderIntent
from quantforge.portfolio import PortfolioState, apply_fill, create_portfolio
from quantforge.product.pilot.fees import (
    CN_FEE_PROFILES,
    CnTradability,
    affordable_lots,
    calculate_fees,
)
from quantforge.risk import RiskLimits, evaluate_order

from .adapters import SimulationBroker
from .research import run_research
from .storage import Ledger, PlatformError, digest

_BUDGET = Decimal("10000.00")
_GROSS_CAP = Decimal("6000.00")
_ACCOUNT_CAP = Decimal("3000.00")
_IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,80}\Z")


def money(value: Any) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1,2})?", value
    ):
        raise PlatformError("EXACT_POSITIVE_CNY_STRING_REQUIRED")
    number = Decimal(value)
    if not 0 < number <= Decimal("1000000"):
        raise PlatformError("AMOUNT_OUTSIDE_BOUNDS")
    return number


def amount(value: Decimal) -> str:
    return format(value, ".2f")


def initial_state() -> dict[str, Any]:
    return {
        "mode": "SIMULATION",
        "bank_cash": amount(_BUDGET),
        "external_funding": amount(_BUDGET),
        "bank_initial": amount(_BUDGET),
        "broker_accounts": {
            key: {
                "name": name,
                "portfolio": create_portfolio(0).model_dump(mode="json"),
            }
            for key, name in (("sim-a", "模拟券商 A"), ("sim-b", "模拟券商 B"))
        },
        "fills": [],
        "research": [],
        "cycle": 0,
        "paused": False,
        "autonomy": stopped_autonomy(),
        "mark_price": "1.000",
        "simulation_session": None,
        "risk_limits": {
            "global_gross_cap_cny": amount(_GROSS_CAP),
            "per_account_cap_cny": amount(_ACCOUNT_CAP),
            "auto_allocation_per_account_cny": "2500.00",
        },
    }


def stopped_autonomy() -> dict[str, Any]:
    return {
        "enabled": False,
        "run_id": None,
        "remaining": 0,
        "interval_seconds": 6,
        "next_at": None,
        "status": "STOPPED",
        "reason": None,
    }


def portfolio_of(state: dict[str, Any], account: str) -> PortfolioState:
    if account not in state["broker_accounts"]:
        raise PlatformError("BROKER_NOT_CONNECTED")
    return PortfolioState.model_validate(state["broker_accounts"][account]["portfolio"])


def gross(state: dict[str, Any]) -> Decimal:
    mark = Decimal(state["mark_price"])
    return sum(
        (
            position.quantity * mark
            for key in state["broker_accounts"]
            for position in portfolio_of(state, key).positions
        ),
        Decimal(0),
    )


def assert_funds(state: dict[str, Any]) -> None:
    # Cash conservation uses retained fill notionals, independent of marking.
    cash = Decimal(state["bank_cash"]) + Decimal(state.get("payment_wallet", "0.00"))
    cash += sum(
        (portfolio_of(state, key).cash for key in state["broker_accounts"]), Decimal(0)
    )
    flows = sum(
        (
            (Decimal(fill["price"]) * Decimal(fill["quantity"]))
            * (1 if fill["side"] == "BUY" else -1)
            + Decimal(fill["fee"])
            for fill in state["fills"]
        ),
        Decimal(0),
    )
    if cash + flows != Decimal(state["external_funding"]):
        raise PlatformError("FUNDS_CONSERVATION_FAILED")
    if (
        Decimal(state["bank_cash"]) < 0
        or Decimal(state.get("payment_wallet", "0.00")) < 0
        or any(portfolio_of(state, key).cash < 0 for key in state["broker_accounts"])
    ):
        raise PlatformError("NEGATIVE_CASH")


class PlatformService:
    def __init__(self, root: Path) -> None:
        self.ledger = Ledger(root)
        with self.ledger.transaction() as db:
            if not self.ledger.projection(db, limit=1)["events_total"]:
                self._emit(db, initial_state(), "INITIALIZED", "模拟银行资金 10,000 元")

    def _emit(
        self,
        db: sqlite3.Connection,
        state: dict[str, Any],
        kind: str,
        summary: str,
        **details: Any,
    ) -> None:
        assert_funds(state)
        self.ledger.append(
            db,
            kind=kind,
            summary=summary,
            state=copy.deepcopy(state),
            details=details,
            mode="SIMULATION",
        )

    def snapshot(self) -> dict[str, Any]:
        with localcontext() as ctx:
            ctx.prec = 50
            projected = self.ledger.read_projection()
            return self._snapshot(projected["tail"], total=projected["events_total"])

    @staticmethod
    def _snapshot(
        history: list[dict[str, Any]], *, total: int | None = None
    ) -> dict[str, Any]:
        if total is None:
            total = len(history)
        if not history or type(total) is not int or total != history[-1]["seq"]:
            raise PlatformError("SNAPSHOT_PROJECTION_SOURCE_CONFLICT")
        state = copy.deepcopy(history[-1]["state"])
        assert_funds(state)
        brokers = []
        mark = Decimal(state["mark_price"])
        total_cash = Decimal(state["bank_cash"])
        fees = Decimal(0)
        for key, info in state["broker_accounts"].items():
            portfolio = portfolio_of(state, key)
            positions = [
                {
                    "symbol": position.symbol,
                    "quantity": format(position.quantity, "f"),
                    "average_price": format(position.average_price, "f"),
                    "mark_price": str(mark),
                    "market_value": amount(position.quantity * mark),
                    "unrealized_pnl": amount(
                        position.quantity * (mark - position.average_price)
                    ),
                }
                for position in portfolio.positions
            ]
            equity = portfolio.cash + sum(
                (p.quantity * mark for p in portfolio.positions), Decimal(0)
            )
            brokers.append(
                {
                    "id": key,
                    "name": info["name"],
                    "mode": "SIMULATION",
                    "cash": amount(portfolio.cash),
                    "equity": amount(equity),
                    "fees": amount(portfolio.cumulative_fees),
                    "positions": positions,
                }
            )
            total_cash += portfolio.cash
            fees += portfolio.cumulative_fees
        exposure = gross(state)
        # Retain legacy ledger conservation without exposing retired wallet funds.
        wallet = Decimal(state.get("payment_wallet", "0.00"))
        return {
            "schema_version": "qf-platform-simulation-v1",
            "mode": "SIMULATION",
            "observed_at": datetime.now(UTC).isoformat(),
            "version": history[-1]["seq"],
            "ledger_hash": history[-1]["event_hash"],
            "source": "Verified local simulation journal; not broker-certified",
            "real_transport": "LOCKED",
            "paid_model_calls": 0,
            "paused": state["paused"],
            "autonomy": state.get("autonomy", stopped_autonomy()),
            "cycle": state["cycle"],
            "bank_cash": state["bank_cash"],
            "securities_equity": amount(total_cash + exposure),
            "total_equity": amount(total_cash + exposure),
            "net_pnl": amount(
                total_cash + exposure - (Decimal(state["external_funding"]) - wallet)
            ),
            "gross_exposure": amount(exposure),
            "cumulative_fees": amount(fees),
            "external_funding": amount(Decimal(state["external_funding"]) - wallet),
            "risk_limits": state["risk_limits"],
            "brokers": brokers,
            "fills": [
                {
                    **fill,
                    **{
                        field: format(Decimal(fill[field]), "f")
                        for field in ("quantity", "price", "fee")
                    },
                }
                for fill in state["fills"][-100:]
            ],
            "fills_total": len(state["fills"]),
            "research": state["research"][-10:],
            "events": [
                {
                    key: item[key]
                    for key in ("seq", "at", "kind", "summary", "event_hash")
                }
                for item in history[-100:]
            ],
            "events_total": total,
            "connections": [
                {
                    "name": "真实券商 API",
                    "status": "NOT_CONNECTED",
                    "reason": "等待具体券商官方接口权限与执行链验收",
                },
                {
                    "name": "真实 AI 研究模型",
                    "status": "NOT_ENABLED",
                    "reason": "本页用已有 Lab FakeProvider 验证流程，不调用付费模型",
                },
            ],
        }

    def operate(
        self, operation: str, payload: dict[str, Any], key: str
    ) -> dict[str, Any]:
        if not _IDENTIFIER.fullmatch(key):
            raise PlatformError("IDEMPOTENCY_KEY_REQUIRED")
        signature = digest({"operation": operation, "payload": payload})
        with localcontext() as ctx, self.ledger.transaction() as db:
            ctx.prec = 50
            history = self.ledger.history(db)
            for event in history:
                if event.get("request_id") == key:
                    if event["signature"] != signature:
                        raise PlatformError("IDEMPOTENCY_CONFLICT")
                    return self._snapshot(history)
            state = copy.deepcopy(history[-1]["state"])
            state.setdefault("autonomy", stopped_autonomy())
            if operation == "transfer":
                if set(payload) != {"from", "to", "amount"}:
                    raise PlatformError("TRANSFER_SCHEMA")
                self._transfer(
                    db, state, payload["from"], payload["to"], money(payload["amount"])
                )
            elif operation == "pause":
                if set(payload) != {"paused"} or type(payload["paused"]) is not bool:
                    raise PlatformError("PAUSE_SCHEMA")
                state["paused"] = payload["paused"]
                self._emit(
                    db,
                    state,
                    "CONTROL_CHANGED",
                    "已暂停模拟开仓" if state["paused"] else "已恢复模拟开仓",
                )
            elif operation == "cycle":
                if payload:
                    raise PlatformError("CYCLE_SCHEMA")
                autonomy = state["autonomy"]
                if key.startswith("auto-") and not autonomy["enabled"]:
                    raise PlatformError("AUTONOMOUS_BATCH_STOPPED")
                if autonomy["enabled"] and key != self._auto_key(state):
                    raise PlatformError("AUTONOMOUS_BATCH_RUNNING")
                self._cycle(db, state, key)
                if autonomy["enabled"]:
                    autonomy["remaining"] -= 1
                    autonomy["next_at"] = (
                        datetime.now(UTC)
                        + timedelta(seconds=autonomy["interval_seconds"])
                    ).isoformat()
                    if autonomy["remaining"] <= 0 or state["cycle"] >= 20:
                        autonomy.update(enabled=False, status="COMPLETED", next_at=None)
                    self._emit(db, state, "AUTONOMY_PROGRESS", "后台自主模拟轮次已提交")
            elif operation == "autonomy":
                if type(payload.get("enabled")) is not bool:
                    raise PlatformError("AUTONOMY_SCHEMA")
                if payload["enabled"]:
                    if (
                        set(payload) != {"enabled", "rounds", "interval_seconds"}
                        or type(payload["rounds"]) is not int
                        or not 1 <= payload["rounds"] <= 20 - state["cycle"]
                        or type(payload["interval_seconds"]) is not int
                        or not 6 <= payload["interval_seconds"] <= 60
                        or state["autonomy"]["enabled"]
                    ):
                        raise PlatformError("AUTONOMY_BUDGET_OR_STATE_INVALID")
                    state["autonomy"] = {
                        "enabled": True,
                        "run_id": key,
                        "remaining": payload["rounds"],
                        "interval_seconds": payload["interval_seconds"],
                        "next_at": datetime.now(UTC).isoformat(),
                        "status": "RUNNING",
                        "reason": None,
                    }
                else:
                    if set(payload) != {"enabled"}:
                        raise PlatformError("AUTONOMY_SCHEMA")
                    state["autonomy"].update(
                        enabled=False, status="STOPPED", next_at=None
                    )
                self._emit(db, state, "AUTONOMY_CONTROL", "后台自主模拟配置已记录")
            else:
                raise PlatformError("OPERATION_NOT_SUPPORTED")
            self.ledger.append(
                db,
                kind="REQUEST_COMMITTED",
                summary="请求已提交",
                state=state,
                details={},
                mode="SIMULATION",
                request_id=key,
                signature=signature,
            )
            projected = self.ledger.projection(db)
            return self._snapshot(projected["tail"], total=projected["events_total"])

    @staticmethod
    def _auto_key(state: dict[str, Any]) -> str:
        run_hash = digest(state["autonomy"]["run_id"])[:32]
        return f"auto-{run_hash}-{state['cycle']}"

    def autonomous_tick(self, *, now: datetime | None = None) -> bool:
        moment = datetime.now(UTC) if now is None else now
        if moment.tzinfo is None:
            raise PlatformError("AWARE_SCHEDULER_CLOCK_REQUIRED")
        state = self.ledger.read_projection(limit=1)["tail"][-1]["state"]
        config = state.get("autonomy", stopped_autonomy())
        if not config["enabled"] or datetime.fromisoformat(config["next_at"]) > moment:
            return False
        key = self._auto_key(state)
        try:
            self.operate("cycle", {}, key)
        except (ValueError, OSError, sqlite3.Error):
            with self.ledger.transaction() as db:
                latest = self.ledger.projection(db, limit=1)["tail"][-1]["state"]
                latest_config = latest.get("autonomy", stopped_autonomy())
                if (
                    latest_config["enabled"]
                    and latest_config.get("run_id") == config["run_id"]
                ):
                    latest_config.update(
                        enabled=False,
                        status="BLOCKED",
                        next_at=None,
                        reason="RESEARCH_OR_STATE_REVIEW_REQUIRED",
                    )
                    latest["autonomy"] = latest_config
                    self._emit(
                        db,
                        latest,
                        "AUTONOMY_BLOCKED",
                        "后台循环已停止，研究或账目待核查",
                    )
            return False
        return True

    def _transfer(
        self,
        db: sqlite3.Connection,
        state: dict[str, Any],
        source: str,
        target: str,
        value: Decimal,
    ) -> None:
        allowed = {"bank", *state["broker_accounts"]}
        if source not in allowed or target not in allowed or source == target:
            raise PlatformError("TRANSFER_ROUTE_NOT_AVAILABLE")
        if source != "bank" and target != "bank":
            raise PlatformError("BROKER_TRANSFER_REQUIRES_BANK_ROUTE")
        for account, change in ((source, -value), (target, value)):
            if account == "bank":
                state["bank_cash"] = amount(Decimal(state["bank_cash"]) + change)
            else:
                portfolio = portfolio_of(state, account)
                updated = portfolio.model_dump(mode="json")
                updated["cash"] = amount(portfolio.cash + change)
                state["broker_accounts"][account]["portfolio"] = updated
        self._emit(
            db,
            state,
            "FUNDS_TRANSFERRED",
            f"模拟调拨 {amount(value)} 元：{source} → {target}",
        )

    def _cycle(self, db: sqlite3.Connection, state: dict[str, Any], key: str) -> None:
        if state["cycle"] >= 20:
            raise PlatformError("BOUNDED_RESEARCH_CYCLES_EXHAUSTED")
        if (self.ledger.root / "research" / key).exists():
            raise PlatformError("RESEARCH_ATTEMPT_REQUIRES_REVIEW")
        research = run_research(self.ledger.root, key, state["cycle"])
        state["research"].append(research)
        state["cycle"] += 1
        state["mark_price"] = research["execution_price"]
        state["simulation_session"] = research["simulation_session"]
        self._emit(
            db, state, "RESEARCH_COMPLETED", "Lab 三阶段提案完成，Core 已计算因果信号"
        )
        if research["critic_verdict"] != "freeze":
            self._emit(db, state, "RESEARCH_REJECTED", "批评未接受候选，保留现金")
            return
        price = Decimal(state["mark_price"])
        at = datetime.fromisoformat(research["simulation_session"])
        for account in state["broker_accounts"]:
            portfolio = portfolio_of(state, account)
            held = sum((p.quantity for p in portfolio.positions), Decimal(0))
            side: Literal["BUY", "SELL"] = (
                "BUY" if research["target"] == "1" else "SELL"
            )
            if side == "BUY" and (held or state["paused"]):
                self._emit(
                    db, state, "ORDER_WITHHELD", f"{account} 已有仓位或开仓已暂停"
                )
                continue
            if side == "SELL" and not held:
                continue
            if side == "BUY":
                allocation = min(
                    Decimal("2500") - portfolio.cash, Decimal(state["bank_cash"])
                )
                if allocation > 0:
                    self._transfer(db, state, "bank", account, allocation)
                    portfolio = portfolio_of(state, account)
                quantity = Decimal(
                    100
                    * affordable_lots(
                        portfolio.cash,
                        price,
                        CN_FEE_PROFILES["etf-min-5"],
                        CnTradability(),
                        max_lots=25,
                    )
                )
            else:
                quantity = held
            if quantity <= 0:
                self._emit(db, state, "ORDER_WITHHELD", f"{account} 一手资金不足")
                continue
            fees = calculate_fees(side, quantity, price, CN_FEE_PROFILES["etf-min-5"])
            intent = OrderIntent(
                strategy_id=f"platform-{key}-{account}",
                symbol="SIM-ETF",
                timestamp=at,
                side=side,
                quantity=quantity,
            )
            risk = evaluate_order(
                intent,
                portfolio,
                {"SIM-ETF": price},
                fees["total"],
                RiskLimits(
                    max_order_notional=_ACCOUNT_CAP,
                    max_position_notional=_ACCOUNT_CAP,
                    max_gross_exposure=_ACCOUNT_CAP,
                ),
            )
            global_ok = side == "SELL" or gross(state) + quantity * price <= _GROSS_CAP
            self._emit(
                db,
                state,
                "RISK_CHECKED",
                f"{account} 账户及总风险已检查",
                risk=risk.model_dump(mode="json"),
                global_approved=global_ok,
            )
            if not risk.approved or not global_ok:
                self._emit(
                    db, state, "ORDER_REJECTED", f"{account} 风控拒绝，未模拟成交"
                )
                continue
            fill = SimulationBroker(account).submit(
                intent, risk, price, fees["total"], at
            )
            state["broker_accounts"][account]["portfolio"] = apply_fill(
                portfolio, fill
            ).model_dump(mode="json")
            state["fills"].append(
                {
                    **fill.model_dump(mode="json"),
                    "account": account,
                    "mode": "SIMULATION",
                    "research_request": key,
                }
            )
            self._emit(
                db,
                state,
                "SIMULATED_FILL",
                f"{account} {side} {quantity} 份，费用 {fees['total']} 元",
            )
