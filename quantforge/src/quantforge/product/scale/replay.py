"""Pure multi-symbol historical paper step; persistence is the host's duty."""

from __future__ import annotations

import hashlib
from decimal import (
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    InvalidOperation,
    localcontext,
)
from typing import Any, Literal

import polars as pl

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
from quantforge.strategies import generate_moving_average_signals

from .evaluator import strategy, synchronized_frames


def replay_step(
    frames: dict[str, pl.DataFrame],
    proposal: dict[str, Any],
    cursor: int,
    state_dict: dict[str, Any],
    fee_bps: str = "10",
) -> dict[str, Any]:
    """Process exactly one synchronized date in deterministic symbol order.

    The returned cursor/state must commit atomically with all fills at the
    host boundary. The pure function cannot prevent callers discarding state.
    Replaying identical inputs returns identical events, never external orders.
    """
    recipe = strategy(proposal)
    if type(cursor) is not int or cursor < 0:
        raise ValueError("REPLAY_CURSOR")
    if not isinstance(fee_bps, str):
        raise TypeError("REPLAY_FEE")
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        try:
            fee_rate = Decimal(fee_bps)
            if not fee_rate.is_finite() or not 0 < fee_rate < 10000:
                raise ValueError("REPLAY_FEE")
            normalized_fee = str(fee_rate.normalize())
        except InvalidOperation:
            raise ValueError("REPLAY_FEE") from None
    selected = synchronized_frames(frames, end_bars=cursor + 1)
    binding = hashlib.sha256(
        canonical_json(
            {
                "proposal": proposal,
                "symbols": sorted(selected),
                "fee_bps": normalized_fee,
                "allocation_fraction": "0.1",
                "risk_limits": {
                    "order": "20000",
                    "position": "50000",
                    "gross": "80000",
                },
            }
        ).encode()
    ).hexdigest()
    if not isinstance(state_dict, dict):
        raise TypeError("REPLAY_STATE")
    if not state_dict:
        if cursor != 0:
            raise ValueError("REPLAY_INITIAL_CURSOR")
        state = create_portfolio(Decimal(100000))
        processed: list[str] = []
    else:
        if set(state_dict) != {
            "portfolio",
            "cursor",
            "recipe_hash",
            "processed_order_ids",
        }:
            raise ValueError("REPLAY_STATE_SCHEMA")
        if state_dict["cursor"] != cursor or type(state_dict["cursor"]) is not int:
            raise ValueError("REPLAY_CURSOR_STATE_MISMATCH")
        if state_dict["recipe_hash"] != binding:
            raise ValueError("REPLAY_RECIPE_CHANGED")
        state = PortfolioState.model_validate(state_dict["portfolio"])
        ids = state_dict["processed_order_ids"]
        if (
            not isinstance(ids, list)
            or any(not isinstance(v, str) for v in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("REPLAY_DUPLICATE_ORDER_HISTORY")
        processed = list(ids)
    if any(p.symbol not in selected for p in state.positions):
        raise ValueError("REPLAY_POSITION_UNIVERSE")
    reference = next(iter(selected.values()))
    moment = reference["timestamp"][cursor]
    prices = {s: Decimal(str(f["open"][cursor])) for s, f in selected.items()}
    closes = {s: Decimal(str(f["close"][cursor])) for s, f in selected.items()}
    intents: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    desired: dict[str, int] = {}
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        for symbol, frame in selected.items():
            signal = 0
            if cursor >= recipe.long_window:
                signal = int(
                    generate_moving_average_signals(frame.head(cursor), recipe)[
                        "value"
                    ][-1]
                )
            desired[symbol] = signal
            held = next(
                (p.quantity for p in state.positions if p.symbol == symbol), Decimal(0)
            )
            quantity, side = Decimal(0), "BUY"
            if signal and held == 0:
                quantity = (
                    state.cash * Decimal("0.1") / prices[symbol]
                ).to_integral_value(rounding=ROUND_FLOOR)
            elif not signal and held > 0:
                quantity, side = held, "SELL"
            if quantity <= 0:
                continue
            chosen_side: Literal["BUY", "SELL"] = "BUY" if side == "BUY" else "SELL"
            order = OrderIntent(
                strategy_id="scale-replay-" + binding,
                symbol=symbol,
                timestamp=moment,
                side=chosen_side,
                quantity=quantity,
            )
            fee = quantity * prices[symbol] * fee_rate / Decimal(10000)
            required = {p.symbol for p in state.positions} | {symbol}
            risk = evaluate_order(
                order,
                state,
                {s: prices[s] for s in sorted(required)},
                fee,
                RiskLimits(
                    max_order_notional="20000",
                    max_position_notional="50000",
                    max_gross_exposure="80000",
                ),
            )
            intents.append(order.model_dump(mode="json"))
            decisions.append({"symbol": symbol, **risk.model_dump(mode="json")})
            if risk.approved:
                filled = execute_paper_order(order, risk, prices[symbol], moment, fee)
                if filled.order_id in processed:
                    raise ValueError("REPLAY_DUPLICATE_FILL")
                state = apply_fill(state, filled)
                processed.append(filled.order_id)
                fills.append(filled.model_dump(mode="json"))
        valuation = value_portfolio(
            state, {p.symbol: closes[p.symbol] for p in state.positions}, moment
        ).model_dump(mode="json")
    return {
        "schema_version": "scale-replay-step-v1",
        "mode": "HISTORICAL_PAPER_ONLY",
        "cursor": cursor + 1,
        "timestamp": moment.isoformat(),
        "desired_positions": desired,
        "state": {
            "portfolio": state.model_dump(mode="json"),
            "cursor": cursor + 1,
            "recipe_hash": binding,
            "processed_order_ids": processed,
        },
        "intents": intents,
        "risk": decisions,
        "fills": fills,
        "valuation": valuation,
        "allocation": "10pct_current_cash_whole_units_sequential_sorted_symbols",
        "price_assumption": "normalized_adjusted_unit_hypothetical_capital_not_executable_market_lots",
        "real_transport": "LOCKED",
        "live_eligible": False,
    }
