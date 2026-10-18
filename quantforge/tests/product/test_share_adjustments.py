from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import polars as pl
import pytest

from quantforge.domain import Position
from quantforge.portfolio import PortfolioState
from quantforge.product.dividend_accounting import CashDividend
from quantforge.product.execution_replay import (
    WeightRisk,
    replay_adjusted_weights,
    replay_weights,
)
from quantforge.product.fixed_portfolio import replay_fixed_with_share_events
from quantforge.product.pilot.fees import CnFeeProfile
from quantforge.product.share_adjustments import (
    ShareAdjustment,
    ShareAdjustmentBook,
    apply_position_adjustment,
    buy_hold_known_actions,
)
from tests.product.test_execution_replay import policy
from tests.product.test_strategy_optimization import bars


def event(**changes: object) -> ShareAdjustment:
    values: dict[str, object] = {
        "event_id": "split-1",
        "symbol": "TEST",
        "published_date": date(2020, 1, 1),
        "available_from": datetime(2020, 1, 2, tzinfo=UTC),
        "record_session": date(2020, 1, 4),
        "quotes_from": date(2020, 1, 5),
        "ratio": Decimal(2),
        "rounding": "CEILING_WHOLE_SHARES",
        "source_url": "https://issuer.example/split.pdf",
        "source_sha256": "a" * 64,
        "verified": True,
    }
    values.update(changes)
    return ShareAdjustment(**values)  # type: ignore[arg-type]


def frame() -> pl.DataFrame:
    prices = [10.0] * 4 + [5.0] * 6
    return bars(10).with_columns(
        *(pl.Series(name, prices) for name in ("open", "high", "low", "close")),
        pl.lit(10_000_000.0).alias("volume"),
    )


def test_source_bound_ceiling_and_idempotency_and_atomic_rejections() -> None:
    book = ShareAdjustmentBook((event(ratio=Decimal("1.14539")),))
    quantity, entries = book.apply_close(date(2020, 1, 4), Decimal(1000))
    assert quantity == 1146 and entries[0]["rounding_shares"] == "0.61000"
    assert entries[0]["cash_effect"] == entries[0]["fee_effect"] == "0"
    assert entries[0]["not_a_trade"]
    assert book.apply_close(date(2020, 1, 4), Decimal(1000)) == (quantity, [])
    assert book.apply_close(date(2020, 1, 4), quantity) == (quantity, [])
    before = book.snapshot()
    with pytest.raises(ValueError, match="quantity conflict"):
        book.apply_close(date(2020, 1, 4), Decimal(1001))
    assert book.snapshot() == before
    missing = ShareAdjustmentBook((event(),))
    with pytest.raises(ValueError, match="record session missing"):
        missing.apply_close(date(2020, 1, 5), Decimal(1000))
    assert missing.snapshot()["records"] == {} and missing.last_date is None
    unverified = ShareAdjustmentBook((event(verified=False),))
    with pytest.raises(ValueError, match="unverified"):
        unverified.apply_close(date(2020, 1, 4), Decimal(1000))
    assert not unverified.records and unverified.last_date is None


def test_share_units_conserve_value_without_fake_trade_or_cash() -> None:
    result = buy_hold_known_actions(
        frame(), shares=Decimal(1000), adjustments=(event(),)
    )
    assert Decimal(result["original_unit_price_return"]) == Decimal("-0.5")
    assert Decimal(result["known_action_return"]) == 0
    assert all(Decimal(row["equity"]) == 10000 for row in result["periods"])
    assert result["periods"][3]["shares_at_observed_close"] == "1000"
    assert result["periods"][3]["shares_after_close"] == "2000"
    assert result["periods"][3]["mark_basis"].startswith("THEORETICAL")
    assert result["periods"][4]["mark_basis"] == "OBSERVED_RAW_CLOSE"
    assert not result["total_return_qualified"]
    with localcontext() as context:
        context.prec = 5
        assert (
            buy_hold_known_actions(
                frame(), shares=Decimal(1000), adjustments=(event(),)
            )
            == result
        )
    with pytest.raises(ValueError, match="raw prices"):
        buy_hold_known_actions(
            frame(),
            shares=Decimal(1000),
            adjustments=(event(),),
            price_basis="ADJUSTED",
        )  # type: ignore[arg-type]


