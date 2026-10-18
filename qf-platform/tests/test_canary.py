from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from qf_platform.canary import plan


def facts() -> dict[str, Any]:
    stamp = datetime.now(UTC).isoformat()
    return {
        "real_broker_bound": True,
        "account_authenticated": True,
        "cash_only": True,
        "instrument_type": "ETF",
        "normal_tradable": True,
        "session_open": True,
        "entry_sent": False,
        "unknown_outcome": False,
        "account_observed_at": stamp,
        "quote_observed_at": stamp,
        "fee_schedule_verified": True,
        "exchange_rules_verified": True,
        "account_target_matches": True,
        "open_orders": [],
        "positions": [],
        "rules_hash": "a" * 64,
        "fee_hash": "b" * 64,
        "quote_hash": "c" * 64,
        "account_hash": "d" * 64,
        "lot_size": 100,
        "price_tick": "0.001",
        "unit_price": "0.940",
        "limit_price": "0.940",
        "available_cash": "100",
        "minimum_commission": "5",
        "commission_rate": "0.0003",
        "exchange_fee_rate": "0",
        "other_buy_fees": "0",
        "safety_buffer": "0",
        "buffer_rationale": "Fixture exact fee bound, not production evidence",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 100,
        "symbol": "FIXTURE_ONLY",
    }


def test_strict_inequality_rounding_and_user_pause() -> None:
    value = plan(facts())
    assert value["status"] == "REAL_EXPERIMENT_PAUSED"
    assert value["arithmetic"]["maximum_limit_price"] == "0.949"
    assert Decimal(value["arithmetic"]["worst_case_total_debit"]) == 99
    equal = plan({**facts(), "limit_price": "0.950"})
    assert equal["candidate"] is None
    assert equal["status"] == "INELIGIBLE_OR_UNKNOWN"
    assert value["order_sent"] is False
    assert value["operator_enabled"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("unit_price", "1.001"),
        ("quantity", 200),
        ("quantity", True),
        ("lot_size", True),
        ("fee_schedule_verified", False),
        ("exchange_rules_verified", False),
        ("account_target_matches", False),
        ("unknown_outcome", True),
        ("entry_sent", True),
        ("order_type", "MARKET"),
        ("cash_only", False),
        ("limit_price", "0.9405"),
        ("safety_buffer", "-1"),
        ("positions", [{}]),
        ("available_cash", "90"),
    ],
)
def test_unknown_ineligible_and_wrong_targets_deny(field: str, value: Any) -> None:
    result = plan({**facts(), field: value})
    assert result["status"] == "INELIGIBLE_OR_UNKNOWN"
    assert result["candidate"] is None


def test_stale_future_and_missing_facts_deny() -> None:
    now = datetime.now(UTC)
    for field, moment in (
        ("account_observed_at", now - timedelta(seconds=31)),
        ("quote_observed_at", now - timedelta(seconds=6)),
        ("quote_observed_at", now + timedelta(seconds=1)),
    ):
        assert (
            plan({**facts(), field: moment.isoformat()}, now=now)["candidate"] is None
        )
    assert plan({})["candidate"] is None


def test_proportional_fees_and_buffer_reduce_cap_conservatively() -> None:
    value = plan(
        {
            **facts(),
            "commission_rate": "0.01",
            "exchange_fee_rate": "0.001",
            "safety_buffer": "1.00",
        }
    )
    assert Decimal(value["arithmetic"]["maximum_limit_price"]) < Decimal("0.940")
