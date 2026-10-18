from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

import pytest

from quantforge.domain import OrderIntent
from quantforge.execution.paper import execute_paper_order
from quantforge.risk import RiskDecision

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
EXECUTION_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:31:00+00:00")
APPROVED = RiskDecision(approved=True)

BUY_ORDER_ID = "paper1-d7a22c60d95e61f1103062d3d4189a21"
SELL_ORDER_ID = "paper1-a2c9c452afe8eed2988dc9cb77908f4a"

IDENTITY_PATTERN = re.compile(r"^paper1-[0-9a-f]{32}$")


def identity(
    *,
    strategy_id: str = "ma",
    symbol: str = "AAPL",
    timestamp: datetime = ORDER_TIMESTAMP,
    side: str = "BUY",
    quantity: object = Decimal(5),
    execution_price: object = Decimal("100.25"),
    execution_fee: object = Decimal("1.50"),
    execution_timestamp: datetime = EXECUTION_TIMESTAMP,
) -> str:
    order = OrderIntent(
        strategy_id=strategy_id,
        symbol=symbol,
        timestamp=timestamp,
        side=side,
        quantity=quantity,
    )
    fill = execute_paper_order(
        order, APPROVED, execution_price, execution_timestamp, execution_fee
    )
    return fill.order_id


def test_identity_format_is_the_contracted_shape() -> None:
    assert IDENTITY_PATTERN.match(identity()) is not None
    assert identity() == BUY_ORDER_ID
    assert identity(side="SELL", quantity=Decimal(2)) == SELL_ORDER_ID


def test_same_order_produces_the_same_identity() -> None:
    assert identity() == identity() == BUY_ORDER_ID


@pytest.mark.parametrize(
    "field, value",
    [
        ("strategy_id", "other"),
        ("symbol", "MSFT"),
        ("side", "SELL"),
        ("quantity", Decimal(6)),
        ("timestamp", datetime.fromisoformat("2026-01-02T14:30:01+00:00")),
    ],
)
def test_each_identity_bearing_field_changes_the_identity(
    field: str, value: object
) -> None:
    assert identity(**{field: value}) != BUY_ORDER_ID


@pytest.mark.parametrize(
    "quantity",
    [
        "2",
        "2.0",
        "2.00",
        "20E-1",
        "0.2E1",
        2,
        Decimal("2.000"),
    ],
)
def test_equivalent_decimal_spellings_share_one_identity(quantity: object) -> None:
    assert identity(side="SELL", quantity=quantity) == SELL_ORDER_ID


def test_equivalent_utc_instants_share_one_identity() -> None:
    shifted = datetime.fromisoformat("2026-01-02T22:30:00+08:00")
    offset = shifted.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 8 * 3600
    assert identity(timestamp=shifted) == BUY_ORDER_ID


def test_execution_facts_are_excluded_from_the_identity() -> None:
    base = identity()
    assert identity(execution_price=Decimal("999.99")) == base
    assert identity(execution_fee=Decimal("12.34")) == base
    assert (
        identity(execution_timestamp=datetime.fromisoformat("2026-01-02T15:00:00+00:00"))
        == base
    )


def test_strategy_id_is_intentionally_part_of_the_identity() -> None:
    alpha = identity(strategy_id="alpha")
    beta = identity(strategy_id="beta")
    assert alpha != beta
    assert alpha != BUY_ORDER_ID
    assert beta != BUY_ORDER_ID
    assert IDENTITY_PATTERN.match(alpha) is not None
    assert identity(strategy_id="alpha") == alpha