def test_dividend_entitlement_before_and_after_split_uses_its_record_units() -> None:
    before = CashDividend(
        "cash-pre",
        "TEST",
        date(2020, 1, 1),
        date(2020, 1, 3),
        date(2020, 1, 4),
        date(2020, 1, 6),
        Decimal("0.1"),
        "https://issuer.example/cash.pdf",
        "b" * 64,
        True,
    )
    after = replace(
        before,
        event_id="cash-post",
        record_date=date(2020, 1, 6),
        ex_date=date(2020, 1, 7),
        pay_date=date(2020, 1, 8),
    )
    result = buy_hold_known_actions(
        frame(), shares=Decimal(1000), adjustments=(event(),), dividends=(before, after)
    )
    assert result["cash_book"]["records"]["cash-pre"]["shares"] == "1000"
    assert result["cash_book"]["records"]["cash-post"]["shares"] == "2000"
    assert Decimal(result["cash_book"]["cash"]) == 300
    assert Decimal(result["periods"][-1]["equity"]) == 10300
    ambiguous = replace(before, record_date=date(2020, 1, 4), ex_date=date(2020, 1, 5))
    with pytest.raises(ValueError, match="issuer ordering"):
        buy_hold_known_actions(
            frame(),
            shares=Decimal(1000),
            adjustments=(event(),),
            dividends=(ambiguous,),
        )


def test_v3_core_fills_cash_and_odd_share_liquidation_remain_consistent() -> None:
    source = frame()
    weights = source.select("symbol", "timestamp").with_columns(
        pl.Series("weight", ["1"] * 7 + ["0"] * 3)
    )
    declared = replace(
        policy(cash=Decimal(10005)),
        fees=CnFeeProfile(
            name="TEST",
            commission_bps="0",
            min_commission="5",
            stamp_duty_bps="0",
            transfer_fee_bps="0",
        ),
    )
    result = replay_adjusted_weights(
        source, weights, declared, WeightRisk(), adjustments=(event(),)
    )
    assert result["schema_version"] == "weighted-share-actions-v3"
    assert Decimal(result["periods"][3]["quantity"]) == 2000
    assert Decimal(result["periods"][4]["quantity"]) == 2000
    assert Decimal(result["periods"][-1]["quantity"]) == 0
    assert Decimal(result["final_equity"]) == 9995
    assert len(result["share_adjustment_events"]) == 1 and result["fill_count"] == 2
    assert all(Decimal(row["cash"]) >= 0 for row in result["periods"])
    assert Decimal(result["fee_total"]) == 10
    split = event(ratio=Decimal("1.14539"))
    odd_source = source.with_columns(
        *(
            pl.Series(name, [10.0] * 4 + [10 / 1.14539] * 6)
            for name in ("open", "high", "low", "close")
        )
    )
    odd = replay_adjusted_weights(
        odd_source, weights, declared, WeightRisk(), adjustments=(split,)
    )
    assert odd["periods"][3]["quantity"] == "1146"
    assert Decimal(odd["periods"][-1]["quantity"]) == 0
    assert odd["orders"][-1]["side"] == "SELL" and odd["orders"][-1]["filled"] == "1146"
    old = replay_weights(source, weights, declared, WeightRisk())
    assert (
        old["schema_version"] == "weighted-quantity-replay-v2"
        and "share_book" not in old
    )
    assert Decimal(old["periods"][3]["quantity"]) == 1000


def test_future_quotes_do_not_revise_action_or_account_prefix() -> None:
    source = frame()
    changed = (
        source.with_row_index("i")
        .with_columns(
            *(
                pl.when(pl.col("i") >= 7)
                .then(pl.col(name) * 3)
                .otherwise(pl.col(name))
                .alias(name)
                for name in ("open", "high", "low", "close")
            )
        )
        .drop("i")
    )
    left = buy_hold_known_actions(source, shares=Decimal(1000), adjustments=(event(),))
    right = buy_hold_known_actions(
        changed, shares=Decimal(1000), adjustments=(event(),)
    )
    assert left["periods"][:7] == right["periods"][:7]
    future = event(
        event_id="future",
        published_date=date(2020, 1, 6),
        available_from=datetime(2020, 1, 7, tzinfo=UTC),
        record_session=date(2020, 1, 8),
        quotes_from=date(2020, 1, 9),
        ratio=Decimal(3),
    )
    prefix = buy_hold_known_actions(
        source.head(7), shares=Decimal(1000), adjustments=(event(), future)
    )
    assert prefix["periods"] == left["periods"][:7]


