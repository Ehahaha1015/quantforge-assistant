"""Deterministic, read-only preflight for one manually executed CNY canary.

Quotes, account balances, trading sessions and terms are operator declarations.
This computes a review plan, never execution authority or a broker-certified fact.
Historical paper tickets do not enter this check.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

from quantforge.domain import OrderIntent
from quantforge.portfolio import create_portfolio
from quantforge.risk import RiskLimits, evaluate_order

from .fees import CnFeeProfile, calculate_fees

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_QUOTE_TTL_SECONDS = 60
_ACCOUNT_TTL_SECONDS = 60


def _exact_decimal(value: Any) -> Decimal:
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("money and prices require exact decimal strings")  # noqa: TRY004 -- Pydantic validation error
    number = Decimal(value)
    if not number.is_finite() or number < 0 or number > Decimal(1000000000):
        raise ValueError("nonnegative finite bounded decimal required")
    return number


Money = Annotated[Decimal, BeforeValidator(_exact_decimal)]


class _Declaration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CanaryQuote(_Declaration):
    symbol: str = Field(pattern=r"^(SH|SZ)?[0-9]{6}$")
    ask: Money = Field(gt=0)
    previous_close: Money = Field(gt=0)
    observed_at: AwareDatetime
    source: str = Field(max_length=1000)
    trading_status: Literal["CONTINUOUS", "SUSPENDED", "UNKNOWN"] = "UNKNOWN"


class CanaryAccount(_Declaration):
    available_cash: Money
    positions: dict[str, Money]
    open_orders: int = Field(strict=True, ge=0)
    observed_at: AwareDatetime
    source: str = Field(max_length=1000)


class CanaryRequest(_Declaration):
    schema_version: Literal["manual-canary-preflight-v1"] = "manual-canary-preflight-v1"
    purpose: Literal["execution_plumbing_only"] = "execution_plumbing_only"
    is_example: bool = Field(default=False, strict=True)
    quote: CanaryQuote
    account: CanaryAccount
    session: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    is_trading_day: bool = Field(strict=True)
    calendar_source: str = Field(max_length=1000)
    fee_profile: CnFeeProfile
    broker_terms_source: str = Field(max_length=1000)
    lot_size: Literal[100] = 100
    price_tick: Money = Field(
        default=Decimal("0.001"), ge=Decimal("0.001"), le=Decimal("0.01")
    )
    price_limit_pct: Money = Field(default=Decimal("0.10"), gt=0, le=Decimal("0.30"))
    market_rules_source: str = Field(max_length=1000)
    buy_limit_price: Money = Field(gt=0)
    total_budget_cny: Money = Field(default=Decimal("100.00"), gt=0, le=100)
    api_spend_cny: Money = Decimal(0)
    api_spend_source: str = Field(max_length=1000)
    max_roundtrip_fees_cny: Money = Field(default=Decimal("10.00"), le=100)


def _symbol(value: str) -> str:
    return value.removeprefix("SH").removeprefix("SZ")


def _age(observed: datetime, now: datetime) -> float:
    return (now - observed).total_seconds()


def check_canary(
    request: CanaryRequest, broker: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    """Pure check against operator inputs and a hash-verified local broker ledger.

    The caller supplies an aware clock. Rechecking is necessary immediately before
    manual entry: an already rendered report cannot lock market/account state.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware preflight clock required")
    with localcontext() as arithmetic:
        arithmetic.prec = 50
        return _check_exact(request, broker, now=now)


