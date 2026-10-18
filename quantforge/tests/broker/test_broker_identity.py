from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from decimal import Decimal

import pytest

from quantforge.broker import (
    BrokerBoundaryError,
    BrokerSubmissionEnvelope,
    BrokerSubmissionOutcome,
    broker_submission_id,
)
from quantforge.domain import OrderIntent
from quantforge.live import (
    LiveExecutionPermission,
    authorize_live_order,
    create_live_authorization,
)
from quantforge.portfolio import create_portfolio
from quantforge.risk import RiskLimits, evaluate_order

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
AUTH_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:31:00+00:00")
BUY_SUBMISSION_ID = "broker1-4c78154fcce6c1791cb74257a777de95"
SELL_SUBMISSION_ID = "broker1-0e84b342e472496e8b9daa69a84ec87b"
BUY_LIVE_FINGERPRINT = "live1-d7a22c60d95e61f1103062d3d4189a21"
PAPER_BUY_IDENTIFIER = "paper1-d7a22c60d95e61f1103062d3d4189a21"

IDENTITY_PATTERN = re.compile(r"^broker1-[0-9a-f]{32}$")

LIMITS = RiskLimits(
    max_order_notional=Decimal(1000),
    max_position_notional=Decimal(1000),
    max_gross_exposure=Decimal(1000),
)


def buy_order(**overrides: object) -> OrderIntent:
    fields: dict[str, object] = {
        "strategy_id": "ma",
        "symbol": "AAPL",
        "timestamp": ORDER_TIMESTAMP,
        "side": "BUY",
        "quantity": Decimal(5),
    }
    fields.update(overrides)
    return OrderIntent.model_validate(fields)


def buy_permission() -> LiveExecutionPermission:
    order = buy_order()
    decision = evaluate_order(
        order,
        create_portfolio(1000),
        {"AAPL": Decimal(100)},
        Decimal(1),
        LIMITS,
    )
    assert decision.approved is True
    return authorize_live_order(
        order, decision, create_live_authorization(order, "operator@example.com", AUTH_TIMESTAMP)
    )


def test_identity_shape_matches_the_broker_namespace() -> None:
    identity = broker_submission_id(buy_order())
    assert IDENTITY_PATTERN.match(identity) is not None
    assert identity == BUY_SUBMISSION_ID


def test_identity_is_stable_across_repeated_calls() -> None:
    assert broker_submission_id(buy_order()) == broker_submission_id(buy_order())
    assert broker_submission_id(buy_order()) == BUY_SUBMISSION_ID


def test_identity_is_reproducible_in_a_fresh_interpreter() -> None:
    code = (
        "from datetime import datetime\n"
        "from decimal import Decimal\n"
        "from quantforge.domain import OrderIntent\n"
        "from quantforge.broker import broker_submission_id\n"
        "order = OrderIntent(strategy_id='ma', symbol='AAPL',\n"
        "    timestamp=datetime.fromisoformat('2026-01-02T14:30:00+00:00'),\n"
        "    side='BUY', quantity=Decimal('5'))\n"
        "print(broker_submission_id(order))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == BUY_SUBMISSION_ID


@pytest.mark.parametrize("quantity", [5, "5", "5.0", "50E-1", Decimal("5.000")])
def test_equivalent_quantity_spellings_share_one_identity(quantity: object) -> None:
    assert broker_submission_id(buy_order(quantity=quantity)) == BUY_SUBMISSION_ID


def test_equivalent_utc_instants_share_one_identity() -> None:
    shifted = datetime.fromisoformat("2026-01-02T22:30:00+08:00")
    assert shifted.utcoffset() is not None
    assert broker_submission_id(buy_order(timestamp=shifted)) == BUY_SUBMISSION_ID


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
def test_each_identity_field_changes_the_identity(field: str, value: object) -> None:
    assert broker_submission_id(buy_order(**{field: value})) != BUY_SUBMISSION_ID


def test_namespaces_are_not_substitutable() -> None:
    identity = broker_submission_id(buy_order())
    assert identity != BUY_LIVE_FINGERPRINT
    assert identity != PAPER_BUY_IDENTIFIER
    assert identity.removeprefix("broker1-") not in {
        BUY_LIVE_FINGERPRINT.removeprefix("live1-"),
        PAPER_BUY_IDENTIFIER.removeprefix("paper1-"),
    }

    for foreign in (BUY_LIVE_FINGERPRINT, PAPER_BUY_IDENTIFIER):
        with pytest.raises(BrokerBoundaryError):
            BrokerSubmissionOutcome(submission_id=foreign, status="ACCEPTED")
        with pytest.raises(BrokerBoundaryError):
            BrokerSubmissionEnvelope(
                order=buy_order(),
                permission=buy_permission(),
                submission_id=foreign,
            )


@pytest.mark.parametrize(
    "identity",
    [
        BUY_SUBMISSION_ID.upper(),
        BUY_SUBMISSION_ID.removeprefix("broker1-"),
        BUY_SUBMISSION_ID + " ",
        " " + BUY_SUBMISSION_ID,
        "broker1-" + "a" * 31,
        "broker1-" + "a" * 33,
        "broker1-" + "z" * 32,
        "broker2-" + "a" * 32,
        "",
    ],
)
def test_non_canonical_identities_are_rejected(identity: str) -> None:
    with pytest.raises(BrokerBoundaryError):
        BrokerSubmissionOutcome(submission_id=identity, status="ACCEPTED")
