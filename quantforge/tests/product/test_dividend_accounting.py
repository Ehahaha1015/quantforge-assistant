from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, localcontext

import polars as pl
import pytest

from quantforge.product.dividend_accounting import (
    CashDividend,
    DividendBook,
    buy_hold_cash_return,
)
from tests.product.test_strategy_optimization import bars


def event(*, verified: bool = True) -> CashDividend:
    return CashDividend(
        "dividend-1",
        "TEST",
        date(2020, 1, 1),
        date(2020, 1, 3),
        date(2020, 1, 4),
        date(2020, 1, 6),
        Decimal("0.1"),
        "https://issuer.example/dividend.pdf",
        "a" * 64,
        verified,
    )


def test_record_entitlement_survives_sale_and_pay_is_not_double_counted() -> None:
    book = DividendBook((event(),))
    book.observe(date(2020, 1, 3), Decimal(1000))
    book.observe(date(2020, 1, 4), Decimal(0))
    assert book.receivable == 100
    assert book.cash == 0
    book.observe(date(2020, 1, 6), Decimal(0))
    assert book.cash == 100
    assert book.receivable == 0
    assert book.observe(date(2020, 1, 6), Decimal(0)) == []
    book.observe(date(2020, 1, 7), Decimal(0))
    assert book.cash == 100


def test_conflicting_record_quantity_and_missing_record_are_refused() -> None:
    book = DividendBook((event(),))
    before = book.snapshot()
    with pytest.raises(ValueError, match="record session missing"):
        book.observe(date(2020, 1, 4), Decimal(1000))
    assert book.snapshot() == before
    book.observe(date(2020, 1, 3), Decimal(1000))
    with pytest.raises(ValueError, match="entitlement conflict"):
        book.observe(date(2020, 1, 3), Decimal(500))


def test_failure_rolls_back_entire_session() -> None:
    first = event()
    second = CashDividend(
        "second",
        "TEST",
        first.published_date,
        first.record_date,
        first.ex_date + timedelta(days=1),
        first.pay_date,
        first.amount_per_share,
        first.source_url,
        first.source_sha256,
        False,
    )
    book = DividendBook((first, second))
    before = book.snapshot()
    with pytest.raises(ValueError, match="unverified"):
        book.observe(first.record_date, Decimal(1000))
    assert book.snapshot() == before
    assert book.last_date is None


def test_ex_date_price_drop_is_offset_by_receivable_not_fake_profit() -> None:
    frame = bars(10).with_columns(
        pl.Series("open", [100.0] * 3 + [99.9] * 7),
        pl.Series("close", [100.0] * 3 + [99.9] * 7),
        pl.lit(101.0).alias("high"),
        pl.lit(99.0).alias("low"),
    )
    result = buy_hold_cash_return(frame, (event(),), coverage_verified=True)
    assert Decimal(result["price_return"]) == Decimal("-0.001")
    assert Decimal(result["known_cash_total_return"]) == 0
    assert result["periods"][3]["receivable"] == "100.0"
    assert result["periods"][5]["cash"] == "100.0"
    assert result["total_return_qualified"]
    with localcontext() as context:
        context.prec = 5
        assert buy_hold_cash_return(frame, (event(),), coverage_verified=True) == result
    with pytest.raises(ValueError, match="raw prices"):
        buy_hold_cash_return(frame, (event(),), price_basis="ADJUSTED")  # type: ignore[arg-type]


def test_incomplete_payment_and_coverage_remain_unqualified() -> None:
    result = buy_hold_cash_return(bars(5), (event(),), coverage_verified=True)
    assert Decimal(result["receivable"]) == 100
    assert len(result["unresolved_events"]) == 1
    assert not result["total_return_qualified"]
    without_coverage = buy_hold_cash_return(bars(10), (event(),))
    assert not without_coverage["total_return_qualified"]


def test_duplicate_identity_and_bad_dates_are_refused() -> None:
    with pytest.raises(ValueError):
        DividendBook((event(), event()))
    values = event().to_json()
    with pytest.raises(ValueError):
        CashDividend(
            "bad",
            "TEST",
            date(2020, 1, 10),
            event().record_date,
            event().ex_date,
            event().pay_date,
            Decimal("0.1"),
            values["source_url"],
            values["source_sha256"],
            True,
        )
    with pytest.raises(ValueError):
        DividendBook((event(),)).observe(
            event().record_date - timedelta(days=1), Decimal("NaN")
        )
