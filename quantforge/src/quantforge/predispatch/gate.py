"""Deterministic pre-dispatch safety gate.

This module evaluates one explicit set of facts at one explicit instant and
returns an immutable decision. It performs no transport, reads no clock, holds
no credentials, and persists nothing.

Three limits are installed and must not be overstated:

- the gate evaluates explicit facts; it cannot prove that supplied prices or a
  supplied portfolio state are globally current, and it cannot make its
  evaluation atomic with a remote broker;
- a declaration of prior dispatch history is an assertion made by an external
  process, and durable ownership of that history is a future prerequisite;
- the gate can authorize only the first dispatch of one submission identity;
  re-dispatch after any prior attempt is denied and deferred.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from ..broker import BrokerSubmissionEnvelope
from ..portfolio import PortfolioState
from ..risk import RiskDecision, RiskError, RiskLimits, evaluate_order

__all__ = [
    "AuthorizationFreshnessPolicy",
    "PreDispatchDecision",
    "PreDispatchError",
    "PreDispatchReason",
    "SubmissionHistoryState",
    "evaluate_pre_dispatch",
]

_IDENTITY_PATTERN = r"^broker1-[0-9a-f]{32}$"
_POLICY_FIELDS = {"max_authorization_age"}
_HISTORY_FIELDS = {"submission_id", "state"}
_DECISION_FIELDS = {
    "envelope",
    "history",
    "policy",
    "evaluation_timestamp",
    "risk_decision",
    "eligible",
    "reasons",
}


class PreDispatchError(ValueError):
    """The public error boundary for local, deterministic gate validation."""


class PreDispatchReason(StrEnum):
    """Canonical denial reason codes, in their canonical order."""

    STALE_AUTHORIZATION = "STALE_AUTHORIZATION"
    FUTURE_AUTHORIZATION = "FUTURE_AUTHORIZATION"
    FRESH_RISK_REJECTED = "FRESH_RISK_REJECTED"
    SUBMISSION_OUTCOME_UNKNOWN = "SUBMISSION_OUTCOME_UNKNOWN"
    SUBMISSION_ALREADY_ACKNOWLEDGED = "SUBMISSION_ALREADY_ACKNOWLEDGED"
    RE_DISPATCH_DEFERRED = "RE_DISPATCH_DEFERRED"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"


class _GateValue(BaseModel):
    """Frozen, strict, extra-forbidding base for the gate value objects."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _load_object(value: str, expected: set[str], label: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise PreDispatchError(f"{label} payload is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != expected:
        raise PreDispatchError(f"{label} payload has unexpected fields")
    return data


