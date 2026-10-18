"""Deterministic daily report-plan replay with explicit unverified fill assumptions."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Context,
    Decimal,
    localcontext,
)
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .chart_quality import is_session
from .daily_five import report_plan
from .moving_average import _single_symbol_bars

VERSION = "daily-report-replay-v2"


def chandelier_stop(previous: Decimal, highs: list[Decimal], atr: Decimal) -> Decimal:
    """Fixed research overlay, computed at close and effective next bar only."""
    if not highs or any(
        not value.is_finite() or value <= 0 for value in (previous, atr, *highs)
    ):
        raise ValueError("positive finite stop, ATR and completed highs required")
    with localcontext(Context(prec=50, rounding=ROUND_HALF_UP)):
        return max(previous, max(highs[-20:]) - 3 * atr)


def replay(
    bars: pl.DataFrame,
    snapshots: dict[str, dict[str, Any]],
    *,
    initial_cash: str = "1000000",
    friction_bps: int = 10,
    max_holding_bars: int = 5,
    exit_policy: str = "ATR_TREND_TIME",
    lot_size: int = 100,
    tick_size: str = "0.01",
    volume_unit: str = "VENDOR_UNVERIFIED",
    mode: str = "REPORT_PLAN",
    chandelier: bool = False,
) -> dict[str, Any]:
    if (
        friction_bps not in (10, 30)
        or type(lot_size) is not int
        or lot_size < 1
        or mode not in {"REPORT_PLAN", "BUY_AND_HOLD", "CASH"}
        or type(chandelier) is not bool
        or (chandelier and (mode != "REPORT_PLAN" or exit_policy != "ATR_TREND_TIME"))
    ):
        raise ValueError("declared research execution assumptions required")
    frame = _single_symbol_bars(bars)
    zone = ZoneInfo("Asia/Shanghai")
    events: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    with localcontext(Context(prec=50, rounding=ROUND_HALF_UP)):
        cash, tick = Decimal(initial_cash), Decimal(tick_size)
        if not cash.is_finite() or not tick.is_finite() or cash <= 0 or tick <= 0:
            raise ValueError("positive finite research cash and tick required")
        capital = cash
        fee_rate = Decimal(friction_bps) / 20000
        slippage = fee_rate
        quantity = 0
        entry_price = Decimal(0)
        entry_fee = Decimal(0)
        entry_at: datetime | None = None
        initial_levels: dict[str, Any] | None = None
        pending_entry: dict[str, Any] | None = None
        pending_exit: str | None = None
        holding_bars = 0
        market_sessions_held = 0
        peak = cash
        max_drawdown = Decimal(0)
        total_fees = Decimal(0)
        blocked = 0
        mae = Decimal(0)
        mfe = Decimal(0)
        valuation_unverified = False
        trailing_stop: Decimal | None = None
        position_highs: list[Decimal] = []

        def event(at: str, kind: str, **values: Any) -> None:
            events.append({"at": at, "kind": kind, **values})

        def price(value: Decimal, buy: bool) -> Decimal:
            return (
                (value * (1 + slippage if buy else 1 - slippage)) / tick
            ).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR) * tick

        def sell(row: dict[str, Any], reference: Decimal, reason: str) -> bool:
            nonlocal cash, quantity, pending_exit, total_fees, initial_levels, blocked
            nonlocal trailing_stop
            at = row["timestamp"].isoformat()
            if (
                entry_at is None
                or row["timestamp"].astimezone(zone).date()
                <= entry_at.astimezone(zone).date()
            ):
                pending_exit = reason
                blocked += 1
                event(at, "EXIT_BLOCKED_T1", reason=reason)
                return False
            fill = price(reference, False)
            if (
                row["volume"] <= 0
                or row["high"] == row["low"]
                or fill < Decimal(str(row["low"]))
            ):
                pending_exit = reason
                blocked += 1
                event(at, "EXIT_UNCONFIRMED_LIQUIDITY", reason=reason)
                return False
            if volume_unit == "SHARES" and Decimal(quantity) > Decimal(
                str(row["volume"])
            ) * Decimal("0.01"):
                pending_exit = reason
                blocked += 1
                event(at, "EXIT_PARTICIPATION_CAP", reason=reason)
                return False
            gross = fill * quantity
            fee = (gross * fee_rate).quantize(Decimal("0.01"))
            cash += gross - fee
            total_fees += fee
            trades.append(
                {
                    "entry_at": entry_at.isoformat(),
                    "exit_at": at,
                    "quantity": quantity,
                    "entry_price": str(entry_price),
                    "exit_price": str(fill),
                    "fees": str(entry_fee + fee),
                    "research_pnl": str(
                        (fill - entry_price) * quantity - entry_fee - fee
                    ),
                    "holding_bars": holding_bars,
                    "market_sessions_held": market_sessions_held,
                    "mae_fraction": str(mae),
                    "mfe_fraction": str(mfe),
                    "excursion_scope": "DAILY_BAR_BOUNDS_NOT_INTRADAY_ORDER",
                    "exit_time_scope": "DAILY_BAR_TIMESTAMP_NOT_INTRADAY_FILL_TIME",
                    "reason": reason,
                }
            )
            event(
                at,
                "SIMULATED_EXIT",
                price=str(fill),
                quantity=quantity,
                fee=str(fee),
                reason=reason,
            )
            quantity = 0
            pending_exit = None
            initial_levels = None
            trailing_stop = None
            position_highs.clear()
            return True

        for index, row in enumerate(frame.iter_rows(named=True)):
            at = row["timestamp"].isoformat()
            day = row["timestamp"].astimezone(zone).date()
            opened = Decimal(str(row["open"]))
            low = Decimal(str(row["low"]))
            high = Decimal(str(row["high"]))
            snapshot = snapshots.get(at)
            missing = []
            if index:
                cursor = frame["timestamp"][index - 1].astimezone(
                    zone
                ).date() + timedelta(days=1)
                while cursor < day:
                    if is_session(cursor, day):
                        missing.append(cursor.isoformat())
                    cursor += timedelta(days=1)
            if missing:
                event(at, "MISSING_MARKET_SESSIONS", dates=missing)
                if quantity:
                    pending_exit = pending_exit or "DATA_GAP_REVIEW"
                if pending_entry is not None:
                    pending_entry = None
                    blocked += 1
                    event(at, "ENTRY_CANCELLED_DATA_GAP")
            raw_gap = bool(
                index
                and abs(opened / Decimal(str(frame["close"][index - 1])) - 1)
                > Decimal("0.15")
            )
            if raw_gap and quantity:
                valuation_unverified = True
                event(at, "RAW_GAP_VALUATION_UNVERIFIED")
            if quantity:
                market_sessions_held += 1
                if pending_exit:
                    sell(row, opened, pending_exit)
            sold_this_bar = False
            if not quantity and pending_entry is not None:
                if row["volume"] <= 0 or row["high"] == row["low"]:
                    blocked += 1
                    event(at, "ENTRY_UNCONFIRMED_LIQUIDITY")
                elif opened > Decimal(
                    pending_entry["plan"]["entry_gap_upper_reference"]
                ):
                    blocked += 1
                    event(at, "ENTRY_CANCELLED_GAP")
                else:
                    fill = price(opened, True)
                    position = report_plan(
                        pending_entry["inputs"],
                        pending_entry["decision"],
                        max_holding_bars=max_holding_bars,
                        purchase_price=str(fill),
                        exit_policy=exit_policy,
                    )
                    shares = (
                        int(
                            (
                                cash / (fill * (1 + fee_rate)) / lot_size
                            ).to_integral_value(rounding=ROUND_FLOOR)
                        )
                        * lot_size
                    )
                    if volume_unit == "SHARES":
                        capacity = (
                            int(
                                (
                                    Decimal(str(row["volume"]))
                                    * Decimal("0.01")
                                    / lot_size
                                ).to_integral_value(rounding=ROUND_FLOOR)
                            )
                            * lot_size
                        )
                        shares = min(shares, capacity)
                    if (
                        shares < lot_size
                        or fill > high
                        or (
                            mode == "REPORT_PLAN"
                            and (not position["levels"] or raw_gap)
                        )
                    ):
                        blocked += 1
                        event(at, "ENTRY_NO_SUPPORTED_FILL")
                    else:
                        entry_fee = (fill * shares * fee_rate).quantize(Decimal("0.01"))
                        cash -= fill * shares + entry_fee
                        total_fees += entry_fee
                        quantity = shares
                        entry_price = fill
                        entry_at = datetime.combine(day, time(9, 30), zone).astimezone(
                            UTC
                        )
                        holding_bars = 0
                        market_sessions_held = 0
                        mae = Decimal(0)
                        mfe = Decimal(0)
                        initial_levels = position["levels"]
                        if chandelier and initial_levels:
                            trailing_stop = Decimal(initial_levels["stop_reference"])
                        event(
                            at,
                            "SIMULATED_ENTRY",
                            price=str(fill),
                            quantity=shares,
                            fee=str(entry_fee),
                            signal_at=pending_entry["inputs"]["as_of_bar"],
                        )
                pending_entry = None
            if quantity:
                holding_bars += 1
                mae = min(mae, low / entry_price - 1)
                mfe = max(mfe, high / entry_price - 1)
                market_sessions_held = (
                    holding_bars + len(missing)
                    if market_sessions_held < holding_bars
                    else market_sessions_held + len(missing)
                )
                if (
                    mode == "REPORT_PLAN"
                    and initial_levels
                    and exit_policy in {"ATR_TREND_TIME", "ATR_TIME"}
                ):
                    stop = (
                        trailing_stop
                        if chandelier and trailing_stop is not None
                        else Decimal(initial_levels["stop_reference"])
                    )
                    take = Decimal(initial_levels["take_profit_reference"])
                    hit_stop = low <= stop
                    hit_take = high >= take
                    if hit_stop or hit_take:
                        reason = (
                            "STOP_FIRST_BOTH_REFERENCES"
                            if hit_stop and hit_take
                            else "STOP_REFERENCE"
                            if hit_stop
                            else "TAKE_PROFIT_REFERENCE"
                        )
                        reference = (
                            min(opened, stop)
                            if hit_stop
                            else min(opened, take)
                            if opened >= take
                            else take
                        )
                        sold_this_bar = sell(row, reference, reason)
            if snapshot is None:
                if quantity:
                    pending_exit = "MISSING_REPORT_INPUT"
                event(at, "REPORT_INPUT_MISSING")
            elif mode == "REPORT_PLAN":
                plan = report_plan(
                    snapshot["inputs"],
                    snapshot["decision"],
                    max_holding_bars=max_holding_bars,
                    purchase_price=str(entry_price) if quantity else None,
                    held_bars=holding_bars if quantity else 0,
                    initial_levels=initial_levels if quantity else None,
                    exit_policy=exit_policy,
                )
                event(
                    at,
                    "REPORT_DECISION",
                    action=plan["action"],
                    entry_action=plan["entry_action"],
                    position_action=plan["position_action"],
                    policy=snapshot["decision"]["policy"],
                    context=snapshot["decision"]["context_relation"],
                )
                if quantity and plan["position_action"] == "EXIT_REVIEW":
                    pending_exit = pending_exit or "REPORT_EXIT_REVIEW"
                elif (
                    not quantity
                    and not sold_this_bar
                    and plan["entry_action"] == "CONSIDER_ENTRY"
                ):
                    pending_entry = {**snapshot, "plan": plan}
            elif mode == "BUY_AND_HOLD" and not quantity:
                if snapshot:
                    plan = report_plan(snapshot["inputs"], snapshot["decision"])
                    if plan.get("entry_gap_upper_reference"):
                        # A passive comparator ignores signal eligibility and chase filters.
                        plan = deepcopy_plan(plan)
                        plan["entry_gap_upper_reference"] = "1e30"
                        pending_entry = {**snapshot, "plan": plan}
            if chandelier and quantity and trailing_stop is not None:
                position_highs.append(high)
                position_highs[:] = position_highs[-20:]
                if snapshot and not snapshot["decision"]["risk_flags"]:
                    atr = Decimal(snapshot["inputs"]["features"]["atr14"])
                    updated = chandelier_stop(trailing_stop, position_highs, atr)
                    if updated > trailing_stop:
                        event(
                            at,
                            "CHANDELIER_UPDATE",
                            previous=str(trailing_stop),
                            stop=str(updated),
                            effective_after=at,
                        )
                    trailing_stop = updated
            equity = cash + Decimal(str(row["close"])) * quantity
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity / peak - 1)
            curve.append(
                {
                    "at": at,
                    "cash": str(cash),
                    "quantity": quantity,
                    "equity": str(equity),
                    "day": day.isoformat(),
                }
            )
        ending_return = str(equity / capital - 1)
    return {
        **({"exit_overlay": "CHANDELIER_20_HIGH_3_ATR_NEXT_BAR"} if chandelier else {}),
        "schema_version": VERSION,
        "mode": mode,
        "initial_cash": str(capital),
        "ending_equity": str(equity),
        "price_return_after_assumed_costs": ending_return,
        "max_drawdown": str(max_drawdown),
        "closed_trades": len(trades),
        "open_quantity": quantity,
        "total_fees": str(total_fees),
        "blocked_attempts": blocked,
        "friction_bps_each_side": friction_bps,
        "lot_size_assumption": lot_size,
        "volume_participation_applied": volume_unit == "SHARES",
        "events": events,
        "trades": trades,
        "equity_curve": curve,
        "execution_qualified": False,
        "forecast_validated": False,
        "valuation_verified": False,
        "large_raw_gap_while_held": valuation_unverified,
        "limitations": [
            "Virtual cash; no orders or broker access.",
            "Raw prices exclude unverified corporate-action cashflows.",
            "Security limit schedules and lots are research assumptions, not historical eligibility proof.",
            "Vendor volume units unknown unless explicitly SHARES; capacity not fabricated.",
            "End equity includes unrealized mark-to-market; no forced fictitious final fill.",
            "Daily OHLC/volume only screens hypothetical fills ex post; intraday queue and fill times unverified.",
        ],
    }


def deepcopy_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {**plan, "levels": dict(plan["levels"]) if plan.get("levels") else None}
