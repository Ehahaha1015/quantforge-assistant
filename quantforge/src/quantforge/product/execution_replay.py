"""Quantity-aware paper replay with exact fees, past-liquidity proxy and dividends."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    localcontext,
)
from typing import Any, Literal

import polars as pl

from quantforge.backtest import SIGNAL_SCHEMA
from quantforge.data import logical_content_hash, prepare_bars
from quantforge.domain import OrderIntent
from quantforge.execution.paper import execute_paper_order
from quantforge.portfolio import PortfolioState, apply_fill, create_portfolio
from quantforge.product.dividend_accounting import CashDividend, DividendBook
from quantforge.risk import RiskLimits, evaluate_order

from .pilot.fees import CnFeeProfile, CnTradability, affordable_lots, calculate_fees
from .share_adjustments import (
    ShareAdjustment,
    ShareAdjustmentBook,
    apply_position_adjustment,
)


@dataclass(frozen=True)
class ExecutionPolicy:
    initial_cash: Decimal
    lot_size: int
    volume_unit: Literal["LOTS", "SHARES"]
    participation: Decimal
    slippage_bps: Decimal
    fees: CnFeeProfile
    risk: RiskLimits
    tick: Decimal = Decimal("0.001")
    extra_delay_bars: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.initial_cash,
            self.participation,
            self.slippage_bps,
            self.tick,
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("exact finite Decimal execution values required")
        if (
            self.initial_cash <= 0
            or not 0 < self.participation <= 1
            or not 0 <= self.slippage_bps < 10000
            or self.tick <= 0
            or type(self.lot_size) is not int
            or self.lot_size < 1
            or self.volume_unit not in {"LOTS", "SHARES"}
            or type(self.extra_delay_bars) is not int
            or self.extra_delay_bars not in {0, 1}
            or not isinstance(self.fees, CnFeeProfile)
            or not isinstance(self.risk, RiskLimits)
        ):
            raise ValueError("bounded declared execution policy required")

    def to_json(self) -> dict[str, Any]:
        return {
            "initial_cash": str(self.initial_cash),
            "lot_size": self.lot_size,
            "volume_unit": self.volume_unit,
            "participation": str(self.participation),
            "slippage_bps": str(self.slippage_bps),
            "tick": str(self.tick),
            "extra_delay_bars": self.extra_delay_bars,
            "fees": self.fees.to_json(),
            "risk": self.risk.model_dump(mode="json"),
            "qualification": "DECLARED_ASSUMPTIONS_NOT_BROKER_VERIFIED",
        }


@dataclass(frozen=True)
class WeightRisk:
    max_weight: Decimal = Decimal(1)
    rebalance_band: Decimal = Decimal("0.02")

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_weight, Decimal)
            or not self.max_weight.is_finite()
            or not isinstance(self.rebalance_band, Decimal)
            or not self.rebalance_band.is_finite()
            or not 0 < self.max_weight <= 1
            or not 0 <= self.rebalance_band <= Decimal("0.20")
        ):
            raise ValueError("exact bounded weight and rebalancing band required")

    def to_json(self) -> dict[str, str]:
        return {
            "max_weight": str(self.max_weight),
            "rebalance_band": str(self.rebalance_band),
        }


def _replay(
    bars: pl.DataFrame,
    targets: pl.DataFrame,
    policy: ExecutionPolicy,
    *,
    dividends: tuple[CashDividend, ...] = (),
    blocked_sessions: dict[str, str] | None = None,
    weight_risk: WeightRisk | None = None,
    share_adjustments: tuple[ShareAdjustment, ...] = (),
    share_contract: bool = False,
) -> dict[str, Any]:
    if not isinstance(policy, ExecutionPolicy) or bars.is_empty():
        raise ValueError("bounded bars and ExecutionPolicy required")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    maximum = max(int(value) for value in bars["timestamp"].cast(pl.Int64))
    frame = prepare_bars(bars, as_of=epoch + timedelta(microseconds=maximum))
    if frame["symbol"].n_unique() != 1:
        raise ValueError("single-symbol quantity replay required")
    expected = (
        {**SIGNAL_SCHEMA, "value": pl.String}
        if weight_risk is not None
        else SIGNAL_SCHEMA
    )
    if frame.height > 20000 or dict(targets.schema) != expected:
        raise ValueError("canonical binary Core signal schema required")
    signals = targets.sort(["symbol", "timestamp"])
    if (
        not signals.select("symbol", "timestamp").equals(
            frame.select("symbol", "timestamp")
        )
        or signals["value"].null_count()
    ):
        raise ValueError("complete matching binary signals required")
    if weight_risk is None:
        if not signals["value"].is_in([0.0, 1.0]).all():
            raise ValueError("binary signals required")
    else:
        if not isinstance(weight_risk, WeightRisk):
            raise ValueError("WeightRisk required")
        for value in signals["value"]:
            if (
                not isinstance(value, str)
                or not re.fullmatch(r"(?:0(?:\.[0-9]{1,4})?|1(?:\.0{1,4})?)", value)
                or Decimal(value) > weight_risk.max_weight
            ):
                raise ValueError("canonical weight exceeds declared cap")
    symbol = str(frame["symbol"][0])
    sessions = [
        (epoch + timedelta(microseconds=int(value))).date()
        for value in frame["timestamp"].cast(pl.Int64)
    ]
    if len(set(sessions)) != len(sessions):
        raise ValueError("one daily execution event per session required")
    if any(event.symbol != symbol for event in dividends):
        raise ValueError("matching dividend identity required")
    relevant = tuple(
        event for event in dividends if sessions[0] <= event.record_date <= sessions[-1]
    )
    book = DividendBook(relevant)
    if any(event.symbol != symbol for event in share_adjustments):
        raise ValueError("matching share event symbol required")
    share_book = ShareAdjustmentBook(
        tuple(
            event
            for event in share_adjustments
            if sessions[0] <= event.record_session <= sessions[-1]
        )
    )
    if {event.record_session for event in share_book.events} & {
        event.record_date for event in relevant
    }:
        raise ValueError(
            "same-close cash/share registration requires explicit issuer ordering"
        )
    portfolio = create_portfolio(policy.initial_cash)
    flow = Decimal(0)
    orders: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    dividend_events: list[dict[str, Any]] = []
    share_entries: list[dict[str, Any]] = []
    blocked = blocked_sessions or {}
    rules = CnTradability(lot_size=policy.lot_size)
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        peak = policy.initial_cash
        drawdown = Decimal(0)
        for index, row in enumerate(frame.iter_rows(named=True)):
            session = sessions[index]
            moment = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
            opening, close = Decimal(str(row["open"])), Decimal(str(row["close"]))
            held = next(
                (
                    position.quantity
                    for position in portfolio.positions
                    if position.symbol == symbol
                ),
                Decimal(0),
            )
            target_index = index - 1 - policy.extra_delay_bars
            target = (
                Decimal(str(signals["value"][target_index]))
                if target_index >= 0
                else Decimal(0)
            )
            desired = (
                (portfolio.cash + held * opening + book.receivable)
                * target
                / opening
                / policy.lot_size
            ).to_integral_value(ROUND_FLOOR) * policy.lot_size
            delta = desired - held
            equity_open = portfolio.cash + held * opening + book.receivable
            current_weight = held * opening / equity_open if equity_open else Decimal(0)
            if (
                weight_risk is not None
                and target > 0
                and current_weight <= weight_risk.max_weight
                and abs(current_weight - target) < weight_risk.rebalance_band
            ):
                delta = Decimal(0)
            if delta:
                side: Literal["BUY", "SELL"] = "BUY" if delta > 0 else "SELL"
                requested = abs(delta)
                status = "REJECTED"
                reason = blocked.get(session.isoformat())
                quantity = Decimal(0)
                fee = Decimal(0)
                risk_reasons = []
                price = (
                    opening * (1 + policy.slippage_bps / 10000)
                    if side == "BUY"
                    else opening * (1 - policy.slippage_bps / 10000)
                )
                price = (price / policy.tick).to_integral_value(
                    ROUND_CEILING if side == "BUY" else ROUND_FLOOR
                ) * policy.tick
                if price <= 0:
                    reason = "PRICE_BELOW_DECLARED_TICK"
                if reason is None:
                    previous_volume = (
                        Decimal(str(frame["volume"][index - 1]))
                        if index
                        else Decimal(0)
                    )
                    if policy.volume_unit == "LOTS":
                        previous_volume *= 100
                    if share_contract and index:
                        previous_volume *= share_book.unit_factor(
                            sessions[index - 1], session
                        )
                    capacity = (
                        previous_volume * policy.participation / policy.lot_size
                    ).to_integral_value(ROUND_FLOOR) * policy.lot_size
                    order_cap = (
                        policy.risk.max_order_notional / price / policy.lot_size
                    ).to_integral_value(ROUND_FLOOR) * policy.lot_size
                    quantity = min(requested, capacity, order_cap)
                    if side == "BUY":
                        quantity = min(
                            quantity,
                            Decimal(
                                affordable_lots(
                                    portfolio.cash,
                                    price,
                                    policy.fees,
                                    rules,
                                    max_lots=int(quantity / policy.lot_size),
                                )
                                * policy.lot_size
                            ),
                        )
                    else:
                        quantity = min(quantity, held)
                    if side == "BUY" and weight_risk is not None:
                        lower, upper = 0, int(quantity / policy.lot_size)
                        while lower < upper:
                            middle = (lower + upper + 1) // 2
                            candidate = Decimal(middle * policy.lot_size)
                            estimate = calculate_fees(
                                side, candidate, price, policy.fees
                            )["total"]
                            after_equity = (
                                equity_open - estimate - candidate * (price - opening)
                            )
                            after_exposure = (held + candidate) * opening
                            if (
                                after_equity > 0
                                and after_exposure
                                <= after_equity * weight_risk.max_weight
                            ):
                                lower = middle
                            else:
                                upper = middle - 1
                        quantity = Decimal(lower * policy.lot_size)
                    if quantity <= 0:
                        reason = "PAST_LIQUIDITY_OR_CASH_INSUFFICIENT"
                    else:
                        fee = calculate_fees(side, quantity, price, policy.fees)[
                            "total"
                        ]
                        intent = OrderIntent(
                            strategy_id="weighted-share-actions-v3"
                            if share_contract
                            else "weighted-quantity-replay-v2"
                            if weight_risk is not None
                            else "quantity-replay-v1",
                            symbol=symbol,
                            timestamp=moment,
                            side=side,
                            quantity=quantity,
                        )
                        risk = evaluate_order(
                            intent, portfolio, {symbol: price}, fee, policy.risk
                        )
                        if not risk.approved:
                            reason = "CORE_RISK_REJECTED"
                            risk_reasons = [value.value for value in risk.reasons]
                        else:
                            fill = execute_paper_order(intent, risk, price, moment, fee)
                            portfolio = apply_fill(portfolio, fill)
                            flow += quantity * price * (1 if side == "BUY" else -1)
                            status = "FILLED" if quantity == requested else "PARTIAL"
                            reason = "DECLARED_PAPER_FILL"
                orders.append(
                    {
                        "session": session.isoformat(),
                        "side": side,
                        "requested": str(requested),
                        "filled": str(quantity) if status != "REJECTED" else "0",
                        "price": str(price),
                        "status": status,
                        "reason": reason,
                        "fee": str(fee) if status != "REJECTED" else "0",
                        "estimated_fee": str(fee),
                        "risk_reasons": risk_reasons,
                        "known_volume_from": sessions[index - 1].isoformat()
                        if index
                        else None,
                        **(
                            {
                                "weight_before_open": str(current_weight),
                                "weight_after_open": str(
                                    (held + (quantity if side == "BUY" else -quantity))
                                    * opening
                                    / (
                                        equity_open
                                        - fee
                                        - quantity
                                        * (price - opening)
                                        * (1 if side == "BUY" else -1)
                                    )
                                )
                                if status != "REJECTED"
                                else str(current_weight),
                            }
                            if weight_risk is not None
                            else {}
                        ),
                    }
                )
            quantity = next(
                (
                    position.quantity
                    for position in portfolio.positions
                    if position.symbol == symbol
                ),
                Decimal(0),
            )
            prior_paid = book.cash
            entries = book.observe(session, quantity)
            dividend_events.extend(
                {"session": session.isoformat(), **entry} for entry in entries
            )
            income = book.cash - prior_paid
            if income:
                portfolio = PortfolioState.model_validate(
                    {
                        **portfolio.model_dump(),
                        "cash": portfolio.cash + income,
                    }
                )
            if share_contract:
                adjusted, action_entries = share_book.apply_close(session, quantity)
                if adjusted != adjusted.to_integral_value():
                    raise ValueError(
                        "V3 declared execution requires whole post-action shares"
                    )
                for entry in action_entries:
                    portfolio = apply_position_adjustment(portfolio, entry)
                    close /= Decimal(entry["ratio"])
                quantity = adjusted
                share_entries.extend(
                    {"session": session.isoformat(), **entry}
                    for entry in action_entries
                )
            if (
                portfolio.cash + flow + portfolio.cumulative_fees
                != policy.initial_cash + book.cash
            ):
                raise ValueError("cash/fill/fee/dividend conservation failed")
            if portfolio.cash < 0 or quantity < 0:
                raise ValueError("nonnegative paper account required")
            equity = portfolio.cash + quantity * close + book.receivable
            peak = max(peak, equity)
            drawdown = min(drawdown, equity / peak - 1)
            periods.append(
                {
                    "session": session.isoformat(),
                    "cash": str(portfolio.cash),
                    "quantity": str(quantity),
                    "fees": str(portfolio.cumulative_fees),
                    "dividend_cash": str(book.cash),
                    "receivable": str(book.receivable),
                    "equity": str(equity),
                    "target": str(target),
                    **(
                        {
                            "mark": str(close),
                            "raw_close": str(row["close"]),
                            "mark_basis": "THEORETICAL_POST_ACTION_UNIT_MARK_NOT_OBSERVED_PRICE"
                            if action_entries
                            else "OBSERVED_RAW_CLOSE",
                        }
                        if share_contract
                        else {}
                    ),
                    **(
                        {
                            "weight_at_close": str(quantity * close / equity)
                            if equity
                            else "0",
                            "overweight_at_close": quantity * close
                            > equity * weight_risk.max_weight,
                            "cash_fraction_at_close": str(portfolio.cash / equity)
                            if equity
                            else "0",
                        }
                        if weight_risk is not None
                        else {}
                    ),
                }
            )
        net_return = Decimal(periods[-1]["equity"]) / policy.initial_cash - 1
    return {
        "schema_version": "weighted-share-actions-v3"
        if share_contract
        else "weighted-quantity-replay-v2"
        if weight_risk is not None
        else "quantity-execution-replay-v1",
        "mode": "PAPER_RESEARCH",
        "symbol": symbol,
        "content_hash": logical_content_hash(frame),
        "policy": policy.to_json(),
        **({"weight_risk": weight_risk.to_json()} if weight_risk is not None else {}),
        **(
            {
                "market_weight_breach_days": sum(
                    row["overweight_at_close"] for row in periods
                )
            }
            if weight_risk is not None
            else {}
        ),
        "orders": orders,
        "periods": periods,
        "dividend_events": dividend_events,
        "dividend_book": book.snapshot(),
        **(
            {
                "share_adjustment_events": share_entries,
                "share_book": share_book.snapshot(),
                "outside_range_share_adjustments": [
                    event.to_json()
                    for event in share_adjustments
                    if event not in share_book.events
                ],
                "share_contract": "EOD_AFTER_RAW_CLOSE; PREVIOUS_VOLUME_CONVERTED_TO_CURRENT_QUOTE_UNITS",
                "sell_quantity_policy": "DECLARED_WHOLE_SHARE_SELLS_NOT_BROKER_VERIFIED",
                "total_return_qualified": False,
            }
            if share_contract
            else {}
        ),
        "final_equity": periods[-1]["equity"],
        "max_drawdown": str(drawdown),
        "net_return": str(net_return),
        "fill_count": sum(row["status"] in {"FILLED", "PARTIAL"} for row in orders),
        "partial_count": sum(row["status"] == "PARTIAL" for row in orders),
        "rejected_count": sum(row["status"] == "REJECTED" for row in orders),
        "fee_total": str(portfolio.cumulative_fees),
        "validated_alpha": False,
        "live_eligible": False,
        "limitations": [
            "Daily UTC session labels, not authenticated intraday execution timestamps.",
            "Prior-session volume is a capacity proxy, not an auction/depth fill guarantee.",
            "Lot, fee, tick, risk and slippage are declared assumptions.",
            "Partial orders recompute remaining target; minimum fees apply to every fill.",
            "Cash distributions available after the observed close; no implicit reinvestment.",
            "Daily sequence cannot certify intraday T+1, halts or price-limit compliance.",
        ],
    }


def replay_quantities(
    bars: pl.DataFrame,
    targets: pl.DataFrame,
    policy: ExecutionPolicy,
    *,
    dividends: tuple[CashDividend, ...] = (),
    blocked_sessions: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _replay(
        bars,
        targets,
        policy,
        dividends=dividends,
        blocked_sessions=blocked_sessions,
    )


def replay_weights(
    bars: pl.DataFrame,
    weights: pl.DataFrame,
    policy: ExecutionPolicy,
    weight_risk: WeightRisk,
    *,
    dividends: tuple[CashDividend, ...] = (),
    blocked_sessions: dict[str, str] | None = None,
) -> dict[str, Any]:
    if set(weights.columns) != {"symbol", "timestamp", "weight"}:
        raise ValueError("versioned weight input schema required")
    return _replay(
        bars,
        weights.rename({"weight": "value"}),
        policy,
        dividends=dividends,
        blocked_sessions=blocked_sessions,
        weight_risk=weight_risk,
    )


def replay_adjusted_weights(
    bars: pl.DataFrame,
    weights: pl.DataFrame,
    policy: ExecutionPolicy,
    weight_risk: WeightRisk,
    *,
    adjustments: tuple[ShareAdjustment, ...],
    dividends: tuple[CashDividend, ...] = (),
    price_basis: Literal["RAW_UNADJUSTED"] = "RAW_UNADJUSTED",
) -> dict[str, Any]:
    """V3 explicit share events; V1/V2 retain their original meanings."""
    if price_basis != "RAW_UNADJUSTED" or set(weights.columns) != {
        "symbol",
        "timestamp",
        "weight",
    }:
        raise ValueError("explicit raw prices and versioned weights required")
    return _replay(
        bars,
        weights.rename({"weight": "value"}),
        policy,
        weight_risk=weight_risk,
        dividends=dividends,
        share_adjustments=adjustments,
        share_contract=True,
    )
