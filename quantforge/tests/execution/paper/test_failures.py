from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quantforge.domain import OrderIntent
from quantforge.execution.paper import PaperExecutionError, execute_paper_order
from quantforge.risk import RiskDecision, RiskReason

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
EXECUTION_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:31:00+00:00")


def order() -> OrderIntent:
    return OrderIntent(
        strategy_id="ma",
        symbol="AAPL",
        timestamp=ORDER_TIMESTAMP,
        side="BUY",
        quantity=Decimal(5),
    )


def approved() -> RiskDecision:
    return RiskDecision(approved=True)


INVALID_PRICES: tuple[object, ...] = (
    0,
    Decimal(0),
    Decimal("0.00"),
    -1,
    Decimal("-0.01"),
    100.25,
    True,
    False,
    Decimal("NaN"),
    Decimal("Infinity"),
    Decimal("-Infinity"),
    "not-a-number",
    "",
    None,
)

INVALID_FEES: tuple[object, ...] = (
    -1,
    Decimal("-0.5"),
    0.75,
    True,
    False,
    Decimal("NaN"),
    Decimal("Infinity"),
    Decimal("-Infinity"),
    "not-a-number",
    "",
    None,
)

INVALID_AUTHORIZATIONS: tuple[object, ...] = (
    True,
    False,
    {},
    {"approved": True},
    "approved",
    None,
)


@pytest.mark.parametrize("price", INVALID_PRICES)
def test_invalid_execution_price_is_rejected(price: object) -> None:
    with pytest.raises(PaperExecutionError):
        execute_paper_order(order(), approved(), price, EXECUTION_TIMESTAMP, Decimal(0))


@pytest.mark.parametrize("fee", INVALID_FEES)
def test_invalid_execution_fee_is_rejected(fee: object) -> None:
    with pytest.raises(PaperExecutionError):
        execute_paper_order(order(), approved(), Decimal(100), EXECUTION_TIMESTAMP, fee)


@pytest.mark.parametrize("authorization", INVALID_AUTHORIZATIONS)
def test_non_decision_authorization_is_a_type_error(
    authorization: object,
) -> None:
    with pytest.raises(TypeError):
        execute_paper_order(
            order(), authorization, Decimal(100), EXECUTION_TIMESTAMP, Decimal(0)
        )


def test_rejected_decision_raises_with_canonical_reason_codes() -> None:
    decision = RiskDecision(
        approved=False,
        reasons=(RiskReason.INSUFFICIENT_CASH, RiskReason.ORDER_NOTIONAL_LIMIT),
    )
    with pytest.raises(PaperExecutionError) as excinfo:
        execute_paper_order(
            order(), decision, Decimal(100), EXECUTION_TIMESTAMP, Decimal(0)
        )
    message = str(excinfo.value)
    assert RiskReason.ORDER_NOTIONAL_LIMIT in message
    assert RiskReason.INSUFFICIENT_CASH in message
    assert message.index("ORDER_NOTIONAL_LIMIT") < message.index("INSUFFICIENT_CASH")


def test_rejected_decision_carries_every_reason() -> None:
    decision = RiskDecision(
        approved=False,
        reasons=(
            RiskReason.GROSS_EXPOSURE_LIMIT,
            RiskReason.POSITION_NOTIONAL_LIMIT,
            RiskReason.INSUFFICIENT_CASH,
            RiskReason.ORDER_NOTIONAL_LIMIT,
        ),
    )
    with pytest.raises(PaperExecutionError) as excinfo:
        execute_paper_order(
            order(), decision, Decimal(100), EXECUTION_TIMESTAMP, Decimal(0)
        )
    message = str(excinfo.value)
    positions = [message.index(reason.value) for reason in RiskReason if reason.value in message]
    assert positions == sorted(positions)
    for reason in RiskReason:
        if reason in decision.reasons:
            assert reason.value in message


def test_execution_timestamp_before_order_timestamp_is_rejected() -> None:
    with pytest.raises(PaperExecutionError) as excinfo:
        execute_paper_order(
            order(), approved(), Decimal(100),
            ORDER_TIMESTAMP - timedelta(seconds=1), Decimal(0),
        )
    assert "execution_timestamp" in str(excinfo.value)


def test_naive_execution_timestamp_is_rejected() -> None:
    # The naive value is the fixture under test, not an accident.
    naive = datetime(2026, 1, 2, 14, 31)  # noqa: DTZ001
    assert naive.tzinfo is None
    with pytest.raises(PaperExecutionError):
        execute_paper_order(order(), approved(), Decimal(100), naive, Decimal(0))


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-02T14:31:00+00:00",
        1_767_000_000,
        None,
        object(),
    ],
)
def test_non_datetime_execution_timestamp_is_rejected(timestamp: object) -> None:
    with pytest.raises(PaperExecutionError):
        execute_paper_order(order(), approved(), Decimal(100), timestamp, Decimal(0))


def test_out_of_range_execution_timestamp_is_rejected() -> None:
    # 0001-01-01T00:00+14:00 has no representable UTC projection.
    offset = timezone(timedelta(hours=14))
    extreme = datetime(1, 1, 1, tzinfo=offset)
    with pytest.raises(PaperExecutionError):
        execute_paper_order(order(), approved(), Decimal(100), extreme, Decimal(0))


def test_failures_leave_inputs_untouched_and_produce_no_fill() -> None:
    subject = order()
    decision = RiskDecision(approved=False, reasons=(RiskReason.SELL_EXCEEDS_POSITION,))
    before = subject.model_dump()
    with pytest.raises(PaperExecutionError):
        execute_paper_order(
            subject, decision, Decimal(-1), EXECUTION_TIMESTAMP, Decimal(-1)
        )
    assert subject.model_dump() == before
    assert decision.reasons == (RiskReason.SELL_EXCEEDS_POSITION,)
