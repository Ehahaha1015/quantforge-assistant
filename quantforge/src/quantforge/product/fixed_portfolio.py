"""Fixed multi-asset paper weights over one Core cash account; no retrospective picks."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, localcontext
from typing import Any, Literal

import polars as pl

from quantforge.domain import OrderIntent
from quantforge.execution.paper import execute_paper_order
from quantforge.portfolio import PortfolioState, apply_fill, create_portfolio
from quantforge.risk import evaluate_order

from .dividend_accounting import CashDividend, DividendBook
from .execution_replay import ExecutionPolicy
from .pilot.fees import CnTradability, affordable_lots, calculate_fees
from .share_adjustments import (
    ShareAdjustment,
    ShareAdjustmentBook,
    apply_position_adjustment,
)
from .trend_refinement import _frame


def _replay_fixed_portfolio(
    assets: dict[str, pl.DataFrame],
    weights: dict[str, Decimal],
    policy: ExecutionPolicy,
    *,
    start_session: date,
    band: Decimal = Decimal(0),
    max_gross_weight: Decimal = Decimal(1),
    dividends: tuple[CashDividend, ...] = (),
    adjustments: tuple[ShareAdjustment, ...] = (),
    share_contract: bool = False,
) -> dict[str, Any]:
    if not 1 <= len(assets) <= 5 or set(assets) != set(weights):
        raise ValueError("one to five matching fixed assets required")
    if any(
        not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1
        for value in weights.values()
    ):
        raise ValueError("exact finite fixed weights required")
    if (
        not isinstance(band, Decimal)
        or not band.is_finite()
        or not 0 <= band <= Decimal("0.2")
    ):
        raise ValueError("exact bounded rebalance band required")
    if (
        not isinstance(max_gross_weight, Decimal)
        or not max_gross_weight.is_finite()
        or not 0 < max_gross_weight <= 1
        or sum(weights.values()) > max_gross_weight
    ):
        raise ValueError("fixed weights exceed gross cap")
    if (
        type(start_session) is not date
        or not isinstance(policy, ExecutionPolicy)
        or policy.extra_delay_bars != 0
    ):
        raise ValueError("declared start and execution policy required")
    frames = {key: _frame(value) for key, value in sorted(assets.items())}
    if any(
        len({stamp.date() for stamp in frame["timestamp"]}) != frame.height
        for frame in frames.values()
    ):
        raise ValueError("one quote per asset daily session required")
    if any(frame["symbol"][0] != key for key, frame in frames.items()):
        raise ValueError("asset identity mismatch")
    rows = {
        key: {row["timestamp"].date(): row for row in frame.iter_rows(named=True)}
        for key, frame in frames.items()
    }
    end = min(max(values) for values in rows.values())
    sessions = sorted(set.union(*(set(value) for value in rows.values())))
    if start_session not in sessions or any(
        start_session <= min(value) for value in rows.values()
    ):
        raise ValueError("all assets require a prior session and a visible start")
    sessions = [value for value in sessions if start_session <= value <= end]
    if (
        not sessions
        or len(sessions) > 20000
        or any(
            event.symbol not in assets or event.record_date < start_session
            for event in dividends
        )
    ):
        raise ValueError("bounded matching cash events required")
    books = {
        key: DividendBook(tuple(event for event in dividends if event.symbol == key))
        for key in frames
    }
    if any(
        event.symbol not in assets or event.record_session < start_session
        for event in adjustments
    ):
        raise ValueError("matching share events at or after account start required")
    share_books = {
        key: ShareAdjustmentBook(
            tuple(
                event
                for event in adjustments
                if event.symbol == key and event.record_session <= end
            )
        )
        for key in frames
    }
    if any(
        {event.record_session for event in share_books[key].events}
        & {event.record_date for event in books[key].events}
        for key in frames
    ):
        raise ValueError(
            "same-close cash/share registration requires explicit issuer ordering"
        )
    previous = {
        key: max(value for value in values if value < start_session)
        for key, values in rows.items()
    }
    marks = {key: Decimal(str(rows[key][previous[key]]["close"])) for key in frames}
    portfolio = create_portfolio(policy.initial_cash)
    orders = []
    periods = []
    dividend_entries: list[dict[str, Any]] = []
    share_entries: list[dict[str, Any]] = []
    mark_basis = {key: "OBSERVED_RAW_CLOSE" for key in frames}
    rules = CnTradability(lot_size=policy.lot_size)
    flow = Decimal(0)
    with localcontext(Context(prec=50)):
        peak = policy.initial_cash
        drawdown = Decimal(0)
        for session in sessions:
            missing = [key for key in frames if session not in rows[key]]
            held = {
                key: next(
                    (
                        position.quantity
                        for position in portfolio.positions
                        if position.symbol == key
                    ),
                    Decimal(0),
                )
                for key in frames
            }
            openings = {
                key: Decimal(str(rows[key][session]["open"]))
                if key not in missing
                else marks[key]
                for key in frames
            }
            equity_open = (
                portfolio.cash
                + sum(held[key] * openings[key] for key in frames)
                + sum(book.receivable for book in books.values())
            )
            desired = {
                key: (
                    equity_open * weights[key] / openings[key] / policy.lot_size
                ).to_integral_value(ROUND_FLOOR)
                * policy.lot_size
                for key in frames
            }
            deltas = {key: desired[key] - held[key] for key in frames}
            for key in frames:
                current = held[key] * openings[key] / equity_open
                gross = (
                    sum(held[item] * openings[item] for item in frames) / equity_open
                )
                if (
                    weights[key] > 0
                    and abs(current - weights[key]) < band
                    and gross <= max_gross_weight
                ):
                    deltas[key] = Decimal(0)
            # Freeze simultaneous targets before effects; sell first, alphabetical buys.
            schedule = sorted(frames, key=lambda key: (deltas[key] >= 0, key))
            if not missing:
                for key in schedule:
                    delta = deltas[key]
                    if not delta:
                        continue
                    side: Literal["BUY", "SELL"] = "BUY" if delta > 0 else "SELL"
                    requested = abs(delta)
                    price = openings[key] * (
                        1 + policy.slippage_bps / 10000 * (1 if side == "BUY" else -1)
                    )
                    price = (price / policy.tick).to_integral_value(
                        ROUND_CEILING if side == "BUY" else ROUND_FLOOR
                    ) * policy.tick
                    prior_volume = Decimal(str(rows[key][previous[key]]["volume"])) * (
                        100 if policy.volume_unit == "LOTS" else 1
                    )
                    if share_contract:
                        prior_volume *= share_books[key].unit_factor(
                            previous[key], session
                        )
                    capacity = (
                        prior_volume * policy.participation / policy.lot_size
                    ).to_integral_value(ROUND_FLOOR) * policy.lot_size
                    order_cap = (
                        policy.risk.max_order_notional / price / policy.lot_size
                    ).to_integral_value(ROUND_FLOOR) * policy.lot_size
                    quantity = min(requested, capacity, order_cap)
                    if side == "SELL":
                        quantity = min(quantity, held[key])
                    else:
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
                        low, high = 0, int(quantity / policy.lot_size)
                        gross_value = sum(
                            held[item] * openings[item] for item in frames
                        )
                        current_equity = (
                            portfolio.cash
                            + gross_value
                            + sum(book.receivable for book in books.values())
                        )
                        while low < high:
                            middle = (low + high + 1) // 2
                            candidate = Decimal(middle * policy.lot_size)
                            fee = calculate_fees(side, candidate, price, policy.fees)[
                                "total"
                            ]
                            after_equity = (
                                current_equity
                                - fee
                                - candidate * (price - openings[key])
                            )
                            if (
                                gross_value + candidate * openings[key]
                                <= max_gross_weight * after_equity
                            ):
                                low = middle
                            else:
                                high = middle - 1
                        quantity = Decimal(low * policy.lot_size)
                    reason, status, fee = (
                        "CAPACITY_CASH_OR_GROSS_CAP",
                        "REJECTED",
                        Decimal(0),
                    )
                    if quantity > 0:
                        estimate = calculate_fees(side, quantity, price, policy.fees)[
                            "total"
                        ]
                        intent = OrderIntent(
                            strategy_id="fixed-multi-share-actions-v2"
                            if share_contract
                            else "fixed-multi-weights-v1",
                            symbol=key,
                            timestamp=datetime.combine(
                                session, datetime.min.time(), tzinfo=UTC
                            ),
                            side=side,
                            quantity=quantity,
                        )
                        risk = evaluate_order(
                            intent,
                            portfolio,
                            {
                                item: price if item == key else openings[item]
                                for item in (
                                    {
                                        position.symbol
                                        for position in portfolio.positions
                                    }
                                    | {key}
                                )
                            },
                            estimate,
                            policy.risk,
                        )
                        if risk.approved:
                            portfolio = apply_fill(
                                portfolio,
                                execute_paper_order(
                                    intent, risk, price, intent.timestamp, estimate
                                ),
                            )
                            fee = estimate
                            sign = 1 if side == "BUY" else -1
                            flow += sign * quantity * price
                            held[key] += sign * quantity
                            reason, status = (
                                "DECLARED_PAPER_FILL",
                                "FILLED" if quantity == requested else "PARTIAL",
                            )
                        else:
                            reason = "CORE_RISK_REJECTED"
                    orders.append(
                        {
                            "session": session.isoformat(),
                            "symbol": key,
                            "side": side,
                            "requested": str(requested),
                            "filled": str(quantity) if status != "REJECTED" else "0",
                            "price": str(price),
                            "fee": str(fee),
                            "reason": reason,
                            "status": status,
                            "known_volume_from": previous[key].isoformat(),
                        }
                    )
            for key in frames:
                prior_cash = books[key].cash
                entries = books[key].observe(session, held[key])
                dividend_entries.extend(
                    {"session": session.isoformat(), "symbol": key, **entry}
                    for entry in entries
                )
                income = books[key].cash - prior_cash
                if income:
                    portfolio = PortfolioState.model_validate(
                        {**portfolio.model_dump(), "cash": portfolio.cash + income}
                    )
                if key not in missing:
                    marks[key] = Decimal(str(rows[key][session]["close"]))
                    previous[key] = session
                    mark_basis[key] = "OBSERVED_RAW_CLOSE"
                if share_contract:
                    adjusted, action_entries = share_books[key].apply_close(
                        session, held[key]
                    )
                    if adjusted != adjusted.to_integral_value():
                        raise ValueError(
                            "V2 fixed execution requires whole post-action shares"
                        )
                    for entry in action_entries:
                        portfolio = apply_position_adjustment(portfolio, entry)
                        marks[key] /= Decimal(entry["ratio"])
                        mark_basis[key] = (
                            "THEORETICAL_POST_ACTION_UNIT_MARK_NOT_OBSERVED_PRICE"
                        )
                    held[key] = adjusted
                    share_entries.extend(
                        {"session": session.isoformat(), **entry}
                        for entry in action_entries
                    )
            paid = sum(book.cash for book in books.values())
            receivable = sum(book.receivable for book in books.values())
            gross_value = sum(held[key] * marks[key] for key in frames)
            equity = portfolio.cash + gross_value + receivable
            if (
                portfolio.cash < 0
                or any(value < 0 for value in held.values())
                or portfolio.cash + flow + portfolio.cumulative_fees
                != policy.initial_cash + paid
            ):
                raise ValueError("shared Core account conservation failed")
            peak = max(peak, equity)
            drawdown = min(drawdown, equity / peak - 1)
            periods.append(
                {
                    "session": session.isoformat(),
                    "cash": str(portfolio.cash),
                    "quantities": {key: str(value) for key, value in held.items()},
                    "marks": {key: str(value) for key, value in marks.items()},
                    "equity": str(equity),
                    "fees": str(portfolio.cumulative_fees),
                    "dividend_cash": str(paid),
                    "receivable": str(receivable),
                    "gross_weight": str(gross_value / equity),
                    "cash_fraction": str(portfolio.cash / equity),
                    "gross_cap_breach": gross_value > max_gross_weight * equity,
                    "status": "PAUSED_MISSING_QUOTES_STALE_MARK"
                    if missing
                    else "OBSERVED_PAPER",
                    "missing_assets": missing,
                    **(
                        {
                            "mark_basis": {
                                key: "STALE_" + mark_basis[key]
                                if key in missing
                                else mark_basis[key]
                                for key in frames
                            },
                            "observed_raw_closes": {
                                key: str(rows[key][session]["close"])
                                if key not in missing
                                else None
                                for key in frames
                            },
                        }
                        if share_contract
                        else {}
                    ),
                }
            )
        net_return = equity / policy.initial_cash - 1
    return {
        "schema_version": "fixed-multi-share-actions-v2"
        if share_contract
        else "fixed-multi-weights-v1",
        "policy": policy.to_json(),
        "weights": {key: str(value) for key, value in weights.items()},
        "rebalance_band": str(band),
        "max_gross_weight": str(max_gross_weight),
        "periods": periods,
        "orders": orders,
        "dividend_events": dividend_entries,
        **(
            {
                "share_adjustment_events": share_entries,
                "share_books": {
                    key: book.snapshot() for key, book in share_books.items()
                },
                "outside_range_share_adjustments": [
                    event.to_json()
                    for event in adjustments
                    if event.record_session > end
                ],
                "total_return_qualified": False,
            }
            if share_contract
            else {}
        ),
        "final_equity": str(equity),
        "net_return": str(net_return),
        "max_drawdown": str(drawdown),
        "fee_total": str(portfolio.cumulative_fees),
        "paused_sessions": sum(bool(row["missing_assets"]) for row in periods),
        "gross_cap_breach_days": sum(row["gross_cap_breach"] for row in periods),
        "validated_alpha": False,
        "live_authority": "NONE",
        "basis": "RAW_PRICE_PLUS_KNOWN_CASH_AND_SHARE_EVENTS_NOT_COMPLETE_TOTAL_RETURN"
        if share_contract
        else "RAW_PRICE_PLUS_KNOWN_CASH_NOT_COMPLETE_TOTAL_RETURN",
        "limitations": [
            "same-market ETFs are correlated",
            "missing quotes pause all trading; stale valuation explicitly retained",
            "sell-first alphabetical buys may exhaust shared cash",
            "fixed targets use observed opens for quantity sizing",
            "capacity and fees are declared; daily bars cannot certify actual fills",
            "close appreciation may breach cap; not continuous protection",
        ],
    }


def replay_fixed_portfolio(
    assets: dict[str, pl.DataFrame],
    weights: dict[str, Decimal],
    policy: ExecutionPolicy,
    *,
    start_session: date,
    band: Decimal = Decimal(0),
    max_gross_weight: Decimal = Decimal(1),
    dividends: tuple[CashDividend, ...] = (),
) -> dict[str, Any]:
    return _replay_fixed_portfolio(
        assets,
        weights,
        policy,
        start_session=start_session,
        band=band,
        max_gross_weight=max_gross_weight,
        dividends=dividends,
    )


def replay_fixed_with_share_events(
    assets: dict[str, pl.DataFrame],
    weights: dict[str, Decimal],
    policy: ExecutionPolicy,
    *,
    start_session: date,
    adjustments: tuple[ShareAdjustment, ...],
    band: Decimal = Decimal(0),
    max_gross_weight: Decimal = Decimal(1),
    dividends: tuple[CashDividend, ...] = (),
    price_basis: Literal["RAW_UNADJUSTED"] = "RAW_UNADJUSTED",
) -> dict[str, Any]:
    if price_basis != "RAW_UNADJUSTED":
        raise ValueError("explicit raw prices required; no double share adjustment")
    return _replay_fixed_portfolio(
        assets,
        weights,
        policy,
        start_session=start_session,
        band=band,
        max_gross_weight=max_gross_weight,
        dividends=dividends,
        adjustments=adjustments,
        share_contract=True,
    )
