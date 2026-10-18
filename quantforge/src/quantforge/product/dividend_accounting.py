"""Exact source-bound cash dividends: record entitlement, accrue, then settle."""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Literal, cast

import polars as pl

from quantforge.data import logical_content_hash, prepare_bars
from quantforge.experiments import canonical_json

from .source_urls import bounded_https_source

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


@dataclass(frozen=True)
class CashDividend:
    event_id: str
    symbol: str
    published_date: date
    record_date: date
    ex_date: date
    pay_date: date
    amount_per_share: Decimal
    source_url: str
    source_sha256: str
    verified: bool

    def __post_init__(self) -> None:
        if not all(
            type(getattr(self, field)) is date
            for field in (
                "published_date",
                "record_date",
                "ex_date",
                "pay_date",
            )
        ):
            raise ValueError("exact calendar dates required")
        if not _ID.fullmatch(self.event_id) or not self.symbol:
            raise ValueError("event and symbol identity required")
        if not self.published_date <= self.record_date < self.ex_date <= self.pay_date:
            raise ValueError("publication/record/ex/payment chronology invalid")
        if (
            not isinstance(self.amount_per_share, Decimal)
            or not self.amount_per_share.is_finite()
            or self.amount_per_share <= 0
            or not _SHA.fullmatch(self.source_sha256)
            or not bounded_https_source(self.source_url)
            or type(self.verified) is not bool
        ):
            raise ValueError("verified source and exact positive cash amount required")

    def to_json(self) -> dict[str, Any]:
        return {
            **asdict(self),
            **{
                field: getattr(self, field).isoformat()
                for field in (
                    "published_date",
                    "record_date",
                    "ex_date",
                    "pay_date",
                )
            },
            "amount_per_share": str(self.amount_per_share),
            "publication_precision": "DATE_ONLY",
        }


class DividendBook:
    """No trading, interest or reinvestment; cash events are independent of shares."""

    def __init__(self, events: tuple[CashDividend, ...]) -> None:
        if len({event.symbol for event in events}) > 1:
            raise ValueError("single-symbol dividend book required")
        if len({event.event_id for event in events}) != len(events):
            raise ValueError("duplicate dividend identity")
        if len({(event.symbol, event.ex_date) for event in events}) != len(events):
            raise ValueError("conflicting same-session cash dividends")
        self.events = tuple(
            sorted(events, key=lambda event: (event.record_date, event.event_id))
        )
        self.records: dict[str, dict[str, Any]] = {}
        self.cash = Decimal(0)
        self.receivable = Decimal(0)
        self.last_date: date | None = None

    def observe(self, session: date, shares: Decimal) -> list[dict[str, Any]]:
        if (
            not isinstance(shares, Decimal)
            or not shares.is_finite()
            or shares < 0
            or (self.last_date is not None and session < self.last_date)
        ):
            raise ValueError("chronological nonnegative share observation required")
        if self.last_date == session:
            if any(
                event.record_date == session
                and self.records[event.event_id]["shares"] != shares
                for event in self.events
                if event.event_id in self.records
            ):
                raise ValueError("record-date entitlement conflict")
            return []
        entries = []
        records = copy.deepcopy(self.records)
        cash, receivable = self.cash, self.receivable
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            for event in self.events:
                if session >= event.record_date and event.event_id not in records:
                    if session != event.record_date:
                        raise ValueError(
                            "record session missing; cannot invent entitlement"
                        )
                    if not event.verified:
                        raise ValueError("unverified cash dividend blocks accrual")
                    amount = shares * event.amount_per_share
                    records[event.event_id] = {
                        "shares": shares,
                        "amount": amount,
                        "accrued": False,
                        "paid": False,
                    }
                    entries.append(
                        {
                            "kind": "DIVIDEND_ENTITLEMENT",
                            "event_id": event.event_id,
                            "shares": str(shares),
                            "amount": str(amount),
                        }
                    )
                record = records.get(event.event_id)
                if record is None:
                    continue
                if session >= event.ex_date and not record["accrued"]:
                    receivable += record["amount"]
                    record["accrued"] = True
                    entries.append(
                        {
                            "kind": "DIVIDEND_ACCRUED",
                            "event_id": event.event_id,
                            "amount": str(record["amount"]),
                        }
                    )
                if session >= event.pay_date and not record["paid"]:
                    receivable -= record["amount"]
                    cash += record["amount"]
                    record["paid"] = True
                    entries.append(
                        {
                            "kind": "DIVIDEND_PAID",
                            "event_id": event.event_id,
                            "amount": str(record["amount"]),
                        }
                    )
            accrued = sum(
                (value["amount"] for value in records.values() if value["accrued"]),
                Decimal(0),
            )
            if cash + receivable != accrued or receivable < 0:
                raise ValueError("dividend conservation failed")
        self.records, self.cash, self.receivable = records, cash, receivable
        self.last_date = session
        return entries

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": "cash-dividend-book-v1",
            "cash": str(self.cash),
            "receivable": str(self.receivable),
            "records": {
                key: {
                    **value,
                    "shares": str(value["shares"]),
                    "amount": str(value["amount"]),
                }
                for key, value in self.records.items()
            },
        }


