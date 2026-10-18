"""Deterministic ETF plumbing plan. This module grants no live authority."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from .storage import PlatformError, digest


def decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1,8})?", value
    ):
        raise PlatformError("CANARY_PLAIN_DECIMAL_REQUIRED")
    return Decimal(value)


def _fresh(value: Any, now: datetime, maximum: int) -> bool:
    try:
        moment = datetime.fromisoformat(value)
        return (
            moment.tzinfo is not None and 0 <= (now - moment).total_seconds() <= maximum
        )
    except (ValueError, TypeError):
        return False


def plan(facts: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        raise PlatformError("CANARY_AWARE_TIME_REQUIRED")
    requirements = {
        "real_broker_bound": facts.get("real_broker_bound") is True,
        "authenticated_account": facts.get("account_authenticated") is True,
        "cash_only": facts.get("cash_only") is True,
        "normal_tradable_etf": facts.get("instrument_type") == "ETF"
        and facts.get("normal_tradable") is True,
        "session_open": facts.get("session_open") is True,
        "no_prior_entry_or_unknown": facts.get("entry_sent") is False
        and facts.get("unknown_outcome") is False,
        "fresh_account": _fresh(facts.get("account_observed_at"), instant, 30),
        "fresh_quote": _fresh(facts.get("quote_observed_at"), instant, 5),
        "verified_fees": facts.get("fee_schedule_verified") is True,
        "verified_rules": facts.get("exchange_rules_verified") is True,
        "exact_account_target": facts.get("account_target_matches") is True,
        "no_existing_orders_or_positions": facts.get("open_orders") == []
        and facts.get("positions") == [],
        "official_evidence": all(
            isinstance(facts.get(k), str)
            and re.fullmatch(r"[a-f0-9]{64}", facts[k]) is not None
            for k in ("rules_hash", "fee_hash", "quote_hash", "account_hash")
        ),
    }
    arithmetic: dict[str, Any] | None = None
    try:
        lot = facts["lot_size"]
        if type(lot) is not int or not 1 <= lot <= 10000:
            raise PlatformError("VERIFIED_LOT_REQUIRED")
        tick = decimal(facts["price_tick"])
        quote = decimal(facts["unit_price"])
        limit = decimal(facts["limit_price"])
        cash = decimal(facts["available_cash"])
        minimum = decimal(facts["minimum_commission"])
        commission_rate = decimal(facts["commission_rate"])
        exchange_rate = decimal(facts["exchange_fee_rate"])
        fixed_fee = decimal(facts["other_buy_fees"])
        buffer = decimal(facts["safety_buffer"])
        if (
            not Decimal("0.0001") <= tick <= Decimal("1")
            or commission_rate > 1
            or exchange_rate > 1
        ):
            raise PlatformError("VERIFIED_TICK_AND_FEES_REQUIRED")

        def debit(price: Decimal) -> tuple[Decimal, Decimal]:
            notional = price * lot
            commission = max(minimum, notional * commission_rate).quantize(
                Decimal("0.01"), rounding=ROUND_CEILING
            )
            exchange_fee = (notional * exchange_rate).quantize(
                Decimal("0.01"), rounding=ROUND_CEILING
            )
            fees = commission + exchange_fee + fixed_fee
            return notional + fees + buffer, fees

        lower, upper = (
            0,
            int((Decimal(1) / tick).to_integral_value(rounding=ROUND_FLOOR)),
        )
        while lower < upper:
            middle = (lower + upper + 1) // 2
            if debit(tick * middle)[0] < 100:
                lower = middle
            else:
                upper = middle - 1
        maximum_price = tick * lower
        total, fees = debit(limit)
        requirements.update(
            {
                "unit_price_at_most_one": 0 < quote <= 1,
                "buy_limit_only": facts.get("side") == "BUY"
                and facts.get("order_type") == "LIMIT",
                "minimum_quantity_only": type(facts.get("quantity")) is int
                and facts["quantity"] == lot,
                "valid_tick": limit > 0 and limit % tick == 0,
                "limit_within_price_cap": limit <= min(Decimal(1), maximum_price),
                "strict_total_debit_under_100": total < 100,
                "sufficient_cash": cash >= total,
                "buffer_justified": isinstance(facts.get("buffer_rationale"), str)
                and len(facts["buffer_rationale"]) >= 10,
            }
        )
        arithmetic = {
            "lot_size": lot,
            "quantity": lot,
            "price_tick": str(tick),
            "unit_price": str(quote),
            "limit_price": str(limit),
            "maximum_limit_price": str(maximum_price),
            "worst_case_fees": str(fees),
            "safety_buffer": str(buffer),
            "worst_case_total_debit": str(total),
        }
    except (KeyError, PlatformError):
        requirements["arithmetic_facts_complete"] = False
    eligible = bool(arithmetic and all(requirements.values()))
    # Existing Core owns live authority; the user's experiment pause wins.
    return {
        "schema_version": "qf-etf-canary-plan-v1",
        "mode": "UNBOUND" if not facts.get("real_broker_bound") else "REAL",
        "status": "REAL_EXPERIMENT_PAUSED" if eligible else "INELIGIBLE_OR_UNKNOWN",
        "live_authority": "PAUSED",
        "operator_enabled": False,
        "order_sent": False,
        "budget_cap": "STRICTLY_LESS_THAN_100_CNY",
        "candidate": facts.get("symbol") if eligible else None,
        "arithmetic": arithmetic,
        "checks": [
            {"name": name, "passed": passed} for name, passed in requirements.items()
        ]
        + [{"name": "live_experiment_authorized", "passed": False}],
        "plan_hash": digest({"facts": facts, "checks": requirements}),
        "observed_at": instant.isoformat(),
    }
