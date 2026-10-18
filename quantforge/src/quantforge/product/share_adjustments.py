"""Source-bound end-of-close share events; cash entitlement and quote units stay explicit."""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import ROUND_CEILING, Context, Decimal, localcontext
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import polars as pl

from quantforge.data import logical_content_hash
from quantforge.domain import Position
from quantforge.portfolio import PortfolioState

from .dividend_accounting import CashDividend, DividendBook
from .source_urls import bounded_https_source
from .trend_refinement import _frame


def _quantity(value: Decimal) -> None:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not 0 <= value <= Decimal("1e18")
        or cast(int, value.as_tuple().exponent) < -8
    ):
        raise ValueError(
            "bounded exact nonnegative quantity with at most eight decimal places required"
        )


@dataclass(frozen=True)
class ShareAdjustment:
    event_id: str
    symbol: str
    published_date: date
    available_from: datetime
    record_session: date
    quotes_from: date
    ratio: Decimal
    rounding: Literal["EXACT_DECIMAL", "CEILING_WHOLE_SHARES"]
    source_url: str
    source_sha256: str
    verified: bool

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.event_id)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", self.symbol)
            or any(
                type(value) is not date
                for value in (
                    self.published_date,
                    self.record_session,
                    self.quotes_from,
                )
            )
        ):
            raise ValueError("canonical event, symbol and exact session dates required")
        if (
            not isinstance(self.available_from, datetime)
            or self.available_from.tzinfo is None
            or self.available_from.utcoffset() is None
        ):
            raise ValueError("aware event availability required")
        available = self.available_from.astimezone(ZoneInfo("Asia/Shanghai"))
        if (
            not self.published_date
            < available.date()
            <= self.record_session
            < self.quotes_from
            or available
            > datetime.combine(self.record_session, time(15), ZoneInfo("Asia/Shanghai"))
        ):
            raise ValueError(
                "date-only publication needs conservative next-day availability before EOD adjustment"
            )
        if (
            not isinstance(self.ratio, Decimal)
            or not self.ratio.is_finite()
            or not Decimal("0.000001") <= self.ratio <= Decimal(1000000)
            or cast(int, self.ratio.as_tuple().exponent) < -12
            or self.rounding not in {"EXACT_DECIMAL", "CEILING_WHOLE_SHARES"}
            or not bounded_https_source(self.source_url)
            or not re.fullmatch(r"[a-f0-9]{64}", self.source_sha256)
            or type(self.verified) is not bool
        ):
            raise ValueError(
                "exact bounded ratio, declared rounding and source identity required"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "published_date": self.published_date.isoformat(),
            "available_from": self.available_from.isoformat(),
            "record_session": self.record_session.isoformat(),
            "quotes_from": self.quotes_from.isoformat(),
            "ratio": str(self.ratio),
            "effective_phase": "AFTER_OBSERVED_SESSION_CLOSE",
            "publication_precision": "DATE_ONLY_CONSERVATIVE_NEXT_DAY",
        }