def buy_hold_cash_return(
    bars: pl.DataFrame,
    events: tuple[CashDividend, ...],
    *,
    shares: Decimal = Decimal(1000),
    price_basis: Literal["RAW_UNADJUSTED"] = "RAW_UNADJUSTED",
    coverage_verified: bool = False,
) -> dict[str, Any]:
    if price_basis != "RAW_UNADJUSTED" or type(coverage_verified) is not bool:
        raise ValueError("cash dividends require raw prices; no double adjustment")
    if not isinstance(shares, Decimal) or not shares.is_finite() or shares <= 0:
        raise ValueError("exact positive buy-and-hold shares required")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    if bars.is_empty():
        raise ValueError("nonempty historical bars required")
    maximum = epoch + timedelta(
        microseconds=cast(int, bars["timestamp"].cast(pl.Int64).max())
    )
    frame = prepare_bars(bars, as_of=maximum)
    symbols = frame["symbol"].unique().to_list()
    if len(symbols) != 1 or any(event.symbol != symbols[0] for event in events):
        raise ValueError("matching single-symbol dividend history required")
    sessions = [
        (epoch + timedelta(microseconds=int(value))).date()
        for value in frame["timestamp"].cast(pl.Int64)
    ]
    if len(set(sessions)) != len(sessions):
        raise ValueError("one price per session required")
    selected = tuple(
        event for event in events if sessions[0] <= event.record_date <= sessions[-1]
    )
    outside = [event.to_json() for event in events if event not in selected]
    book = DividendBook(selected)
    periods = []
    ledger: list[dict[str, Any]] = []
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        initial = Decimal(str(frame["open"][0])) * shares
        for index, session in enumerate(sessions):
            entries = book.observe(session, shares)
            ledger.extend(
                {"session": session.isoformat(), **entry} for entry in entries
            )
            price_equity = Decimal(str(frame["close"][index])) * shares
            periods.append(
                {
                    "session": session.isoformat(),
                    "shares": str(shares),
                    "price_equity": str(price_equity),
                    "cash": str(book.cash),
                    "receivable": str(book.receivable),
                    "total_equity": str(price_equity + book.cash + book.receivable),
                }
            )
        price_return = Decimal(periods[-1]["price_equity"]) / initial - 1
        total_return = Decimal(periods[-1]["total_equity"]) / initial - 1
    unresolved = [
        event.to_json()
        for event in selected
        if not event.verified
        or event.event_id not in book.records
        or not book.records[event.event_id]["paid"]
    ]
    return {
        "schema_version": "cash-dividend-reconstruction-v1",
        "mode": "ACCOUNTING_RECONSTRUCTION",
        "symbol": symbols[0],
        "content_hash": logical_content_hash(frame),
        "price_basis": price_basis,
        "shares": str(shares),
        "initial_equity": str(initial),
        "price_return": str(price_return),
        "known_cash_total_return": str(total_return),
        "dividend_cash": str(book.cash),
        "receivable": str(book.receivable),
        "events": [event.to_json() for event in selected],
        "unresolved_events": unresolved,
        "outside_range_events": outside,
        "book": book.snapshot(),
        "ledger": ledger,
        "periods": periods,
        "coverage_verified": coverage_verified,
        "total_return_qualified": coverage_verified and not unresolved,
        "validated_alpha": False,
        "live_eligible": False,
        "limitations": [
            "Declared buy-and-hold share quantity, no actual brokerage position.",
            "Cash distributions not reinvested; no interest, transaction fees or tax modelling.",
            "Publication precision is date-only; no same-day announcement trading claim.",
            "Raw prices plus cash/receivables; adjusted prices are refused to avoid double count.",
            "Complete event coverage is separately attested, not inferred from listed events.",
            "Known cash-event reconstruction is not certified investable fund NAV.",
        ],
        "definition": canonical_json(
            {
                "record": "shares at record-session close",
                "ex": "add receivable once",
                "pay": "receivable to cash once",
            }
        ),
    }
