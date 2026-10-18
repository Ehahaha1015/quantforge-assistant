from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from quantforge.product.pilot.broker import open_broker_account
from quantforge.product.pilot.fees import CN_FEE_PROFILES
from quantforge.product.pilot.readiness import CanaryRequest, check_canary

NOW = datetime.fromisoformat("2026-10-12T10:00:00+08:00")


def _input() -> dict[str, Any]:
    return {
        "quote": {
            "symbol": "512170",
            "ask": "0.890",
            "previous_close": "0.900",
            "observed_at": NOW.isoformat(),
            "source": "operator broker quote observation",
            "trading_status": "CONTINUOUS",
        },
        "account": {
            "available_cash": "100.00",
            "positions": {},
            "open_orders": 0,
            "observed_at": NOW.isoformat(),
            "source": "operator app balance and orders",
        },
        "session": "2026-10-12",
        "is_trading_day": True,
        "calendar_source": "operator checked exchange calendar",
        "fee_profile": CN_FEE_PROFILES["etf-min-5"].to_json(),
        "broker_terms_source": "operator actual broker written schedule",
        "market_rules_source": "operator confirmed product tick and price band",
        "buy_limit_price": "0.900",
        "api_spend_source": "no model calls in this canary",
    }


def _broker() -> dict[str, Any]:
    return {"cash": "100.00", "opened_cash": "100.00", "positions": {}, "fills": 0}


def test_one_lot_preflight_includes_both_minimum_commissions_and_risk() -> None:
    result = check_canary(CanaryRequest(**_input()), _broker(), now=NOW)
    assert result["status"] == "READY_FOR_MANUAL_REVIEW"
    assert result["review_plan"]["quantity"] == 100
    assert result["review_plan"]["limit_price"] == "0.900"
    assert Decimal(result["facts"]["entry_cash_at_limit_cny"]) == Decimal(95)
    assert Decimal(result["facts"]["exit_fee_reserve_cny"]) == Decimal(5)
    assert Decimal(result["facts"]["fee_fraction_of_total_budget"]) == Decimal("0.1")
    assert Decimal(
        result["facts"]["max_affordable_entry_price_with_exit_reserve"]
    ) == Decimal("0.900")
    assert result["facts"]["risk"]["approved"] is True
    assert result["real_transport"] == "LOCKED" and result["no_order_sent"]


def test_buy_affordability_alone_cannot_spend_exit_reserve() -> None:
    value = _input()
    value["buy_limit_price"] = "0.950"
    result = check_canary(CanaryRequest(**value), _broker(), now=NOW)
    assert Decimal(result["facts"]["entry_cash_at_limit_cny"]) == 100
    assert "INSUFFICIENT_BUDGET_WITH_EXIT_FEE_RESERVE" in result["blocks"]
    assert result["review_plan"] is None


def test_model_spend_counts_towards_total_budget() -> None:
    value = _input()
    value["api_spend_cny"] = "0.02"
    result = check_canary(CanaryRequest(**value), _broker(), now=NOW)
    assert "INSUFFICIENT_BUDGET_WITH_EXIT_FEE_RESERVE" in result["blocks"]
    assert Decimal(
        result["facts"]["max_affordable_entry_price_with_exit_reserve"]
    ) == Decimal("0.899")


@pytest.mark.parametrize("label", ["quote", "account"])
@pytest.mark.parametrize("seconds,reason", [(61, "_STALE"), (-1, "_FROM_FUTURE")])
def test_stale_or_future_observations_block(
    label: str, seconds: int, reason: str
) -> None:
    value = _input()
    value[label]["observed_at"] = (NOW - timedelta(seconds=seconds)).isoformat()
    result = check_canary(CanaryRequest(**value), _broker(), now=NOW)
    assert label.upper() + reason in result["blocks"]


@pytest.mark.parametrize("clock", ["09:00:00", "11:30:00", "12:00:00", "15:00:00"])
def test_closed_hours_block(clock: str) -> None:
    now = datetime.fromisoformat("2026-10-12T" + clock + "+08:00")
    result = check_canary(CanaryRequest(**_input()), _broker(), now=now)
    assert "OUTSIDE_CONTINUOUS_TRADING_SESSION" in result["blocks"]


