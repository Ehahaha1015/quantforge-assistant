from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from quantforge.broker import (
    BrokerSubmissionEnvelope,
    create_broker_submission_envelope,
)
from quantforge.domain import OrderIntent, Position
from quantforge.live import authorize_live_order, create_live_authorization
from quantforge.portfolio import PortfolioState
from quantforge.predispatch import (
    AuthorizationFreshnessPolicy,
    SubmissionHistoryState,
)
from quantforge.provenance import (
    FreshRiskProvenance,
    evaluate_pre_dispatch_with_provenance,
)
from quantforge.risk import RiskDecision, RiskLimits

ORDER_AT = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
AUTHORIZED_AT = datetime(2030, 1, 1, 12, 1, tzinfo=UTC)
EVALUATED_AT = datetime(2030, 1, 1, 12, 2, tzinfo=UTC)
LAST_FILL_AT = datetime(2030, 1, 1, 11, 59, tzinfo=UTC)

LIVE_FINGERPRINT = "live1-bbac6388a36116c866f609f70b5a9d48"
SUBMISSION_ID = "broker1-cd2ee7ab6550b22073eb4317083ff179"
PRICE_COMMITMENT = (
    "riskprice1-da724c15da8d5e9082483e864b18bbb145fa18aceba977c0013b00a6e47091ff"
)
PORTFOLIO_COMMITMENT = (
    "riskportfolio1-ed850f4206d03c37a265301735753c6736d5beb5ed3b97b1c08ea7bbd55a423c"
)
PROVENANCE_ID = (
    "riskprov1-f3927418f0c4d7393183d6ad6a610c5ff4cecd668fe24788a36de64318e6547a"
)

PINNED_PAYLOAD = (
    '{"envelope":{"order":{"quantity":{"coefficient":"5","exponent":0,"sign":0},'
    '"side":"BUY","strategy_id":"alpha","symbol":"AAPL","timestamp":'
    '"2030-01-01T12:00:00.000000Z"},"permission":{"authorization":'
    '{"authorized_at":"2030-01-01T12:01:00.000000Z","authorized_by":"operator-7",'
    '"mode":"LIVE","order_fingerprint":"live1-bbac6388a36116c866f609f70b5a9d48"},'
    '"mode":"LIVE","order":{"quantity":{"coefficient":"5","exponent":0,"sign":0},'
    '"side":"BUY","strategy_id":"alpha","symbol":"AAPL","timestamp":'
    '"2030-01-01T12:00:00.000000Z"},"order_fingerprint":'
    '"live1-bbac6388a36116c866f609f70b5a9d48","risk_decision":{"approved":true,'
    '"detail":"","reasons":[]}},"submission_id":'
    '"broker1-cd2ee7ab6550b22073eb4317083ff179"},"estimated_fee":'
    '{"coefficient":"1","exponent":0,"sign":0},"evaluation_timestamp":'
    '"2030-01-01T12:02:00.000000Z","limits":{"max_gross_exposure":'
    '{"coefficient":"2","exponent":3,"sign":0},"max_order_notional":'
    '{"coefficient":"1","exponent":3,"sign":0},"max_position_notional":'
    '{"coefficient":"1","exponent":3,"sign":0}},"portfolio":{"cash":'
    '{"coefficient":"1","exponent":3,"sign":0},"cumulative_fees":'
    '{"coefficient":"0","exponent":0,"sign":0},"initial_cash":'
    '{"coefficient":"1","exponent":3,"sign":0},"last_fill_timestamp":'
    '"2030-01-01T11:59:00.000000Z","positions":[{"average_price":'
    '{"coefficient":"5","exponent":1,"sign":0},"quantity":{"coefficient":"2",'
    '"exponent":0,"sign":0},"symbol":"MSFT"}]},"portfolio_commitment":'
    '"riskportfolio1-ed850f4206d03c37a265301735753c6736d5beb5ed3b97b1c08ea7bbd55a423c",'
    '"pre_dispatch":{"eligible":true,"history":{"state":"NEVER_DISPATCHED",'
    '"submission_id":"broker1-cd2ee7ab6550b22073eb4317083ff179"},"policy":'
    '{"max_authorization_age_microseconds":"300000000"},"reasons":[],'
    '"risk_decision":{"approved":true,"detail":"","reasons":[]}},'
    '"price_set_commitment":'
    '"riskprice1-da724c15da8d5e9082483e864b18bbb145fa18aceba977c0013b00a6e47091ff",'
    '"risk_prices":[{"price":{"coefficient":"1","exponent":2,"sign":0},'
    '"symbol":"AAPL"},{"price":{"coefficient":"5","exponent":1,"sign":0},'
    '"symbol":"MSFT"}],"schema":"riskprov1"}'
)


