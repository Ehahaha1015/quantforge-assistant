"""Deterministic content binding for one fresh risk composition.

The identifiers in this module establish content identity and internal
consistency only.  They do not establish an external source, timeliness,
account reconciliation, durability, or any remote action.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException
from typing import Any, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from .. import predispatch as _predispatch_api
from .. import risk as _risk_api
from ..broker import BrokerSubmissionEnvelope
from ..domain import OrderIntent, Position
from ..live import HumanLiveAuthorization, LiveExecutionPermission
from ..portfolio import PortfolioState
from ..predispatch import (
    AuthorizationFreshnessPolicy,
    PreDispatchDecision,
    PreDispatchError,
    SubmissionHistoryState,
)
from ..risk import RiskDecision, RiskError, RiskLimits, RiskReason

__all__ = [
    "FreshRiskProvenance",
    "RiskPriceFact",
    "RiskProvenanceError",
    "evaluate_pre_dispatch_with_provenance",
]

_PROVENANCE_PATTERN = r"^riskprov1-[0-9a-f]{64}$"
_PRICE_PATTERN = r"^riskprice1-[0-9a-f]{64}$"
_PORTFOLIO_PATTERN = r"^riskportfolio1-[0-9a-f]{64}$"
_CANONICAL_INSTANT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_DECIMAL_FIELDS = {"coefficient", "exponent", "sign"}
_ORDER_FIELDS = {"strategy_id", "symbol", "timestamp", "side", "quantity"}
_RISK_DECISION_FIELDS = {"approved", "detail", "reasons"}
_AUTHORIZATION_FIELDS = {
    "authorized_at",
    "authorized_by",
    "mode",
    "order_fingerprint",
}
_PERMISSION_FIELDS = {
    "authorization",
    "mode",
    "order",
    "order_fingerprint",
    "risk_decision",
}
_ENVELOPE_FIELDS = {"order", "permission", "submission_id"}
_POSITION_FIELDS = {"average_price", "quantity", "symbol"}
_PORTFOLIO_FIELDS = {
    "cash",
    "cumulative_fees",
    "initial_cash",
    "last_fill_timestamp",
    "positions",
}
_PRICE_FACT_FIELDS = {"price", "symbol"}
_LIMIT_FIELDS = {
    "max_gross_exposure",
    "max_order_notional",
    "max_position_notional",
}
_HISTORY_FIELDS = {"state", "submission_id"}
_POLICY_FIELDS = {"max_authorization_age_microseconds"}
_PRE_DISPATCH_FIELDS = {"eligible", "history", "policy", "reasons", "risk_decision"}
_PAYLOAD_FIELDS = {
    "envelope",
    "estimated_fee",
    "evaluation_timestamp",
    "limits",
    "portfolio",
    "portfolio_commitment",
    "pre_dispatch",
    "price_set_commitment",
    "risk_prices",
    "schema",
}
_OUTER_FIELDS = {"payload", "provenance_id"}


class RiskProvenanceError(ValueError):
    """Public boundary for malformed or contradictory provenance."""


class _ProvenanceValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _exact_decimal(value: Any, *, name: str, positive: bool) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int, str)):
        raise RiskProvenanceError(
            f"{name} must be an exact Decimal, int, or decimal string"
        )
    try:
        number = Decimal(value)
    except (DecimalException, ValueError) as exc:
        raise RiskProvenanceError(f"{name} must be a decimal number") from exc
    if not number.is_finite():
        raise RiskProvenanceError(f"{name} must be finite")
    if positive and number <= 0:
        raise RiskProvenanceError(f"{name} must be greater than zero")
    if not positive and number < 0:
        raise RiskProvenanceError(f"{name} must be non-negative")
    return number


def _utc(value: Any, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise RiskProvenanceError(f"{name} must be a datetime")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise RiskProvenanceError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)
    except RiskProvenanceError:
        raise
    except (OverflowError, TypeError, ValueError) as exc:
        raise RiskProvenanceError(
            f"{name} is outside the representable UTC range"
        ) from exc


class RiskPriceFact(_ProvenanceValue):
    """One exact symbol and positive price used by fresh risk."""

    symbol: str
    price: Decimal

    @field_validator("symbol")
    @classmethod
    def _non_empty_symbol(cls, value: str) -> str:
        if not value:
            raise RiskProvenanceError("risk price symbols must be non-empty strings")
        return value

    @field_validator("price", mode="before")
    @classmethod
    def _positive_price(cls, value: Any) -> Decimal:
        return _exact_decimal(value, name="risk price", positive=True)

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise RiskProvenanceError(f"risk price fact is invalid: {exc}") from exc


@dataclass(frozen=True, slots=True)
class _FreshRiskInputSnapshot:
    envelope: BrokerSubmissionEnvelope
    history: SubmissionHistoryState
    policy: AuthorizationFreshnessPolicy
    evaluation_timestamp: datetime
    portfolio: PortfolioState
    risk_prices: tuple[RiskPriceFact, ...]
    estimated_fee: Decimal
    limits: RiskLimits

    def price_mapping(self) -> dict[str, Decimal]:
        return {fact.symbol: fact.price for fact in self.risk_prices}


def _canonical_decimal(value: Decimal) -> dict[str, int | str]:
    sign, raw_digits, raw_exponent = value.as_tuple()
    if value.is_zero():
        return {"coefficient": "0", "exponent": 0, "sign": 0}
    digits = list(raw_digits)
    exponent = cast(int, raw_exponent)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return {
        "coefficient": "".join(str(digit) for digit in digits),
        "exponent": exponent,
        "sign": sign,
    }


def _canonical_datetime(value: datetime) -> str:
    moment = _utc(value, name="canonical datetime")
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T"
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}."
        f"{moment.microsecond:06d}Z"
    )


def _canonical_order(order: OrderIntent) -> dict[str, Any]:
    return {
        "quantity": _canonical_decimal(order.quantity),
        "side": order.side,
        "strategy_id": order.strategy_id,
        "symbol": order.symbol,
        "timestamp": _canonical_datetime(order.timestamp),
    }


def _canonical_risk_decision(decision: RiskDecision) -> dict[str, Any]:
    return {
        "approved": decision.approved,
        "detail": decision.detail,
        "reasons": [reason.value for reason in decision.reasons],
    }


def _canonical_authorization(
    authorization: HumanLiveAuthorization,
) -> dict[str, Any]:
    return {
        "authorized_at": _canonical_datetime(authorization.authorized_at),
        "authorized_by": authorization.authorized_by,
        "mode": authorization.mode,
        "order_fingerprint": authorization.order_fingerprint,
    }


def _canonical_permission(permission: LiveExecutionPermission) -> dict[str, Any]:
    return {
        "authorization": _canonical_authorization(permission.authorization),
        "mode": permission.mode,
        "order": _canonical_order(permission.order),
        "order_fingerprint": permission.order_fingerprint,
        "risk_decision": _canonical_risk_decision(permission.risk_decision),
    }


def _canonical_envelope(envelope: BrokerSubmissionEnvelope) -> dict[str, Any]:
    return {
        "order": _canonical_order(envelope.order),
        "permission": _canonical_permission(envelope.permission),
        "submission_id": envelope.submission_id,
    }


def _canonical_portfolio(portfolio: PortfolioState) -> dict[str, Any]:
    return {
        "cash": _canonical_decimal(portfolio.cash),
        "cumulative_fees": _canonical_decimal(portfolio.cumulative_fees),
        "initial_cash": _canonical_decimal(portfolio.initial_cash),
        "last_fill_timestamp": (
            None
            if portfolio.last_fill_timestamp is None
            else _canonical_datetime(portfolio.last_fill_timestamp)
        ),
        "positions": [
            {
                "average_price": _canonical_decimal(position.average_price),
                "quantity": _canonical_decimal(position.quantity),
                "symbol": position.symbol,
            }
            for position in portfolio.positions
        ],
    }


def _canonical_price_facts(
    risk_prices: tuple[RiskPriceFact, ...],
) -> list[dict[str, Any]]:
    return [
        {"price": _canonical_decimal(fact.price), "symbol": fact.symbol}
        for fact in risk_prices
    ]


def _canonical_limits(limits: RiskLimits) -> dict[str, Any]:
    return {
        "max_gross_exposure": _canonical_decimal(limits.max_gross_exposure),
        "max_order_notional": _canonical_decimal(limits.max_order_notional),
        "max_position_notional": _canonical_decimal(limits.max_position_notional),
    }


def _duration_microseconds(value: timedelta) -> str:
    total = (
        value.days * 86_400_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )
    return str(total)


def _canonical_pre_dispatch(decision: PreDispatchDecision) -> dict[str, Any]:
    return {
        "eligible": decision.eligible,
        "history": {
            "state": decision.history.state,
            "submission_id": decision.history.submission_id,
        },
        "policy": {
            "max_authorization_age_microseconds": _duration_microseconds(
                decision.policy.max_authorization_age
            )
        },
        "reasons": [reason.value for reason in decision.reasons],
        "risk_decision": _canonical_risk_decision(decision.risk_decision),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _commitment(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return prefix + digest


def _price_commitment(risk_prices: tuple[RiskPriceFact, ...]) -> str:
    return _commitment(
        "riskprice1-",
        {"risk_prices": _canonical_price_facts(risk_prices), "schema": "riskprice1"},
    )


def _portfolio_commitment(portfolio: PortfolioState) -> str:
    return _commitment(
        "riskportfolio1-",
        {"portfolio": _canonical_portfolio(portfolio), "schema": "riskportfolio1"},
    )


def _identity_payload(
    *,
    pre_dispatch_decision: PreDispatchDecision,
    portfolio: PortfolioState,
    risk_prices: tuple[RiskPriceFact, ...],
    estimated_fee: Decimal,
    limits: RiskLimits,
    price_set_commitment: str,
    portfolio_commitment: str,
) -> dict[str, Any]:
    return {
        "envelope": _canonical_envelope(pre_dispatch_decision.envelope),
        "estimated_fee": _canonical_decimal(estimated_fee),
        "evaluation_timestamp": _canonical_datetime(
            pre_dispatch_decision.evaluation_timestamp
        ),
        "limits": _canonical_limits(limits),
        "portfolio": _canonical_portfolio(portfolio),
        "portfolio_commitment": portfolio_commitment,
        "pre_dispatch": _canonical_pre_dispatch(pre_dispatch_decision),
        "price_set_commitment": price_set_commitment,
        "risk_prices": _canonical_price_facts(risk_prices),
        "schema": "riskprov1",
    }


def _provenance_id(payload: dict[str, Any]) -> str:
    return "riskprov1-" + hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _derived_prices(risk_prices: tuple[RiskPriceFact, ...]) -> dict[str, Decimal]:
    return {fact.symbol: fact.price for fact in risk_prices}


def _symbol_bytes(symbol: str) -> bytes:
    try:
        return symbol.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RiskProvenanceError(
            "risk price symbols must have a valid UTF-8 representation"
        ) from exc


def _validate_canonical_price_tuple(risk_prices: tuple[RiskPriceFact, ...]) -> None:
    symbols = [fact.symbol for fact in risk_prices]
    if len(set(symbols)) != len(symbols):
        raise RiskProvenanceError("risk price facts contain a duplicate symbol")
    expected = sorted(risk_prices, key=lambda fact: _symbol_bytes(fact.symbol))
    if list(risk_prices) != expected:
        raise RiskProvenanceError("risk price facts are not in canonical symbol order")


class FreshRiskProvenance(_ProvenanceValue):
    """Immutable self-validating content provenance for one composition."""

    schema: Literal["riskprov1"]  # type: ignore[assignment]
    provenance_id: str = Field(pattern=_PROVENANCE_PATTERN)
    price_set_commitment: str = Field(pattern=_PRICE_PATTERN)
    portfolio_commitment: str = Field(pattern=_PORTFOLIO_PATTERN)
    pre_dispatch_decision: PreDispatchDecision
    portfolio: PortfolioState
    risk_prices: tuple[RiskPriceFact, ...]
    estimated_fee: Decimal
    limits: RiskLimits

    @field_validator("pre_dispatch_decision", mode="before")
    @classmethod
    def _requires_decision(cls, value: Any) -> Any:
        if not isinstance(value, PreDispatchDecision):
            raise RiskProvenanceError(
                "pre_dispatch_decision must be a PreDispatchDecision"
            )
        return value

    @field_validator("portfolio", mode="before")
    @classmethod
    def _requires_portfolio(cls, value: Any) -> Any:
        if not isinstance(value, PortfolioState):
            raise RiskProvenanceError("portfolio must be a PortfolioState")
        return value

    @field_validator("limits", mode="before")
    @classmethod
    def _requires_limits(cls, value: Any) -> Any:
        if not isinstance(value, RiskLimits):
            raise RiskProvenanceError("limits must be RiskLimits")
        return value

    @field_validator("risk_prices", mode="before")
    @classmethod
    def _requires_price_tuple(cls, value: Any) -> Any:
        if not isinstance(value, tuple) or any(
            not isinstance(item, RiskPriceFact) for item in value
        ):
            raise RiskProvenanceError(
                "risk_prices must be a tuple of RiskPriceFact values"
            )
        return value

    @field_validator("estimated_fee", mode="before")
    @classmethod
    def _valid_fee(cls, value: Any) -> Decimal:
        return _exact_decimal(value, name="estimated_fee", positive=False)

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise RiskProvenanceError(
                f"fresh risk provenance is invalid: {exc}"
            ) from exc
        self._validate_artifact()

    def _validate_artifact(self) -> None:
        _validate_canonical_price_tuple(self.risk_prices)
        prices = _derived_prices(self.risk_prices)
        try:
            direct = _risk_api.evaluate_order(
                self.pre_dispatch_decision.envelope.order,
                self.portfolio,
                prices,
                self.estimated_fee,
                self.limits,
            )
        except RiskError as exc:
            raise RiskProvenanceError(
                f"stored risk inputs are invalid: {exc}"
            ) from exc
        if direct != self.pre_dispatch_decision.risk_decision:
            raise RiskProvenanceError(
                "stored fresh risk decision does not match frozen risk replay"
            )
        try:
            replayed = _predispatch_api.evaluate_pre_dispatch(
                self.pre_dispatch_decision.envelope,
                self.pre_dispatch_decision.history,
                self.pre_dispatch_decision.policy,
                self.pre_dispatch_decision.evaluation_timestamp,
                self.portfolio,
                prices,
                self.estimated_fee,
                self.limits,
            )
        except PreDispatchError as exc:
            raise RiskProvenanceError(
                f"stored pre-dispatch inputs are invalid: {exc}"
            ) from exc
        if replayed != self.pre_dispatch_decision:
            raise RiskProvenanceError(
                "stored pre-dispatch decision does not match frozen replay"
            )
        expected_price = _price_commitment(self.risk_prices)
        if self.price_set_commitment != expected_price:
            raise RiskProvenanceError("price-set commitment does not match stored facts")
        expected_portfolio = _portfolio_commitment(self.portfolio)
        if self.portfolio_commitment != expected_portfolio:
            raise RiskProvenanceError("portfolio commitment does not match stored facts")
        payload = _identity_payload(
            pre_dispatch_decision=self.pre_dispatch_decision,
            portfolio=self.portfolio,
            risk_prices=self.risk_prices,
            estimated_fee=self.estimated_fee,
            limits=self.limits,
            price_set_commitment=self.price_set_commitment,
            portfolio_commitment=self.portfolio_commitment,
        )
        if self.provenance_id != _provenance_id(payload):
            raise RiskProvenanceError("provenance identity does not match stored facts")

    def _payload(self) -> dict[str, Any]:
        return _identity_payload(
            pre_dispatch_decision=self.pre_dispatch_decision,
            portfolio=self.portfolio,
            risk_prices=self.risk_prices,
            estimated_fee=self.estimated_fee,
            limits=self.limits,
            price_set_commitment=self.price_set_commitment,
            portfolio_commitment=self.portfolio_commitment,
        )

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        if args or kwargs:
            raise TypeError("canonical provenance serialization accepts no options")
        return _canonical_json(
            {"provenance_id": self.provenance_id, "payload": self._payload()}
        )

    @classmethod
    def from_json(cls, value: str) -> Self:
        outer = _load_json_object(value, _OUTER_FIELDS, "provenance")
        payload = _require_object(outer["payload"], _PAYLOAD_FIELDS, "payload")
        if payload["schema"] != "riskprov1":
            raise RiskProvenanceError("payload schema must be riskprov1")
        envelope = _parse_envelope(payload["envelope"])
        portfolio = _parse_portfolio(payload["portfolio"])
        prices = _parse_price_facts(payload["risk_prices"])
        fee = _parse_decimal_token(payload["estimated_fee"], "estimated_fee")
        limits = _parse_limits(payload["limits"])
        moment = _parse_datetime(payload["evaluation_timestamp"], "evaluation_timestamp")
        decision = _parse_pre_dispatch(
            payload["pre_dispatch"], envelope=envelope, evaluation_timestamp=moment
        )
        price_commitment = _require_string(
            payload["price_set_commitment"], "price_set_commitment"
        )
        portfolio_commitment = _require_string(
            payload["portfolio_commitment"], "portfolio_commitment"
        )
        provenance_id = _require_string(outer["provenance_id"], "provenance_id")
        return cls(
            schema="riskprov1",
            provenance_id=provenance_id,
            price_set_commitment=price_commitment,
            portfolio_commitment=portfolio_commitment,
            pre_dispatch_decision=decision,
            portfolio=portfolio,
            risk_prices=prices,
            estimated_fee=fee,
            limits=limits,
        )


def _materialize_prices(
    risk_prices: Mapping[str, Decimal | int | str],
) -> tuple[RiskPriceFact, ...]:
    facts: list[RiskPriceFact] = []
    seen: set[str] = set()
    try:
        items = risk_prices.items()
        for symbol, price in items:
            if not isinstance(symbol, str) or not symbol:
                raise RiskProvenanceError(
                    "risk price symbols must be non-empty strings"
                )
            if symbol in seen:
                raise RiskProvenanceError(
                    f"risk price mapping yielded duplicate symbol {symbol!r}"
                )
            seen.add(symbol)
            facts.append(RiskPriceFact(symbol=symbol, price=price))
    except RiskProvenanceError:
        raise
    except Exception as exc:
        raise RiskProvenanceError("risk price mapping could not be traversed") from exc
    return tuple(sorted(facts, key=lambda fact: _symbol_bytes(fact.symbol)))


def evaluate_pre_dispatch_with_provenance(
    envelope: BrokerSubmissionEnvelope,
    history: SubmissionHistoryState,
    policy: AuthorizationFreshnessPolicy,
    evaluation_timestamp: datetime,
    portfolio: PortfolioState,
    risk_prices: Mapping[str, Decimal | int | str],
    estimated_fee: Decimal | int | str,
    limits: RiskLimits,
) -> FreshRiskProvenance:
    """Compose frozen risk and pre-dispatch evaluation from one snapshot."""
    if not isinstance(envelope, BrokerSubmissionEnvelope):
        raise TypeError("envelope must be a BrokerSubmissionEnvelope")
    if not isinstance(history, SubmissionHistoryState):
        raise TypeError("history must be a SubmissionHistoryState")
    if not isinstance(policy, AuthorizationFreshnessPolicy):
        raise TypeError("policy must be an AuthorizationFreshnessPolicy")
    if not isinstance(portfolio, PortfolioState):
        raise TypeError("portfolio must be a PortfolioState")
    if not isinstance(limits, RiskLimits):
        raise TypeError("limits must be RiskLimits")
    if not isinstance(risk_prices, Mapping):
        raise TypeError("risk_prices must be a mapping")
    snapshot = _FreshRiskInputSnapshot(
        envelope=envelope,
        history=history,
        policy=policy,
        evaluation_timestamp=_utc(
            evaluation_timestamp, name="evaluation_timestamp"
        ),
        portfolio=portfolio,
        risk_prices=_materialize_prices(risk_prices),
        estimated_fee=_exact_decimal(
            estimated_fee, name="estimated_fee", positive=False
        ),
        limits=limits,
    )
    prices = snapshot.price_mapping()
    try:
        direct = _risk_api.evaluate_order(
            snapshot.envelope.order,
            snapshot.portfolio,
            prices,
            snapshot.estimated_fee,
            snapshot.limits,
        )
    except RiskError as exc:
        raise RiskProvenanceError(f"fresh risk evaluation is invalid: {exc}") from exc
    try:
        decision = _predispatch_api.evaluate_pre_dispatch(
            snapshot.envelope,
            snapshot.history,
            snapshot.policy,
            snapshot.evaluation_timestamp,
            snapshot.portfolio,
            prices,
            snapshot.estimated_fee,
            snapshot.limits,
        )
    except PreDispatchError as exc:
        raise RiskProvenanceError(
            f"pre-dispatch evaluation is invalid: {exc}"
        ) from exc
    if direct != decision.risk_decision:
        raise RiskProvenanceError(
            "direct risk decision does not match pre-dispatch fresh risk"
        )
    price_commitment = _price_commitment(snapshot.risk_prices)
    portfolio_commitment = _portfolio_commitment(snapshot.portfolio)
    payload = _identity_payload(
        pre_dispatch_decision=decision,
        portfolio=snapshot.portfolio,
        risk_prices=snapshot.risk_prices,
        estimated_fee=snapshot.estimated_fee,
        limits=snapshot.limits,
        price_set_commitment=price_commitment,
        portfolio_commitment=portfolio_commitment,
    )
    return FreshRiskProvenance(
        schema="riskprov1",
        provenance_id=_provenance_id(payload),
        price_set_commitment=price_commitment,
        portfolio_commitment=portfolio_commitment,
        pre_dispatch_decision=decision,
        portfolio=snapshot.portfolio,
        risk_prices=snapshot.risk_prices,
        estimated_fee=snapshot.estimated_fee,
        limits=snapshot.limits,
    )


def _reject_duplicate_json_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RiskProvenanceError(f"JSON object contains duplicate field {key!r}")
        result[key] = value
    return result


def _load_json_object(value: str, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise RiskProvenanceError(f"{label} JSON must be a string")
    try:
        data = json.loads(value, object_pairs_hook=_reject_duplicate_json_fields)
    except RiskProvenanceError:
        raise
    except (TypeError, ValueError) as exc:
        raise RiskProvenanceError(f"{label} is not valid JSON") from exc
    return _require_object(data, fields, label)


def _require_object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RiskProvenanceError(f"{label} has unexpected fields")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise RiskProvenanceError(f"{label} must be a string")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise RiskProvenanceError(f"{label} must be a Boolean")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise RiskProvenanceError(f"{label} must be an array")
    return value


def _parse_decimal_token(value: Any, label: str) -> Decimal:
    token = _require_object(value, _DECIMAL_FIELDS, label)
    coefficient = token["coefficient"]
    exponent = token["exponent"]
    sign = token["sign"]
    if not isinstance(coefficient, str) or not coefficient.isascii():
        raise RiskProvenanceError(f"{label} coefficient is invalid")
    if isinstance(exponent, bool) or not isinstance(exponent, int):
        raise RiskProvenanceError(f"{label} exponent is invalid")
    if isinstance(sign, bool) or not isinstance(sign, int) or sign not in (0, 1):
        raise RiskProvenanceError(f"{label} sign is invalid")
    if coefficient == "0":
        if exponent != 0 or sign != 0:
            raise RiskProvenanceError(f"{label} zero is not canonical")
    elif (
        not coefficient
        or not coefficient.isdigit()
        or coefficient[0] == "0"
        or coefficient[-1] == "0"
    ):
        raise RiskProvenanceError(f"{label} coefficient is not canonical")
    try:
        number = Decimal((sign, tuple(int(digit) for digit in coefficient), exponent))
    except (DecimalException, OverflowError, ValueError) as exc:
        raise RiskProvenanceError(f"{label} is not a representable Decimal") from exc
    if not number.is_finite() or _canonical_decimal(number) != token:
        raise RiskProvenanceError(f"{label} is not a canonical finite Decimal")
    return number


def _parse_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or _CANONICAL_INSTANT.fullmatch(value) is None:
        raise RiskProvenanceError(f"{label} is not a canonical UTC instant")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RiskProvenanceError(f"{label} is not a valid UTC instant") from exc
    if _canonical_datetime(moment) != value:
        raise RiskProvenanceError(f"{label} is not canonical")
    return moment


def _parse_order(value: Any, label: str) -> OrderIntent:
    data = _require_object(value, _ORDER_FIELDS, label)
    strategy_id = _require_string(data["strategy_id"], f"{label}.strategy_id")
    symbol = _require_string(data["symbol"], f"{label}.symbol")
    side = _require_string(data["side"], f"{label}.side")
    if side not in ("BUY", "SELL"):
        raise RiskProvenanceError(f"{label}.side is invalid")
    try:
        return OrderIntent(
            strategy_id=strategy_id,
            symbol=symbol,
            timestamp=_parse_datetime(data["timestamp"], f"{label}.timestamp"),
            side=cast(Literal["BUY", "SELL"], side),
            quantity=_parse_decimal_token(data["quantity"], f"{label}.quantity"),
        )
    except (ValidationError, ValueError) as exc:
        raise RiskProvenanceError(f"{label} is invalid") from exc


def _parse_risk_decision(value: Any, label: str) -> RiskDecision:
    data = _require_object(value, _RISK_DECISION_FIELDS, label)
    approved = _require_bool(data["approved"], f"{label}.approved")
    detail = _require_string(data["detail"], f"{label}.detail")
    raw_reasons = _require_list(data["reasons"], f"{label}.reasons")
    if any(not isinstance(reason, str) for reason in raw_reasons):
        raise RiskProvenanceError(f"{label}.reasons contains a non-string code")
    try:
        reasons = tuple(RiskReason(reason) for reason in raw_reasons)
        decision = RiskDecision(approved=approved, detail=detail, reasons=reasons)
    except (ValueError, ValidationError) as exc:
        raise RiskProvenanceError(f"{label} is invalid") from exc
    if [reason.value for reason in decision.reasons] != raw_reasons:
        raise RiskProvenanceError(f"{label}.reasons is not canonical")
    return decision


def _parse_authorization(value: Any, label: str) -> HumanLiveAuthorization:
    data = _require_object(value, _AUTHORIZATION_FIELDS, label)
    try:
        return HumanLiveAuthorization(
            mode=_require_string(data["mode"], f"{label}.mode"),
            order_fingerprint=_require_string(
                data["order_fingerprint"], f"{label}.order_fingerprint"
            ),
            authorized_by=_require_string(
                data["authorized_by"], f"{label}.authorized_by"
            ),
            authorized_at=_parse_datetime(
                data["authorized_at"], f"{label}.authorized_at"
            ),
        )
    except (ValueError, ValidationError) as exc:
        raise RiskProvenanceError(f"{label} is invalid") from exc


def _parse_permission(value: Any, label: str) -> LiveExecutionPermission:
    data = _require_object(value, _PERMISSION_FIELDS, label)
    try:
        return LiveExecutionPermission(
            authorization=_parse_authorization(
                data["authorization"], f"{label}.authorization"
            ),
            mode=_require_string(data["mode"], f"{label}.mode"),
            order=_parse_order(data["order"], f"{label}.order"),
            order_fingerprint=_require_string(
                data["order_fingerprint"], f"{label}.order_fingerprint"
            ),
            risk_decision=_parse_risk_decision(
                data["risk_decision"], f"{label}.risk_decision"
            ),
        )
    except (ValueError, ValidationError) as exc:
        raise RiskProvenanceError(f"{label} is invalid") from exc


def _parse_envelope(value: Any) -> BrokerSubmissionEnvelope:
    data = _require_object(value, _ENVELOPE_FIELDS, "envelope")
    order = _parse_order(data["order"], "envelope.order")
    try:
        return BrokerSubmissionEnvelope(
            order=order,
            permission=_parse_permission(data["permission"], "envelope.permission"),
            submission_id=_require_string(
                data["submission_id"], "envelope.submission_id"
            ),
        )
    except (ValueError, ValidationError) as exc:
        raise RiskProvenanceError("envelope is invalid") from exc


def _parse_portfolio(value: Any) -> PortfolioState:
    data = _require_object(value, _PORTFOLIO_FIELDS, "portfolio")
    raw_positions = _require_list(data["positions"], "portfolio.positions")
    positions: list[Position] = []
    for index, raw in enumerate(raw_positions):
        label = f"portfolio.positions[{index}]"
        item = _require_object(raw, _POSITION_FIELDS, label)
        try:
            positions.append(
                Position(
                    symbol=_require_string(item["symbol"], f"{label}.symbol"),
                    quantity=_parse_decimal_token(
                        item["quantity"], f"{label}.quantity"
                    ),
                    average_price=_parse_decimal_token(
                        item["average_price"], f"{label}.average_price"
                    ),
                )
            )
        except (ValueError, ValidationError) as exc:
            raise RiskProvenanceError(f"{label} is invalid") from exc
    raw_last_fill = data["last_fill_timestamp"]
    last_fill = (
        None
        if raw_last_fill is None
        else _parse_datetime(raw_last_fill, "portfolio.last_fill_timestamp")
    )
    try:
        portfolio = PortfolioState(
            initial_cash=_parse_decimal_token(
                data["initial_cash"], "portfolio.initial_cash"
            ),
            cash=_parse_decimal_token(data["cash"], "portfolio.cash"),
            positions=tuple(positions),
            cumulative_fees=_parse_decimal_token(
                data["cumulative_fees"], "portfolio.cumulative_fees"
            ),
            last_fill_timestamp=last_fill,
        )
    except (ValueError, ValidationError) as exc:
        raise RiskProvenanceError("portfolio is invalid") from exc
    if [position.symbol for position in portfolio.positions] != [
        position.symbol for position in positions
    ]:
        raise RiskProvenanceError("portfolio positions are not in canonical order")
    return portfolio


def _parse_price_facts(value: Any) -> tuple[RiskPriceFact, ...]:
    raw_facts = _require_list(value, "risk_prices")
    facts: list[RiskPriceFact] = []
    for index, raw in enumerate(raw_facts):
        label = f"risk_prices[{index}]"
        data = _require_object(raw, _PRICE_FACT_FIELDS, label)
        facts.append(
            RiskPriceFact(
                symbol=_require_string(data["symbol"], f"{label}.symbol"),
                price=_parse_decimal_token(data["price"], f"{label}.price"),
            )
        )
    result = tuple(facts)
    _validate_canonical_price_tuple(result)
    return result


def _parse_limits(value: Any) -> RiskLimits:
    data = _require_object(value, _LIMIT_FIELDS, "limits")
    try:
        return RiskLimits(
            max_order_notional=_parse_decimal_token(
                data["max_order_notional"], "limits.max_order_notional"
            ),
            max_position_notional=_parse_decimal_token(
                data["max_position_notional"], "limits.max_position_notional"
            ),
            max_gross_exposure=_parse_decimal_token(
                data["max_gross_exposure"], "limits.max_gross_exposure"
            ),
        )
    except (ValueError, ValidationError) as exc:
        raise RiskProvenanceError("limits are invalid") from exc


def _parse_history(value: Any) -> SubmissionHistoryState:
    data = _require_object(value, _HISTORY_FIELDS, "pre_dispatch.history")
    try:
        return SubmissionHistoryState(
            submission_id=_require_string(
                data["submission_id"], "pre_dispatch.history.submission_id"
            ),
            state=_require_string(data["state"], "pre_dispatch.history.state"),
        )
    except (ValueError, ValidationError) as exc:
        raise RiskProvenanceError("pre_dispatch.history is invalid") from exc


def _parse_policy(value: Any) -> AuthorizationFreshnessPolicy:
    data = _require_object(value, _POLICY_FIELDS, "pre_dispatch.policy")
    raw = data["max_authorization_age_microseconds"]
    if (
        not isinstance(raw, str)
        or not raw
        or not raw.isascii()
        or not raw.isdigit()
        or (len(raw) > 1 and raw[0] == "0")
    ):
        raise RiskProvenanceError(
            "pre_dispatch.policy duration is not canonical"
        )
    try:
        duration = timedelta(microseconds=int(raw))
        policy = AuthorizationFreshnessPolicy(max_authorization_age=duration)
    except (OverflowError, ValueError, ValidationError) as exc:
        raise RiskProvenanceError("pre_dispatch.policy is invalid") from exc
    if _duration_microseconds(policy.max_authorization_age) != raw:
        raise RiskProvenanceError("pre_dispatch.policy duration is not canonical")
    return policy


def _parse_pre_dispatch(
    value: Any,
    *,
    envelope: BrokerSubmissionEnvelope,
    evaluation_timestamp: datetime,
) -> PreDispatchDecision:
    data = _require_object(value, _PRE_DISPATCH_FIELDS, "pre_dispatch")
    raw_reasons = _require_list(data["reasons"], "pre_dispatch.reasons")
    if any(not isinstance(reason, str) for reason in raw_reasons):
        raise RiskProvenanceError("pre_dispatch.reasons contains a non-string code")
    try:
        decision = PreDispatchDecision(
            envelope=envelope,
            history=_parse_history(data["history"]),
            policy=_parse_policy(data["policy"]),
            evaluation_timestamp=evaluation_timestamp,
            risk_decision=_parse_risk_decision(
                data["risk_decision"], "pre_dispatch.risk_decision"
            ),
            eligible=_require_bool(data["eligible"], "pre_dispatch.eligible"),
            reasons=tuple(raw_reasons),
        )
    except (ValueError, ValidationError) as exc:
        raise RiskProvenanceError("pre_dispatch is invalid") from exc
    if [reason.value for reason in decision.reasons] != raw_reasons:
        raise RiskProvenanceError("pre_dispatch.reasons is not canonical")
    return decision