def _check_exact(
    request: CanaryRequest, broker: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    local_now = now.astimezone(_SHANGHAI)
    blocks: list[str] = []
    if request.is_example:
        blocks.append("EXAMPLE_INPUT_NOT_ACTIONABLE")
    if request.quote.trading_status != "CONTINUOUS":
        blocks.append("INSTRUMENT_NOT_CONTINUOUSLY_TRADING")
    sources = {
        "QUOTE_SOURCE_REQUIRED": request.quote.source,
        "ACCOUNT_SOURCE_REQUIRED": request.account.source,
        "CALENDAR_SOURCE_REQUIRED": request.calendar_source,
        "BROKER_TERMS_REQUIRED": request.broker_terms_source,
        "MARKET_RULES_REQUIRED": request.market_rules_source,
        "API_SPEND_SOURCE_REQUIRED": request.api_spend_source,
    }
    blocks.extend(reason for reason, source in sources.items() if not source.strip())
    if request.session != local_now.date().isoformat():
        blocks.append("SESSION_IS_NOT_TODAY")
    if not request.is_trading_day or local_now.weekday() > 4:
        blocks.append("MARKET_CLOSED")
    clock = local_now.time()
    if not (time(9, 30) <= clock < time(11, 30) or time(13) <= clock < time(15)):
        blocks.append("OUTSIDE_CONTINUOUS_TRADING_SESSION")
    ages = {
        "quote": _age(request.quote.observed_at, now),
        "account": _age(request.account.observed_at, now),
    }
    for label, ttl in (
        ("quote", _QUOTE_TTL_SECONDS),
        ("account", _ACCOUNT_TTL_SECONDS),
    ):
        if ages[label] < 0:
            blocks.append(label.upper() + "_FROM_FUTURE")
        elif ages[label] > ttl:
            blocks.append(label.upper() + "_STALE")
    if request.account.open_orders:
        blocks.append("OPEN_ORDERS_EXIST")
    if any(request.account.positions.values()):
        blocks.append("ACCOUNT_ALREADY_HAS_POSITIONS")
    if broker["fills"] or broker["positions"]:
        blocks.append("CANARY_ALREADY_STARTED")
    if broker.get("accounting_anomalies"):
        blocks.append("BROKER_LEDGER_ACCOUNTING_ANOMALY")
    if Decimal(broker["cash"]) != request.account.available_cash:
        blocks.append("CASH_DIFFERS_FROM_DECLARED_LEDGER")
    if Decimal(broker["opened_cash"]) > request.total_budget_cny:
        blocks.append("OPENING_CAPITAL_EXCEEDS_BUDGET")
    tick = request.price_tick
    for label, price in (
        ("ASK", request.quote.ask),
        ("PREVIOUS_CLOSE", request.quote.previous_close),
        ("BUY_LIMIT", request.buy_limit_price),
    ):
        if price % tick:
            blocks.append(label + "_OFF_TICK")
    up = (request.quote.previous_close * (1 + request.price_limit_pct) / tick).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    ) * tick
    down = (
        request.quote.previous_close * (1 - request.price_limit_pct) / tick
    ).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
    if request.quote.ask >= up:
        blocks.append("ASK_AT_OR_ABOVE_LIMIT_UP")
    if request.quote.ask <= down:
        blocks.append("ASK_AT_OR_BELOW_LIMIT_DOWN")
    if not down < request.buy_limit_price < up:
        blocks.append("BUY_LIMIT_OUTSIDE_CONSERVATIVE_PRICE_BAND")
    if request.quote.ask > request.buy_limit_price:
        blocks.append("ASK_EXCEEDS_BUY_LIMIT")

    quantity = Decimal(request.lot_size)
    buy = calculate_fees("BUY", quantity, request.buy_limit_price, request.fee_profile)
    sell = calculate_fees(
        "SELL", quantity, request.buy_limit_price, request.fee_profile
    )
    roundtrip = buy["total"] + sell["total"]
    entry_cash = buy["amount"] + buy["total"]
    trading_budget = max(Decimal(0), request.total_budget_cny - request.api_spend_cny)
    available = min(trading_budget, request.account.available_cash)
    if request.api_spend_cny >= request.total_budget_cny:
        blocks.append("API_SPEND_EXHAUSTS_TOTAL_BUDGET")
    if entry_cash + sell["total"] > available:
        blocks.append("INSUFFICIENT_BUDGET_WITH_EXIT_FEE_RESERVE")
    if roundtrip > request.max_roundtrip_fees_cny:
        blocks.append("ROUNDTRIP_FEES_EXCEED_DECLARED_CAP")

    # Greatest affordable tick, including both fee components at that price.
    lower, upper = 0, int((available / quantity / tick).to_integral_value(ROUND_FLOOR))
    while lower < upper:
        middle = (lower + upper + 1) // 2
        price = middle * tick
        total = quantity * price + sum(
            calculate_fees(side, quantity, price, request.fee_profile)["total"]
            for side in ("BUY", "SELL")
        )
        if total <= available:
            lower = middle
        else:
            upper = middle - 1
    risk = evaluate_order(
        OrderIntent(
            strategy_id="manual-canary-preflight-v1",
            symbol=_symbol(request.quote.symbol),
            timestamp=now,
            side="BUY",
            quantity=quantity,
        ),
        create_portfolio(request.account.available_cash),
        {_symbol(request.quote.symbol): request.buy_limit_price},
        buy["total"],
        RiskLimits(
            max_order_notional=trading_budget or Decimal("0.01"),
            max_position_notional=trading_budget or Decimal("0.01"),
            max_gross_exposure=trading_budget or Decimal("0.01"),
        ),
    )
    if not risk.approved:
        blocks.append("RISK_REJECTED")
    return {
        "schema_version": "manual-canary-preflight-v1",
        "status": "BLOCKED" if blocks else "READY_FOR_MANUAL_REVIEW",
        "blocks": blocks,
        "checked_at": now.isoformat(),
        "observations": "OPERATOR_DECLARED; not broker-certified",
        "ledger_last_event_hash": broker.get("ledger_last_event_hash"),
        "input": request.model_dump(mode="json"),
        "quote_age_seconds": ages["quote"],
        "account_age_seconds": ages["account"],
        "facts": {
            "budget_cny": str(request.total_budget_cny),
            "api_spend_cny": str(request.api_spend_cny),
            "entry_cash_at_limit_cny": str(entry_cash),
            "exit_fee_reserve_cny": str(sell["total"]),
            "estimated_roundtrip_fees_cny": str(roundtrip),
            "fee_fraction_of_total_budget": str(roundtrip / request.total_budget_cny),
            "max_affordable_entry_price_with_exit_reserve": str(lower * tick),
            "cash_remaining_after_entry_cny": str(
                request.account.available_cash - entry_cash
            ),
            "risk": risk.model_dump(mode="json"),
        },
        "review_plan": (
            {
                "symbol": request.quote.symbol,
                "side": "BUY",
                "quantity": request.lot_size,
                "order_type": "LIMIT",
                "limit_price": str(request.buy_limit_price),
                "session": request.session,
                "exit": "Close manually on a later confirmed trading session; import both actual fills.",
            }
            if not blocks
            else None
        ),
        "limitations": [
            "Review immediately before manual entry; observations expire after 60 seconds.",
            "Fill price/quantity and subsequent exit fees remain unobserved; no guaranteed fill or loss bound.",
            "Opening one lot verifies execution plumbing only; no strategy endorsement or validated alpha.",
            "The operator must check current broker orders before entering another order; no automatic retry.",
        ],
        "real_transport": "LOCKED",
        "no_order_sent": True,
    }