def make_order(**changes: Any) -> OrderIntent:
    values: dict[str, Any] = {
        "strategy_id": "alpha",
        "symbol": "AAPL",
        "timestamp": ORDER_AT,
        "side": "BUY",
        "quantity": Decimal("5.00"),
    }
    values.update(changes)
    return OrderIntent(**values)


def make_envelope(
    order: OrderIntent | None = None,
    *,
    authorized_by: str = "operator-7",
    authorized_at: datetime = AUTHORIZED_AT,
) -> BrokerSubmissionEnvelope:
    selected = make_order() if order is None else order
    authorization = create_live_authorization(selected, authorized_by, authorized_at)
    permission = authorize_live_order(
        selected, RiskDecision(approved=True), authorization
    )
    return create_broker_submission_envelope(selected, permission)


def make_portfolio(**changes: Any) -> PortfolioState:
    values: dict[str, Any] = {
        "initial_cash": Decimal("1000.00"),
        "cash": Decimal("1000.00"),
        "positions": (
            Position(
                symbol="MSFT",
                quantity=Decimal("2.0"),
                average_price=Decimal("50.00"),
            ),
        ),
        "cumulative_fees": Decimal("0.00"),
        "last_fill_timestamp": LAST_FILL_AT,
    }
    values.update(changes)
    return PortfolioState(**values)


def make_limits(**changes: Any) -> RiskLimits:
    values: dict[str, Any] = {
        "max_order_notional": Decimal("1000.00"),
        "max_position_notional": Decimal("1000.0"),
        "max_gross_exposure": Decimal("2000.000"),
    }
    values.update(changes)
    return RiskLimits(**values)


def make_artifact(
    *,
    order: OrderIntent | None = None,
    envelope: BrokerSubmissionEnvelope | None = None,
    portfolio: PortfolioState | None = None,
    prices: dict[str, Decimal] | None = None,
    fee: Decimal = Decimal("1.00"),
    limits: RiskLimits | None = None,
    evaluation_timestamp: datetime = EVALUATED_AT,
    authorized_by: str = "operator-7",
) -> FreshRiskProvenance:
    selected_envelope = (
        make_envelope(order, authorized_by=authorized_by)
        if envelope is None
        else envelope
    )
    selected_portfolio = make_portfolio() if portfolio is None else portfolio
    selected_prices = (
        {"MSFT": Decimal("50.0"), "AAPL": Decimal("100.00")}
        if prices is None
        else prices
    )
    history = SubmissionHistoryState(
        submission_id=selected_envelope.submission_id,
        state="NEVER_DISPATCHED",
    )
    return evaluate_pre_dispatch_with_provenance(
        selected_envelope,
        history,
        AuthorizationFreshnessPolicy(max_authorization_age=timedelta(minutes=5)),
        evaluation_timestamp,
        selected_portfolio,
        selected_prices,
        fee,
        make_limits() if limits is None else limits,
    )


def equivalent_offset_artifact() -> FreshRiskProvenance:
    plus_eight = timezone(timedelta(hours=8))
    order = make_order(timestamp=datetime(2030, 1, 1, 20, 0, tzinfo=plus_eight))
    envelope = make_envelope(
        order,
        authorized_at=datetime(2030, 1, 1, 20, 1, tzinfo=plus_eight),
    )
    portfolio = make_portfolio(
        last_fill_timestamp=datetime(2030, 1, 1, 19, 59, tzinfo=plus_eight)
    )
    return make_artifact(
        envelope=envelope,
        portfolio=portfolio,
        evaluation_timestamp=datetime(2030, 1, 1, 20, 2, tzinfo=plus_eight),
    )