class ShareAdjustmentBook:
    def __init__(self, events: tuple[ShareAdjustment, ...]) -> None:
        if (
            len({e.symbol for e in events}) > 1
            or len({e.event_id for e in events}) != len(events)
            or len({e.record_session for e in events}) != len(events)
        ):
            raise ValueError("single symbol and nonconflicting share events required")
        self.events = tuple(sorted(events, key=lambda e: e.record_session))
        if any(
            before.quotes_from > after.record_session
            for before, after in zip(self.events, self.events[1:])
        ):
            raise ValueError("overlapping share/quote unit transitions refused")
        self.records: dict[str, dict[str, Any]] = {}
        self.last_date: date | None = None
        self.last_input = self.last_output = Decimal(0)

    def apply_close(
        self, session: date, shares: Decimal
    ) -> tuple[Decimal, list[dict[str, Any]]]:
        _quantity(shares)
        if type(session) is not date or (
            self.last_date is not None and session < self.last_date
        ):
            raise ValueError("chronological exact close session required")
        if session == self.last_date:
            if shares not in {self.last_input, self.last_output}:
                raise ValueError("same-close adjustment quantity conflict")
            return self.last_output, []
        records = copy.deepcopy(self.records)
        result, entries = shares, []
        with localcontext(Context(prec=50)):
            for event in self.events:
                if event.record_session < session < event.quotes_from:
                    raise ValueError("quote unit transition session is not declared")
                if session >= event.record_session and event.event_id not in records:
                    if session != event.record_session:
                        raise ValueError(
                            "share record session missing; cannot invent entitlement"
                        )
                    if not event.verified:
                        raise ValueError("unverified share event blocks adjustment")
                    exact = result * event.ratio
                    adjusted = (
                        exact.to_integral_value(rounding=ROUND_CEILING)
                        if event.rounding == "CEILING_WHOLE_SHARES"
                        else exact
                    )
                    _quantity(adjusted)
                    entry = {
                        "kind": "SHARE_ADJUSTMENT_AFTER_CLOSE",
                        "event_id": event.event_id,
                        "symbol": event.symbol,
                        "record_session": session.isoformat(),
                        "quantity_before": str(result),
                        "ratio": str(event.ratio),
                        "exact_quantity_after": str(exact),
                        "quantity_after": str(adjusted),
                        "rounding_shares": str(adjusted - exact),
                        "rounding": event.rounding,
                        "quotes_from": event.quotes_from.isoformat(),
                        "source_sha256": event.source_sha256,
                        "cash_effect": "0",
                        "fee_effect": "0",
                        "not_a_trade": True,
                    }
                    records[event.event_id] = entry
                    entries.append(copy.deepcopy(entry))
                    result = adjusted
        self.records, self.last_date = records, session
        self.last_input, self.last_output = shares, result
        return result, entries

    def unit_factor(self, before: date, after: date) -> Decimal:
        if before > after:
            raise ValueError("chronological quote units required")
        with localcontext(Context(prec=50)):
            factor = Decimal(1)
            for event in self.events:
                if before < event.quotes_from <= after:
                    if not event.verified or event.event_id not in self.records:
                        raise ValueError(
                            "quote-unit transition requires applied verified share event"
                        )
                    factor *= event.ratio
            return factor

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": "share-adjustment-book-v1",
            "records": copy.deepcopy(self.records),
        }


def apply_position_adjustment(
    state: PortfolioState, entry: dict[str, Any]
) -> PortfolioState:
    """Exact cash/fees stay untouched; cost-average division uses 50-digit Decimal."""
    symbol = entry["symbol"]
    before, after = Decimal(entry["quantity_before"]), Decimal(entry["quantity_after"])
    _quantity(before)
    _quantity(after)
    ratio = Decimal(entry["ratio"])
    if (
        not ratio.is_finite()
        or ratio <= 0
        or entry["cash_effect"] != "0"
        or entry["fee_effect"] != "0"
        or entry["kind"] != "SHARE_ADJUSTMENT_AFTER_CLOSE"
    ):
        raise ValueError("share adjustment arithmetic/effect conflict")
    with localcontext(Context(prec=50)):
        exact = before * ratio
        expected = (
            exact.to_integral_value(rounding=ROUND_CEILING)
            if entry["rounding"] == "CEILING_WHOLE_SHARES"
            else exact
        )
        if (
            entry["rounding"] not in {"CEILING_WHOLE_SHARES", "EXACT_DECIMAL"}
            or after != expected
            or Decimal(entry["exact_quantity_after"]) != exact
            or Decimal(entry["rounding_shares"]) != after - exact
        ):
            raise ValueError("share adjustment arithmetic/effect conflict")
    positions = []
    found = False
    with localcontext(Context(prec=50)):
        for position in state.positions:
            if position.symbol == symbol:
                found = True
                if position.quantity != before or after <= 0:
                    raise ValueError("authoritative position/adjustment conflict")
                positions.append(
                    Position(
                        symbol=symbol,
                        quantity=after,
                        average_price=position.quantity
                        * position.average_price
                        / after,
                    )
                )
            else:
                positions.append(position)
    if not found and (before != 0 or after != 0):
        raise ValueError("cannot adjust nonexistent holdings")
    return PortfolioState.model_validate(
        {**state.model_dump(), "positions": tuple(positions)}
    )