def _utc(value: Any, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise PreDispatchError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PreDispatchError(f"{name} must be timezone-aware")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise PreDispatchError(
            f"{name} is outside the representable UTC range"
        ) from exc


class AuthorizationFreshnessPolicy(_GateValue):
    """The explicit maximum age of a declared human authorization."""

    max_authorization_age: timedelta

    @field_validator("max_authorization_age")
    @classmethod
    def _positive(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise PreDispatchError(
                "max_authorization_age must be strictly positive"
            )
        return value

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise PreDispatchError(
                f"authorization freshness policy is invalid: {exc}"
            ) from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        data = _load_object(value, _POLICY_FIELDS, "authorization freshness policy")
        raw = data["max_authorization_age"]
        if not isinstance(raw, str):
            raise PreDispatchError(
                "max_authorization_age must be an ISO-8601 duration string"
            )
        try:
            age = TypeAdapter(timedelta).validate_json(json.dumps(raw))
        except ValidationError as exc:
            raise PreDispatchError(
                "max_authorization_age is not a valid ISO-8601 duration"
            ) from exc
        return cls(max_authorization_age=age)


class SubmissionHistoryState(_GateValue):
    """The explicit prior-dispatch fact for one exact submission identity."""

    submission_id: str = Field(pattern=_IDENTITY_PATTERN)
    state: Literal[
        "NEVER_DISPATCHED",
        "NO_EFFECT_CONFIRMED",
        "ACKNOWLEDGED",
        "OUTCOME_UNKNOWN",
    ]

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise PreDispatchError(
                f"submission history state is invalid: {exc}"
            ) from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        data = _load_object(value, _HISTORY_FIELDS, "submission history state")
        return cls(submission_id=data["submission_id"], state=data["state"])


def _expected_reasons(
    envelope: BrokerSubmissionEnvelope,
    history: SubmissionHistoryState,
    policy: AuthorizationFreshnessPolicy,
    evaluation_timestamp: datetime,
    risk_decision: RiskDecision,
) -> tuple[PreDispatchReason, ...]:
    reasons: set[PreDispatchReason] = set()
    authorized_at = envelope.permission.authorization.authorized_at
    if authorized_at > evaluation_timestamp:
        reasons.add(PreDispatchReason.FUTURE_AUTHORIZATION)
    if evaluation_timestamp - authorized_at > policy.max_authorization_age:
        reasons.add(PreDispatchReason.STALE_AUTHORIZATION)
    if risk_decision.approved is not True:
        reasons.add(PreDispatchReason.FRESH_RISK_REJECTED)
    if history.submission_id != envelope.submission_id:
        reasons.add(PreDispatchReason.RECONCILIATION_MISMATCH)
    elif history.state == "OUTCOME_UNKNOWN":
        reasons.add(PreDispatchReason.SUBMISSION_OUTCOME_UNKNOWN)
    elif history.state == "ACKNOWLEDGED":
        reasons.add(PreDispatchReason.SUBMISSION_ALREADY_ACKNOWLEDGED)
    elif history.state == "NO_EFFECT_CONFIRMED":
        reasons.add(PreDispatchReason.RE_DISPATCH_DEFERRED)
    return tuple(reason for reason in PreDispatchReason if reason in reasons)


class PreDispatchDecision(_GateValue):
    """The immutable, self-validating result of one gate evaluation."""

    envelope: BrokerSubmissionEnvelope
    history: SubmissionHistoryState
    policy: AuthorizationFreshnessPolicy
    evaluation_timestamp: datetime
    risk_decision: RiskDecision
    eligible: bool
    reasons: tuple[PreDispatchReason, ...]

    @field_validator("envelope", mode="before")
    @classmethod
    def _requires_envelope(cls, value: Any) -> Any:
        if not isinstance(value, BrokerSubmissionEnvelope):
            raise PreDispatchError(
                "envelope must be a quantforge.broker.BrokerSubmissionEnvelope"
            )
        return value

    @field_validator("history", mode="before")
    @classmethod
    def _requires_history(cls, value: Any) -> Any:
        if not isinstance(value, SubmissionHistoryState):
            raise PreDispatchError(
                "history must be a quantforge.predispatch.SubmissionHistoryState"
            )
        return value

    @field_validator("policy", mode="before")
    @classmethod
    def _requires_policy(cls, value: Any) -> Any:
        if not isinstance(value, AuthorizationFreshnessPolicy):
            raise PreDispatchError(
                "policy must be a quantforge.predispatch.AuthorizationFreshnessPolicy"
            )
        return value

    @field_validator("risk_decision", mode="before")
    @classmethod
    def _requires_risk_decision(cls, value: Any) -> Any:
        if not isinstance(value, RiskDecision):
            raise PreDispatchError(
                "risk_decision must be a quantforge.risk.RiskDecision"
            )
        return value

    @field_validator("evaluation_timestamp")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value, name="evaluation_timestamp")

    @field_validator("reasons", mode="before")
    @classmethod
    def _reason_codes(cls, value: Any) -> Any:
        if not isinstance(value, (tuple, list)):
            raise PreDispatchError("reasons must be a tuple of reason codes")
        try:
            return tuple(
                item if isinstance(item, PreDispatchReason) else PreDispatchReason(item)
                for item in value
            )
        except ValueError as exc:
            raise PreDispatchError("reasons contains an unknown reason code") from exc

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        expected = _expected_reasons(
            self.envelope,
            self.history,
            self.policy,
            self.evaluation_timestamp,
            self.risk_decision,
        )
        if self.reasons != expected:
            raise PreDispatchError(
                "decision reasons do not match the evaluated facts"
            )
        if self.eligible != (expected == ()):
            raise PreDispatchError(
                "decision eligibility contradicts its reasons"
            )
        return self

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise PreDispatchError(
                f"pre-dispatch decision is invalid: {exc}"
            ) from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        data = _load_object(value, _DECISION_FIELDS, "pre-dispatch decision")
        try:
            envelope = BrokerSubmissionEnvelope.from_json(json.dumps(data["envelope"]))
            history = SubmissionHistoryState.from_json(json.dumps(data["history"]))
            policy = AuthorizationFreshnessPolicy.from_json(json.dumps(data["policy"]))
            risk_decision = RiskDecision.from_json(json.dumps(data["risk_decision"]))
        except (ValidationError, ValueError) as exc:
            raise PreDispatchError(
                "pre-dispatch decision payload is invalid"
            ) from exc
        raw_moment = data["evaluation_timestamp"]
        if not isinstance(raw_moment, str):
            raise PreDispatchError(
                "evaluation_timestamp must be an ISO-8601 instant string"
            )
        try:
            moment = datetime.fromisoformat(raw_moment)
        except ValueError as exc:
            raise PreDispatchError(
                "evaluation_timestamp is not an ISO-8601 instant"
            ) from exc
        return cls(
            envelope=envelope,
            history=history,
            policy=policy,
            evaluation_timestamp=moment,
            risk_decision=risk_decision,
            eligible=data["eligible"],
            reasons=data["reasons"],
        )


def evaluate_pre_dispatch(
    envelope: BrokerSubmissionEnvelope,
    history: SubmissionHistoryState,
    policy: AuthorizationFreshnessPolicy,
    evaluation_timestamp: datetime,
    portfolio: PortfolioState,
    risk_prices: Mapping[str, Any],
    estimated_fee: Any,
    limits: RiskLimits,
) -> PreDispatchDecision:
    """Evaluate one explicit set of facts at one explicit instant."""
    if not isinstance(envelope, BrokerSubmissionEnvelope):
        raise TypeError(
            "envelope must be a quantforge.broker.BrokerSubmissionEnvelope"
        )
    if not isinstance(history, SubmissionHistoryState):
        raise TypeError(
            "history must be a quantforge.predispatch.SubmissionHistoryState"
        )
    if not isinstance(policy, AuthorizationFreshnessPolicy):
        raise TypeError(
            "policy must be a quantforge.predispatch.AuthorizationFreshnessPolicy"
        )
    if not isinstance(portfolio, PortfolioState):
        raise TypeError("portfolio must be a quantforge.portfolio.PortfolioState")
    if not isinstance(limits, RiskLimits):
        raise TypeError("limits must be a quantforge.risk.RiskLimits")
    moment = _utc(evaluation_timestamp, name="evaluation_timestamp")
    try:
        risk_decision = evaluate_order(
            envelope.order, portfolio, risk_prices, estimated_fee, limits
        )
    except RiskError as exc:
        raise PreDispatchError(
            f"fresh risk evaluation is invalid: {exc}"
        ) from exc
    reasons = _expected_reasons(envelope, history, policy, moment, risk_decision)
    return PreDispatchDecision(
        envelope=envelope,
        history=history,
        policy=policy,
        evaluation_timestamp=moment,
        risk_decision=risk_decision,
        eligible=reasons == (),
        reasons=reasons,
    )