@pytest.mark.parametrize(
    "changes",
    [
        {"ratio": 1.2},
        {"ratio": Decimal("NaN")},
        {"ratio": Decimal(0)},
        {"available_from": datetime(2020, 1, 2)},  # noqa: DTZ001 -- negative timezone fixture
        {"available_from": datetime(2020, 1, 1, tzinfo=UTC)},
        {"available_from": datetime(2020, 1, 4, 23, tzinfo=UTC)},
        {"source_url": "http://issuer.example/split"},
        {"source_sha256": "bad"},
    ],
)
def test_invalid_or_not_yet_available_event_is_refused(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        event(**changes)


def test_duplicate_and_quote_unit_gap_and_fractional_execution_are_refused() -> None:
    with pytest.raises(ValueError):
        ShareAdjustmentBook((event(), event(event_id="same-day")))
    delayed = ShareAdjustmentBook((event(quotes_from=date(2020, 1, 7)),))
    delayed.apply_close(date(2020, 1, 4), Decimal(1000))
    with pytest.raises(ValueError, match="transition session"):
        delayed.apply_close(date(2020, 1, 5), Decimal(2000))
    assert delayed.unit_factor(date(2020, 1, 4), date(2020, 1, 7)) == 2
    source = frame()
    weights = source.select("symbol", "timestamp").with_columns(
        pl.lit("1").alias("weight")
    )
    fractional = event(ratio=Decimal("1.14539"), rounding="EXACT_DECIMAL")
    with pytest.raises(ValueError, match="whole post-action"):
        replay_adjusted_weights(
            source,
            weights,
            policy(cash=Decimal(10005)),
            WeightRisk(),
            adjustments=(fractional,),
        )


def test_shared_account_handles_units_and_missing_new_unit_quote() -> None:
    first = frame()
    second = first.with_columns(
        pl.lit("OTHER").alias("symbol"),
        *(pl.lit(10.0).alias(k) for k in ("open", "high", "low", "close")),
    )
    declared = replace(
        policy(cash=Decimal(20010)),
        fees=CnFeeProfile(
            name="TEST",
            commission_bps="0",
            min_commission="5",
            stamp_duty_bps="0",
            transfer_fee_bps="0",
        ),
    )
    arguments = {
        "weights": {"TEST": Decimal("0.5"), "OTHER": Decimal("0.5")},
        "policy": declared,
        "start_session": date(2020, 1, 2),
        "adjustments": (event(),),
    }
    original = replay_fixed_with_share_events(
        {"TEST": first, "OTHER": second}, **arguments
    )
    assert original["schema_version"] == "fixed-multi-share-actions-v2"
    assert all(Decimal(row["equity"]) == 20000 for row in original["periods"])
    assert Decimal(original["periods"][2]["quantities"]["TEST"]) == 2000
    missing = first.filter(pl.col("timestamp").dt.day() != 5)
    changed = replay_fixed_with_share_events(
        {"TEST": missing, "OTHER": second}, **arguments
    )
    period = next(row for row in changed["periods"] if row["session"] == "2020-01-05")
    assert period["status"] == "PAUSED_MISSING_QUOTES_STALE_MARK"
    assert period["mark_basis"]["TEST"].startswith("STALE_THEORETICAL")
    assert Decimal(period["equity"]) == 20000
    assert period["observed_raw_closes"]["TEST"] is None
    assert changed["paused_sessions"] == 1


def test_position_action_preserves_cash_fees_cost_and_refuses_forged_arithmetic() -> (
    None
):
    state = PortfolioState(
        initial_cash=Decimal(10000),
        cash=Decimal(1000),
        cumulative_fees=Decimal(5),
        positions=(
            Position(symbol="TEST", quantity=Decimal(1000), average_price=Decimal(9)),
        ),
    )
    _, entries = ShareAdjustmentBook((event(),)).apply_close(
        date(2020, 1, 4), Decimal(1000)
    )
    changed = apply_position_adjustment(state, entries[0])
    assert (
        changed.cash == state.cash and changed.cumulative_fees == state.cumulative_fees
    )
    assert changed.positions[0].quantity * changed.positions[0].average_price == 9000
    assert changed.last_fill_timestamp == state.last_fill_timestamp
    with pytest.raises(ValueError, match="arithmetic/effect conflict"):
        apply_position_adjustment(state, {**entries[0], "quantity_after": "3000"})
    with pytest.raises(ValueError, match="position/adjustment conflict"):
        apply_position_adjustment(changed, entries[0])


def test_intraday_duplicates_cannot_revalue_same_eod_action_twice() -> None:
    source = frame()
    duplicate = source.slice(3, 1).with_columns(
        pl.col("timestamp") + pl.duration(hours=1)
    )
    poisoned = pl.concat([source, duplicate]).sort("timestamp")
    with pytest.raises(ValueError, match="daily session"):
        buy_hold_known_actions(poisoned, shares=Decimal(1000), adjustments=(event(),))
    with pytest.raises(ValueError, match="daily session"):
        replay_fixed_with_share_events(
            {"TEST": poisoned},
            {"TEST": Decimal("0.5")},
            policy(),
            start_session=date(2020, 1, 2),
            adjustments=(event(),),
        )