@pytest.mark.parametrize("day", ["2026-10-10", "2026-10-11"])
def test_weekend_cannot_be_declared_open(day: str) -> None:
    value = _input()
    value["session"] = day
    result = check_canary(
        CanaryRequest(**value),
        _broker(),
        now=datetime.fromisoformat(day + "T10:00:00+08:00"),
    )
    assert "MARKET_CLOSED" in result["blocks"]


def test_holiday_terms_missing_and_old_session_are_explicit_blocks() -> None:
    value = _input()
    value.update(is_trading_day=False, broker_terms_source="", session="2026-10-09")
    result = check_canary(CanaryRequest(**value), _broker(), now=NOW)
    assert {"MARKET_CLOSED", "BROKER_TERMS_REQUIRED", "SESSION_IS_NOT_TODAY"} <= set(
        result["blocks"]
    )


def test_existing_positions_orders_or_started_canary_cannot_get_second_buy_plan() -> (
    None
):
    value, broker = _input(), _broker()
    value["account"].update(positions={"512170": "100"}, open_orders=1)
    broker.update(fills=1, positions={"512170": "100"}, cash="5.00")
    result = check_canary(CanaryRequest(**value), broker, now=NOW)
    assert {
        "OPEN_ORDERS_EXIST",
        "ACCOUNT_ALREADY_HAS_POSITIONS",
        "CANARY_ALREADY_STARTED",
        "CASH_DIFFERS_FROM_DECLARED_LEDGER",
    } <= set(result["blocks"])


def test_examples_and_unknown_model_created_fields_cannot_be_actionable() -> None:
    value = _input()
    value["is_example"] = True
    result = check_canary(CanaryRequest(**value), _broker(), now=NOW)
    assert "EXAMPLE_INPUT_NOT_ACTIONABLE" in result["blocks"]
    value["approved"] = True
    with pytest.raises(ValidationError):
        CanaryRequest(**value)


def test_etf_price_bands_use_declared_three_decimal_tick() -> None:
    value = _input()
    value["quote"].update(previous_close="0.891", ask="0.981")
    value["buy_limit_price"] = "0.981"
    result = check_canary(CanaryRequest(**value), _broker(), now=NOW)
    assert "ASK_AT_OR_ABOVE_LIMIT_UP" in result["blocks"]
    value["buy_limit_price"] = "0.9005"
    result = check_canary(CanaryRequest(**value), _broker(), now=NOW)
    assert "BUY_LIMIT_OFF_TICK" in result["blocks"]


@pytest.mark.parametrize("trading_status", ["UNKNOWN", "SUSPENDED"])
def test_unknown_or_suspended_instrument_does_not_get_buy_plan(
    trading_status: str,
) -> None:
    value = _input()
    value["quote"]["trading_status"] = trading_status
    result = check_canary(CanaryRequest(**value), _broker(), now=NOW)
    assert "INSTRUMENT_NOT_CONTINUOUSLY_TRADING" in result["blocks"]
    assert result["review_plan"] is None


def test_precision_is_independent_of_callers_decimal_context() -> None:
    request = CanaryRequest(**_input())
    expected = check_canary(request, _broker(), now=NOW)
    with localcontext() as context:
        context.prec = 2
        assert check_canary(request, _broker(), now=NOW) == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("total_budget_cny", "100.01"),
        ("buy_limit_price", 0.9),
        ("api_spend_cny", "NaN"),
    ],
)
def test_invalid_monetary_input_fails_closed(field: str, value: Any) -> None:
    data = _input()
    data[field] = value
    with pytest.raises(ValidationError):
        CanaryRequest(**data)


def test_cli_blocks_example_and_missing_ledger_without_creating_facts(
    tmp_path: Path,
) -> None:
    data = _input()
    data["is_example"] = True
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(data), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "quantforge.product.pilot.readiness_cli",
        "--root",
        str(tmp_path),
        "--input",
        str(input_path),
    ]
    missing = subprocess.run(command, text=True, capture_output=True, check=False)
    assert missing.returncode == 2 and not missing.stderr
    assert json.loads(missing.stdout)["status"] == "BLOCKED"
    assert not (tmp_path / "broker.sqlite").exists()
    open_broker_account(tmp_path, cash="100.00", declared_source="test declaration")
    blocked = subprocess.run(command, text=True, capture_output=True, check=False)
    assert blocked.returncode == 2 and not blocked.stderr
    assert "EXAMPLE_INPUT_NOT_ACTIONABLE" in json.loads(blocked.stdout)["blocks"]
