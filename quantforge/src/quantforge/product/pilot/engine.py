"""Pure single-symbol CN paper step with declared A-share frictions.

One call processes exactly one bar: prior-close SMA signal, next-open intent,
board-lot sizing with fee-inclusive affordability, conservative price-limit and
T+1 guards, the frozen risk gateway, frozen paper execution and portfolio
accounting. The pure function performs no IO; committing the returned state is
the host's duty, and a discarded state simply replays identical events.

This is a paper-only operational profile. It cannot emit an external order and
grants no validated-alpha or live status.
"""

from __future__ import annotations

import hashlib
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

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

from .fees import (
    CnFeeProfile,
    CnTradability,
    affordable_lots,
    buy_fillable,
    calculate_fees,
    limit_prices,
    sell_fillable,
)

__all__ = ["PilotConfig", "PilotEngineError", "pilot_binding", "pilot_step"]

_PRECISION = 50
_STATE_KEYS = {"portfolio", "cursor", "binding", "processed_order_ids", "last_buy"}


class PilotEngineError(ValueError):
    """Sanitized CN pilot engine precondition failure."""


class PilotConfig(BaseModel):
    """One frozen pilot recipe; identity is the canonical JSON of this model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    short_window: int
    long_window: int
    initial_cash: Decimal
    strategy: Literal["sma", "canary_hold"] = "sma"
    sizing: Literal["all_in", "fraction", "one_lot"] = "all_in"
    allocation_fraction: Decimal = Decimal(1)
    max_lots_per_order: int | None = None
    fee_profile: CnFeeProfile
    tradability: CnTradability
    risk_limits: RiskLimits

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        if not value or value.strip() != value or len(value) > 32:
            raise PilotEngineError("symbol must be a trimmed nonempty identifier")
        return value

    @field_validator("initial_cash", "allocation_fraction", mode="before")
    @classmethod
    def _decimal_field(cls, value: Any, info: Any) -> Decimal:
        if isinstance(value, (bool, float)) or not isinstance(
            value, (Decimal, int, str)
        ):
            raise PilotEngineError(
                f"{info.field_name} must be an exact Decimal, int, or str"
            )
        try:
            number = Decimal(value)
        except (ArithmeticError, ValueError) as exc:
            raise PilotEngineError(
                f"{info.field_name} must be a decimal number"
            ) from exc
        if not number.is_finite() or number <= 0:
            raise PilotEngineError(f"{info.field_name} must be positive")
        return number

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise PilotEngineError(f"pilot configuration is invalid: {exc}") from exc
        if not 0 < self.short_window < self.long_window:
            raise PilotEngineError("require 0 < short_window < long_window")
        if self.sizing == "fraction" and self.allocation_fraction > 1:
            raise PilotEngineError("allocation_fraction must be within (0, 1]")
        if self.strategy == "canary_hold" and self.sizing != "one_lot":
            raise PilotEngineError("canary_hold requires one_lot sizing")
        if self.max_lots_per_order is not None and self.max_lots_per_order < 1:
            raise PilotEngineError("max_lots_per_order must be positive when set")

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "short_window": self.short_window,
            "long_window": self.long_window,
            "initial_cash": str(self.initial_cash),
            "strategy": self.strategy,
            "sizing": self.sizing,
            "allocation_fraction": str(self.allocation_fraction),
            "max_lots_per_order": self.max_lots_per_order,
            "fee_profile": self.fee_profile.to_json(),
            "tradability": self.tradability.to_json(),
            "risk_limits": {
                "max_order_notional": str(self.risk_limits.max_order_notional),
                "max_position_notional": str(self.risk_limits.max_position_notional),
                "max_gross_exposure": str(self.risk_limits.max_gross_exposure),
            },
        }


def pilot_binding(config: PilotConfig) -> str:
    """Deterministic recipe identity stored in every pilot state step."""
    return hashlib.sha256(canonical_json(config.to_json()).encode()).hexdigest()


def _ticket(
    side: Literal["BUY", "SELL"],
    quantity: int | None,
    price: Decimal,
    config: PilotConfig,
    status: str,
    note: str,
) -> dict[str, Any]:
    est = (
        calculate_fees(side, Decimal(quantity), price, config.fee_profile)
        if quantity
        else None
    )
    return {
        "side": side,
        "quantity": str(quantity) if quantity else None,
        "reference_price": str(price),
        "estimated_fees": (
            {
                "commission": str(est["commission"]),
                "stamp_duty": str(est["stamp_duty"]),
                "transfer_fee": str(est["transfer_fee"]),
                "total": str(est["total"]),
            }
            if est is not None
            else None
        ),
        "status": status,
        "note": note,
    }


def pilot_step(
    frame: pl.DataFrame,
    config: PilotConfig,
    cursor: int,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Process exactly one daily bar; identical inputs return identical events."""
    if not isinstance(config, PilotConfig):
        raise PilotEngineError("config must be a PilotConfig")
    if type(cursor) is not int or cursor < 0:
        raise PilotEngineError("PILOT_CURSOR")
    if cursor >= frame.height:
        raise PilotEngineError("PILOT_DATA_EXHAUSTED")
    symbols = set(frame["symbol"].unique().to_list())
    if symbols != {config.symbol}:
        raise PilotEngineError("PILOT_FRAME_SYMBOL_MISMATCH")
    binding = pilot_binding(config)
    if not isinstance(state, dict):
        raise PilotEngineError("PILOT_STATE")
    if not state:
        if cursor != 0:
            raise PilotEngineError("PILOT_INITIAL_CURSOR")
        portfolio = create_portfolio(config.initial_cash)
        processed: list[str] = []
        last_buy: dict[str, Any] | None = None
    else:
        if set(state) != _STATE_KEYS:
            raise PilotEngineError("PILOT_STATE_SCHEMA")
        if state["cursor"] != cursor or type(state["cursor"]) is not int:
            raise PilotEngineError("PILOT_CURSOR_STATE_MISMATCH")
        if state["binding"] != binding:
            raise PilotEngineError("PILOT_RECIPE_CHANGED")
        portfolio = PortfolioState.model_validate(state["portfolio"])
        ids = state["processed_order_ids"]
        if (
            not isinstance(ids, list)
            or any(not isinstance(value, str) for value in ids)
            or len(set(ids)) != len(ids)
        ):
            raise PilotEngineError("PILOT_DUPLICATE_ORDER_HISTORY")
        processed = list(ids)
        last_buy = state["last_buy"]
        if last_buy is not None and (
            not isinstance(last_buy, dict)
            or set(last_buy) != {"date", "quantity"}
            or not isinstance(last_buy["date"], str)
        ):
            raise PilotEngineError("PILOT_STATE_SCHEMA")
    if any(position.symbol != config.symbol for position in portfolio.positions):
        raise PilotEngineError("PILOT_POSITION_SYMBOL_MISMATCH")

    moment = frame["timestamp"][cursor]
    date_label = moment.date().isoformat()
    price = Decimal(str(frame["open"][cursor]))
    mark = Decimal(str(frame["close"][cursor]))
    previous_close = Decimal(str(frame["close"][cursor - 1])) if cursor > 0 else None
    desired = 0
    if config.strategy == "canary_hold":
        # Canary: buy one lot on the first affordable session and hold; the
        # human closes the position at the broker and reports it back.
        desired = 1
    else:
        strategy = MovingAverageCrossoverConfig(
            short_window=config.short_window, long_window=config.long_window
        )
        if cursor >= strategy.long_window:
            signals = generate_moving_average_signals(frame.head(cursor), strategy)
            desired = int(signals["value"][-1])
    held = next(
        (p.quantity for p in portfolio.positions if p.symbol == config.symbol),
        Decimal(0),
    )
    ticket: dict[str, Any] | None = None
    intent = decision = fill = None
    with localcontext(Context(prec=_PRECISION, rounding=ROUND_HALF_EVEN)):
        if desired and held == 0:
            limit_up = (
                limit_prices(previous_close, config.tradability)[0]
                if previous_close is not None
                else None
            )
            if limit_up is not None and not buy_fillable(price, limit_up):
                ticket = _ticket(
                    "BUY",
                    None,
                    price,
                    config,
                    "UNFILLABLE_PRICE_LIMIT",
                    f"open {price} at or above limit-up {limit_up}; no fill assumed",
                )
            else:
                lots = affordable_lots(
                    portfolio.cash,
                    price,
                    config.fee_profile,
                    config.tradability,
                    max_lots=config.max_lots_per_order,
                )
                if config.sizing == "fraction":
                    lot = Decimal(config.tradability.lot_size)
                    budget_lots = int(
                        (
                            portfolio.cash * config.allocation_fraction / (price * lot)
                        ).to_integral_value(rounding=ROUND_FLOOR)
                    )
                    lots = min(lots, budget_lots)
                elif config.sizing == "one_lot":
                    # The canary takes the smallest meaningful position: one lot.
                    lots = min(lots, 1)
                if lots < 1:
                    ticket = _ticket(
                        "BUY",
                        None,
                        price,
                        config,
                        "INSUFFICIENT_CASH_FOR_ONE_LOT",
                        f"cash {portfolio.cash} cannot cover one {config.tradability.lot_size}-share lot at {price} plus fees",
                    )
                else:
                    quantity = Decimal(lots * config.tradability.lot_size)
                    order = OrderIntent(
                        strategy_id=binding,
                        symbol=config.symbol,
                        timestamp=moment,
                        side="BUY",
                        quantity=quantity,
                    )
                    fees = calculate_fees("BUY", quantity, price, config.fee_profile)
                    risk = evaluate_order(
                        order,
                        portfolio,
                        {config.symbol: price},
                        fees["total"],
                        config.risk_limits,
                    )
                    intent, decision = (
                        order.model_dump(mode="json"),
                        risk.model_dump(mode="json"),
                    )
                    if risk.approved:
                        filled = execute_paper_order(
                            order, risk, price, moment, fees["total"]
                        )
                        portfolio = apply_fill(portfolio, filled)
                        fill = filled.model_dump(mode="json")
                        processed.append(filled.order_id)
                        last_buy = {
                            "date": date_label,
                            "quantity": str(filled.quantity),
                        }
                        ticket = _ticket(
                            "BUY",
                            int(quantity),
                            price,
                            config,
                            "PAPER_FILLED",
                            "declared paper fill at the next open",
                        )
                    else:
                        ticket = _ticket(
                            "BUY",
                            int(quantity),
                            price,
                            config,
                            "RISK_REJECTED",
                            ";".join(reason.value for reason in risk.reasons),
                        )
        elif not desired and held > 0:
            if (
                config.tradability.t_plus_one
                and last_buy is not None
                and last_buy["date"] == date_label
            ):
                ticket = _ticket(
                    "SELL",
                    int(held),
                    price,
                    config,
                    "T_PLUS_ONE_BLOCKED",
                    "shares bought on this session cannot be sold until T+1",
                )
            else:
                limit_down = (
                    limit_prices(previous_close, config.tradability)[1]
                    if previous_close is not None
                    else None
                )
                if limit_down is not None and not sell_fillable(price, limit_down):
                    ticket = _ticket(
                        "SELL",
                        int(held),
                        price,
                        config,
                        "UNFILLABLE_PRICE_LIMIT",
                        f"open {price} at or below limit-down {limit_down}; no fill assumed",
                    )
                else:
                    order = OrderIntent(
                        strategy_id=binding,
                        symbol=config.symbol,
                        timestamp=moment,
                        side="SELL",
                        quantity=held,
                    )
                    fees = calculate_fees("SELL", held, price, config.fee_profile)
                    risk = evaluate_order(
                        order,
                        portfolio,
                        {config.symbol: price},
                        fees["total"],
                        config.risk_limits,
                    )
                    intent, decision = (
                        order.model_dump(mode="json"),
                        risk.model_dump(mode="json"),
                    )
                    if risk.approved:
                        filled = execute_paper_order(
                            order, risk, price, moment, fees["total"]
                        )
                        portfolio = apply_fill(portfolio, filled)
                        fill = filled.model_dump(mode="json")
                        processed.append(filled.order_id)
                        last_buy = None
                        ticket = _ticket(
                            "SELL",
                            int(held),
                            price,
                            config,
                            "PAPER_FILLED",
                            "declared paper fill at the next open",
                        )
                    else:
                        ticket = _ticket(
                            "SELL",
                            int(held),
                            price,
                            config,
                            "RISK_REJECTED",
                            ";".join(reason.value for reason in risk.reasons),
                        )
        marks = {config.symbol: mark} if portfolio.positions else {}
        valuation = value_portfolio(portfolio, marks, moment).model_dump(mode="json")

    return {
        "cursor": cursor + 1,
        "timestamp": moment.isoformat(),
        "session": date_label,
        "direction": desired,
        "ticket": ticket,
        "order_intent": intent,
        "risk": decision,
        "fill": fill,
        "state": {
            "portfolio": portfolio.model_dump(mode="json"),
            "cursor": cursor + 1,
            "binding": binding,
            "processed_order_ids": processed,
            "last_buy": last_buy,
        },
        "valuation": valuation,
    }


def initial_state(config: PilotConfig) -> dict[str, Any]:
    """The canonical empty state a host persists with the START event."""
    if not isinstance(config, PilotConfig):
        raise PilotEngineError("config must be a PilotConfig")
    return {
        "portfolio": create_portfolio(config.initial_cash).model_dump(mode="json"),
        "cursor": 0,
        "binding": pilot_binding(config),
        "processed_order_ids": [],
        "last_buy": None,
    }