def buy_hold_known_actions(
    bars: pl.DataFrame,
    *,
    shares: Decimal,
    adjustments: tuple[ShareAdjustment, ...],
    dividends: tuple[CashDividend, ...] = (),
    price_basis: Literal["RAW_UNADJUSTED"] = "RAW_UNADJUSTED",
) -> dict[str, Any]:
    _quantity(shares)
    if shares <= 0 or price_basis != "RAW_UNADJUSTED":
        raise ValueError(
            "positive declared shares and raw prices required; no double adjustment"
        )
    frame = _frame(bars)
    symbol = str(frame["symbol"][0])
    sessions = [t.date() for t in frame["timestamp"]]
    if len(sessions) != len(set(sessions)):
        raise ValueError("one observed quote per daily session required")
    if any(e.symbol != symbol for e in adjustments) or any(
        e.symbol != symbol for e in dividends
    ):
        raise ValueError("matching cash and share event symbol required")
    selected = tuple(
        e for e in adjustments if sessions[0] <= e.record_session <= sessions[-1]
    )
    cash_events = tuple(
        e for e in dividends if sessions[0] <= e.record_date <= sessions[-1]
    )
    if {e.record_session for e in selected} & {e.record_date for e in cash_events}:
        raise ValueError(
            "same-close cash/share registration requires separate explicit issuer ordering"
        )
    cash_book, share_book = DividendBook(cash_events), ShareAdjustmentBook(selected)
    quantity = shares
    ledger: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    with localcontext(Context(prec=50)):
        initial = Decimal(str(frame["open"][0])) * shares
        for session, raw_price in zip(sessions, frame["close"], strict=True):
            cash_entries = cash_book.observe(session, quantity)
            before = quantity
            quantity, share_entries = share_book.apply_close(session, quantity)
            price = Decimal(str(raw_price))
            factor = Decimal(1)
            for entry in share_entries:
                factor *= Decimal(entry["ratio"])
            mark = price / factor
            ledger.extend(
                {"session": session.isoformat(), **entry}
                for entry in (*cash_entries, *share_entries)
            )
            value = quantity * mark
            periods.append(
                {
                    "session": session.isoformat(),
                    "raw_close": str(price),
                    "shares_at_observed_close": str(before),
                    "shares_after_close": str(quantity),
                    "mark": str(mark),
                    "mark_basis": "THEORETICAL_POST_ACTION_UNIT_MARK_NOT_OBSERVED_PRICE"
                    if share_entries
                    else "OBSERVED_RAW_CLOSE",
                    "price_equity": str(value),
                    "cash": str(cash_book.cash),
                    "receivable": str(cash_book.receivable),
                    "equity": str(value + cash_book.cash + cash_book.receivable),
                }
            )
        original_price_return = Decimal(str(frame["close"][-1])) * shares / initial - 1
        known_return = Decimal(periods[-1]["equity"]) / initial - 1
    return {
        "schema_version": "known-cash-share-reconstruction-v1",
        "mode": "ACCOUNTING_RECONSTRUCTION",
        "symbol": symbol,
        "content_hash": logical_content_hash(frame),
        "initial_equity": str(initial),
        "original_unit_price_return": str(original_price_return),
        "known_action_return": str(known_return),
        "periods": periods,
        "ledger": ledger,
        "cash_book": cash_book.snapshot(),
        "share_book": share_book.snapshot(),
        "adjustments": [e.to_json() for e in selected],
        "dividends": [e.to_json() for e in cash_events],
        "outside_range_adjustments": [
            e.to_json() for e in adjustments if e not in selected
        ],
        "price_basis": price_basis,
        "total_return_qualified": False,
        "validated_alpha": False,
        "live_eligible": False,
        "limitations": [
            "Known events only; complete action coverage not certified",
            "No fees, tax or reinvestment",
            "EOD unit marks are theoretical; next observed quote replaces them",
            "50-digit deterministic Decimal division",
        ],
    }
