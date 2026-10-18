"""Immutable evidence values for the deterministic side-effect frontier.

These values validate caller-supplied declarations.  They do not perform or
prove any external action, persistence, authentication, or broker operation.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException
from enum import StrEnum
from typing import Any, ClassVar, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from ..domain import Position
from ..portfolio import PortfolioState
from ..provenance import FreshRiskProvenance, RiskPriceFact, RiskProvenanceError

_OPAQUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SUBMISSION_PATTERN = re.compile(r"^broker1-[0-9a-f]{32}$")
_PROVENANCE_PATTERN = re.compile(r"^riskprov1-[0-9a-f]{64}$")
_PRICE_PATTERN = re.compile(r"^riskprice1-[0-9a-f]{64}$")
_PORTFOLIO_PATTERN = re.compile(r"^riskportfolio1-[0-9a-f]{64}$")
_CANONICAL_INSTANT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)


class FrontierError(ValueError):
    """Public boundary for malformed or contradictory frontier artifacts."""


class FrontierReason(StrEnum):
    """Canonical denial reasons in their wire order."""

    PRE_DISPATCH_INELIGIBLE = "PRE_DISPATCH_INELIGIBLE"
    DISPATCH_TIMESTAMP_MISMATCH = "DISPATCH_TIMESTAMP_MISMATCH"
    STALE_PRICE_EVIDENCE = "STALE_PRICE_EVIDENCE"
    FUTURE_PRICE_EVIDENCE = "FUTURE_PRICE_EVIDENCE"
    PRICE_PROVENANCE_MISMATCH = "PRICE_PROVENANCE_MISMATCH"
    STALE_ACCOUNT_EVIDENCE = "STALE_ACCOUNT_EVIDENCE"
    FUTURE_ACCOUNT_EVIDENCE = "FUTURE_ACCOUNT_EVIDENCE"
    ACCOUNT_PROVENANCE_MISMATCH = "ACCOUNT_PROVENANCE_MISMATCH"
    DURABLE_RECORD_MISSING = "DURABLE_RECORD_MISSING"
    DURABLE_COMMIT_UNACKNOWLEDGED = "DURABLE_COMMIT_UNACKNOWLEDGED"
    RECOVERY_INCOMPLETE = "RECOVERY_INCOMPLETE"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    CLAIM_UNAVAILABLE = "CLAIM_UNAVAILABLE"
    OPERATOR_EVIDENCE_MISSING = "OPERATOR_EVIDENCE_MISSING"
    OPERATOR_DISABLED = "OPERATOR_DISABLED"
    OPERATOR_EVIDENCE_STALE = "OPERATOR_EVIDENCE_STALE"
    ENVIRONMENT_ACCOUNT_MISMATCH = "ENVIRONMENT_ACCOUNT_MISMATCH"
    CAPABILITY_INSUFFICIENT = "CAPABILITY_INSUFFICIENT"
    FRONTIER_ALREADY_CROSSED = "FRONTIER_ALREADY_CROSSED"


def _utc(value: Any, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise FrontierError(f"{name} must be a datetime")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise FrontierError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)
    except FrontierError:
        raise
    except (OverflowError, TypeError, ValueError) as exc:
        raise FrontierError(f"{name} is outside the representable UTC range") from exc


def _positive_duration(value: Any, *, name: str) -> timedelta:
    if not isinstance(value, timedelta):
        raise FrontierError(f"{name} must be a timedelta")
    if value <= timedelta(0):
        raise FrontierError(f"{name} must be strictly positive")
    return value


def _integer(value: Any, *, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FrontierError(f"{name} must be an integer")
    if value < minimum:
        raise FrontierError(f"{name} must be at least {minimum}")
    return value


def _identifier(value: Any, *, name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise FrontierError(f"{name} is malformed")
    return value


def _opaque(value: Any, *, name: str) -> str:
    return _identifier(value, name=name, pattern=_OPAQUE_PATTERN)


def _submission(value: Any, *, name: str = "submission_id") -> str:
    return _identifier(value, name=name, pattern=_SUBMISSION_PATTERN)


def _provenance_id(value: Any) -> str:
    return _identifier(value, name="provenance_id", pattern=_PROVENANCE_PATTERN)


def _price_commitment(value: Any) -> str:
    return _identifier(value, name="price_set_commitment", pattern=_PRICE_PATTERN)


def _portfolio_commitment(value: Any) -> str:
    return _identifier(value, name="portfolio_commitment", pattern=_PORTFOLIO_PATTERN)


def _tuple_of(value: Any, item_type: type[Any], *, name: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple) or any(
        not isinstance(item, item_type) for item in value
    ):
        raise FrontierError(f"{name} must be a tuple of {item_type.__name__} values")
    return value


def _canonical_price_tuple(value: Any) -> tuple[RiskPriceFact, ...]:
    facts = cast(
        tuple[RiskPriceFact, ...], _tuple_of(value, RiskPriceFact, name="risk_prices")
    )
    symbols = [fact.symbol for fact in facts]
    if len(set(symbols)) != len(symbols):
        raise FrontierError("risk_prices contains a duplicate symbol")
    try:
        expected = tuple(sorted(facts, key=lambda fact: fact.symbol.encode("utf-8")))
    except UnicodeEncodeError as exc:
        raise FrontierError("risk price symbols must be valid UTF-8") from exc
    if facts != expected:
        raise FrontierError("risk_prices is not in canonical symbol order")
    return facts


def _canonical_details(value: Any) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise FrontierError("mismatch_details must be a tuple")
    details: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise FrontierError("mismatch_details must contain strings")
        normalized = unicodedata.normalize("NFC", item)
        if (
            not normalized
            or normalized != item
            or len(item) > 512
            or any(ord(character) < 32 for character in item)
        ):
            raise FrontierError("mismatch_details contains a noncanonical detail")
        try:
            item.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise FrontierError("mismatch_details must be valid UTF-8") from exc
        details.append(item)
    expected = sorted(details, key=lambda item: item.encode("utf-8"))
    if details != expected or len(set(details)) != len(details):
        raise FrontierError("mismatch_details is not canonical")
    return tuple(details)


def _canonical_submission_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise FrontierError("unresolved_submission_ids must be a tuple")
    result = tuple(
        _submission(item, name="unresolved submission identity") for item in value
    )
    if result != tuple(sorted(result)) or len(set(result)) != len(result):
        raise FrontierError("unresolved_submission_ids is not canonical")
    return result


class _FrontierValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    _label: ClassVar[str] = "frontier artifact"

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise FrontierError(f"{self._label} is invalid: {exc}") from exc

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        if args or kwargs:
            raise TypeError("canonical frontier serialization accepts no options")
        return _canonical_json(_canonical_value(self))

    @classmethod
    def from_json(cls, value: str) -> Self:
        data = _load_json_object(value, cls._label)
        artifact = cast(Self, _parse_frontier_model(cls, data))
        if artifact.model_dump_json() != value:
            raise FrontierError(f"{cls._label} JSON is not canonical")
        return artifact


class ExecutionTargetBinding(_FrontierValue):
    _label = "execution target binding"

    schema: Literal["frontier-target-v1"]  # type: ignore[assignment]
    broker: str
    environment: Literal["PAPER", "SANDBOX", "PRODUCTION"]
    account_reference: str
    adapter_id: str
    adapter_version: str
    submission_id: str

    @field_validator(
        "broker", "account_reference", "adapter_id", "adapter_version", mode="before"
    )
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return _opaque(value, name=str(info.field_name))

    @field_validator("submission_id", mode="before")
    @classmethod
    def _submission_id(cls, value: Any) -> str:
        return _submission(value)


class PriceFreshnessEvidence(_FrontierValue):
    _label = "price freshness evidence"

    schema: Literal["frontier-price-v1"]  # type: ignore[assignment]
    evidence_id: str
    market_source: str
    source_observation_id: str
    provenance_id: str
    price_set_commitment: str
    risk_prices: tuple[RiskPriceFact, ...]
    observed_at: datetime
    maximum_age: timedelta

    @field_validator(
        "evidence_id", "market_source", "source_observation_id", mode="before"
    )
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return _opaque(value, name=str(info.field_name))

    @field_validator("provenance_id", mode="before")
    @classmethod
    def _provenance(cls, value: Any) -> str:
        return _provenance_id(value)

    @field_validator("price_set_commitment", mode="before")
    @classmethod
    def _commitment(cls, value: Any) -> str:
        return _price_commitment(value)

    @field_validator("risk_prices", mode="before")
    @classmethod
    def _prices(cls, value: Any) -> tuple[RiskPriceFact, ...]:
        return _canonical_price_tuple(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed(cls, value: Any) -> datetime:
        return _utc(value, name="observed_at")

    @field_validator("maximum_age", mode="before")
    @classmethod
    def _age(cls, value: Any) -> timedelta:
        return _positive_duration(value, name="maximum_age")


class AccountReconciliationEvidence(_FrontierValue):
    _label = "account reconciliation evidence"

    schema: Literal["frontier-account-v1"]  # type: ignore[assignment]
    evidence_id: str
    target: ExecutionTargetBinding
    observation_id: str
    provenance_id: str
    portfolio_commitment: str
    scope: Literal["FULL_PORTFOLIO_STATE"]
    observed_portfolio: PortfolioState
    observed_at: datetime
    maximum_age: timedelta
    reconciliation_status: Literal["MATCHED", "MISMATCHED"]
    mismatch_details: tuple[str, ...]

    @field_validator("evidence_id", "observation_id", mode="before")
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return _opaque(value, name=str(info.field_name))

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, value: Any) -> ExecutionTargetBinding:
        if not isinstance(value, ExecutionTargetBinding):
            raise FrontierError("target must be an ExecutionTargetBinding")
        return value

    @field_validator("provenance_id", mode="before")
    @classmethod
    def _provenance(cls, value: Any) -> str:
        return _provenance_id(value)

    @field_validator("portfolio_commitment", mode="before")
    @classmethod
    def _commitment(cls, value: Any) -> str:
        return _portfolio_commitment(value)

    @field_validator("observed_portfolio", mode="before")
    @classmethod
    def _portfolio(cls, value: Any) -> PortfolioState:
        if not isinstance(value, PortfolioState):
            raise FrontierError("observed_portfolio must be a PortfolioState")
        return value

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed(cls, value: Any) -> datetime:
        return _utc(value, name="observed_at")

    @field_validator("maximum_age", mode="before")
    @classmethod
    def _age(cls, value: Any) -> timedelta:
        return _positive_duration(value, name="maximum_age")

    @field_validator("mismatch_details", mode="before")
    @classmethod
    def _details(cls, value: Any) -> tuple[str, ...]:
        return _canonical_details(value)

    @model_validator(mode="after")
    def _status_matches_details(self) -> Self:
        if (self.reconciliation_status == "MATCHED") != (self.mismatch_details == ()):
            raise FrontierError("reconciliation status contradicts mismatch details")
        return self


class DurableSubmissionRecord(_FrontierValue):
    _label = "durable submission record"

    schema: Literal["frontier-durable-record-v1"]  # type: ignore[assignment]
    record_id: str
    submission_id: str
    provenance_id: str
    target: ExecutionTargetBinding
    generation: int
    recovery_epoch: int
    state: Literal[
        "PREPARED",
        "DISPATCHING",
        "ACKNOWLEDGED",
        "REJECTED",
        "OUTCOME_UNKNOWN",
        "NO_EFFECT_CONFIRMED",
    ]
    created_at: datetime
    updated_at: datetime

    @field_validator("record_id", mode="before")
    @classmethod
    def _record_id(cls, value: Any) -> str:
        return _opaque(value, name="record_id")

    @field_validator("submission_id", mode="before")
    @classmethod
    def _submission_id(cls, value: Any) -> str:
        return _submission(value)

    @field_validator("provenance_id", mode="before")
    @classmethod
    def _provenance(cls, value: Any) -> str:
        return _provenance_id(value)

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, value: Any) -> ExecutionTargetBinding:
        if not isinstance(value, ExecutionTargetBinding):
            raise FrontierError("target must be an ExecutionTargetBinding")
        return value

    @field_validator("generation", "recovery_epoch", mode="before")
    @classmethod
    def _positive_integer(cls, value: Any, info: Any) -> int:
        return _integer(value, name=str(info.field_name), minimum=1)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _moments(cls, value: Any, info: Any) -> datetime:
        return _utc(value, name=str(info.field_name))

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.target.submission_id != self.submission_id:
            raise FrontierError("record target contradicts submission_id")
        if self.updated_at < self.created_at:
            raise FrontierError("record update precedes creation")
        return self


class DurableCommitReceipt(_FrontierValue):
    _label = "durable commit receipt"

    schema: Literal["frontier-commit-receipt-v1"]  # type: ignore[assignment]
    receipt_id: str
    record_id: str
    submission_id: str
    target: ExecutionTargetBinding
    generation: int
    recovery_epoch: int
    committed: bool
    committed_at: datetime

    @field_validator("receipt_id", "record_id", mode="before")
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return _opaque(value, name=str(info.field_name))

    @field_validator("submission_id", mode="before")
    @classmethod
    def _submission_id(cls, value: Any) -> str:
        return _submission(value)

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, value: Any) -> ExecutionTargetBinding:
        if not isinstance(value, ExecutionTargetBinding):
            raise FrontierError("target must be an ExecutionTargetBinding")
        return value

    @field_validator("generation", "recovery_epoch", mode="before")
    @classmethod
    def _positive_integer(cls, value: Any, info: Any) -> int:
        return _integer(value, name=str(info.field_name), minimum=1)

    @field_validator("committed", mode="before")
    @classmethod
    def _strict_bool(cls, value: Any) -> bool:
        if not isinstance(value, bool):
            raise FrontierError("committed must be a Boolean")
        return value

    @field_validator("committed_at", mode="before")
    @classmethod
    def _moment(cls, value: Any) -> datetime:
        return _utc(value, name="committed_at")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.target.submission_id != self.submission_id:
            raise FrontierError("receipt target contradicts submission_id")
        return self


class AtomicDispatchClaimEvidence(_FrontierValue):
    _label = "atomic dispatch claim evidence"

    schema: Literal["frontier-claim-v1"]  # type: ignore[assignment]
    claim_id: str
    submission_id: str
    target: ExecutionTargetBinding
    record_id: str
    generation: int
    recovery_epoch: int
    owner_id: str
    token: str
    status: Literal["ACQUIRED", "CONFLICT"]
    acquired_at: datetime
    expires_at: datetime | None

    @field_validator("claim_id", "record_id", "owner_id", "token", mode="before")
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return _opaque(value, name=str(info.field_name))

    @field_validator("submission_id", mode="before")
    @classmethod
    def _submission_id(cls, value: Any) -> str:
        return _submission(value)

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, value: Any) -> ExecutionTargetBinding:
        if not isinstance(value, ExecutionTargetBinding):
            raise FrontierError("target must be an ExecutionTargetBinding")
        return value

    @field_validator("generation", "recovery_epoch", mode="before")
    @classmethod
    def _positive_integer(cls, value: Any, info: Any) -> int:
        return _integer(value, name=str(info.field_name), minimum=1)

    @field_validator("acquired_at", mode="before")
    @classmethod
    def _acquired(cls, value: Any) -> datetime:
        return _utc(value, name="acquired_at")

    @field_validator("expires_at", mode="before")
    @classmethod
    def _expiry(cls, value: Any) -> datetime | None:
        return None if value is None else _utc(value, name="expires_at")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.target.submission_id != self.submission_id:
            raise FrontierError("claim target contradicts submission_id")
        if self.expires_at is not None and self.expires_at < self.acquired_at:
            raise FrontierError("claim expiry precedes acquisition")
        return self


class RecoveryCompleteEvidence(_FrontierValue):
    _label = "recovery complete evidence"

    schema: Literal["frontier-recovery-v1"]  # type: ignore[assignment]
    evidence_id: str
    target: ExecutionTargetBinding
    recovery_epoch: int
    scanned_through_generation: int
    status: Literal["COMPLETE", "INCOMPLETE"]
    unresolved_submission_ids: tuple[str, ...]
    observed_at: datetime

    @field_validator("evidence_id", mode="before")
    @classmethod
    def _evidence_id(cls, value: Any) -> str:
        return _opaque(value, name="evidence_id")

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, value: Any) -> ExecutionTargetBinding:
        if not isinstance(value, ExecutionTargetBinding):
            raise FrontierError("target must be an ExecutionTargetBinding")
        return value

    @field_validator("recovery_epoch", mode="before")
    @classmethod
    def _epoch(cls, value: Any) -> int:
        return _integer(value, name="recovery_epoch", minimum=1)

    @field_validator("scanned_through_generation", mode="before")
    @classmethod
    def _generation(cls, value: Any) -> int:
        return _integer(value, name="scanned_through_generation", minimum=0)

    @field_validator("unresolved_submission_ids", mode="before")
    @classmethod
    def _unresolved(cls, value: Any) -> tuple[str, ...]:
        return _canonical_submission_tuple(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed(cls, value: Any) -> datetime:
        return _utc(value, name="observed_at")


class OperatorExecutionEnableEvidence(_FrontierValue):
    _label = "operator execution enable evidence"

    schema: Literal["frontier-operator-v1"]  # type: ignore[assignment]
    evidence_id: str
    target: ExecutionTargetBinding
    action_id: str
    enabled: bool
    observed_at: datetime
    maximum_age: timedelta

    @field_validator("evidence_id", "action_id", mode="before")
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return _opaque(value, name=str(info.field_name))

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, value: Any) -> ExecutionTargetBinding:
        if not isinstance(value, ExecutionTargetBinding):
            raise FrontierError("target must be an ExecutionTargetBinding")
        return value

    @field_validator("enabled", mode="before")
    @classmethod
    def _strict_bool(cls, value: Any) -> bool:
        if not isinstance(value, bool):
            raise FrontierError("enabled must be a Boolean")
        return value

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed(cls, value: Any) -> datetime:
        return _utc(value, name="observed_at")

    @field_validator("maximum_age", mode="before")
    @classmethod
    def _age(cls, value: Any) -> timedelta:
        return _positive_duration(value, name="maximum_age")


class BrokerCapabilityDeclaration(_FrontierValue):
    _label = "broker capability declaration"

    schema: Literal["frontier-capability-v1"]  # type: ignore[assignment]
    declaration_id: str
    source_reference: str
    target: ExecutionTargetBinding
    client_identity_mode: Literal["BROKER_ENFORCED", "LOCAL_ONLY", "UNKNOWN"]
    uniqueness_scope: Literal["ACCOUNT", "BROKER", "NONE", "UNKNOWN"]
    uniqueness_window: timedelta | None
    duplicate_behavior: Literal[
        "RETURN_EXISTING", "REJECT_DUPLICATE", "MAY_DUPLICATE", "UNKNOWN"
    ]
    lookup_mode: Literal["BY_CLIENT_ID", "UNSUPPORTED", "UNKNOWN"]
    reconciliation_mode: Literal["SUPPORTED", "UNSUPPORTED", "UNKNOWN"]
    timeout_semantics: Literal["OUTCOME_UNKNOWN", "UNSAFE_REJECTION", "UNKNOWN"]
    frontier_classification: Literal["CONSERVATIVE", "UNSAFE", "UNKNOWN"]
    acknowledgement_semantics: Literal["ACK_NOT_FILL", "ACK_AS_FILL", "UNKNOWN"]

    @field_validator("declaration_id", "source_reference", mode="before")
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return _opaque(value, name=str(info.field_name))

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, value: Any) -> ExecutionTargetBinding:
        if not isinstance(value, ExecutionTargetBinding):
            raise FrontierError("target must be an ExecutionTargetBinding")
        return value

    @field_validator("uniqueness_window", mode="before")
    @classmethod
    def _window(cls, value: Any) -> timedelta | None:
        return (
            None
            if value is None
            else _positive_duration(value, name="uniqueness_window")
        )

    @model_validator(mode="after")
    def _identity_declaration_is_consistent(self) -> Self:
        if self.client_identity_mode == "BROKER_ENFORCED":
            valid = (
                self.uniqueness_scope in ("ACCOUNT", "BROKER")
                and self.uniqueness_window is not None
                and self.duplicate_behavior in ("RETURN_EXISTING", "REJECT_DUPLICATE")
            )
        elif self.client_identity_mode == "LOCAL_ONLY":
            valid = (
                self.uniqueness_scope == "NONE"
                and self.uniqueness_window is None
                and self.duplicate_behavior == "MAY_DUPLICATE"
            )
        else:
            valid = (
                self.uniqueness_scope == "UNKNOWN"
                and self.uniqueness_window is None
                and self.duplicate_behavior == "UNKNOWN"
            )
        if not valid:
            raise FrontierError("broker identity declaration is inconsistent")
        return self


class FrontierEligibilityDecision(_FrontierValue):
    _label = "frontier eligibility decision"

    schema: Literal["frontier-decision-v1"]  # type: ignore[assignment]
    submission_id: str
    provenance_id: str
    provenance: FreshRiskProvenance
    dispatch_timestamp: datetime
    target: ExecutionTargetBinding
    price_evidence: PriceFreshnessEvidence | None
    account_evidence: AccountReconciliationEvidence | None
    durable_record: DurableSubmissionRecord | None
    commit_receipt: DurableCommitReceipt | None
    recovery_evidence: RecoveryCompleteEvidence | None
    operator_evidence: OperatorExecutionEnableEvidence | None
    capability: BrokerCapabilityDeclaration | None
    claim: AtomicDispatchClaimEvidence | None
    eligible: bool
    reasons: tuple[FrontierReason, ...]

    @field_validator("submission_id", mode="before")
    @classmethod
    def _submission_id(cls, value: Any) -> str:
        return _submission(value)

    @field_validator("provenance_id", mode="before")
    @classmethod
    def _provenance_id(cls, value: Any) -> str:
        return _provenance_id(value)

    @field_validator("provenance", mode="before")
    @classmethod
    def _provenance(cls, value: Any) -> FreshRiskProvenance:
        if not isinstance(value, FreshRiskProvenance):
            raise FrontierError("provenance must be a FreshRiskProvenance")
        return value

    @field_validator("dispatch_timestamp", mode="before")
    @classmethod
    def _dispatch_timestamp(cls, value: Any) -> datetime:
        return _utc(value, name="dispatch_timestamp")

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, value: Any) -> ExecutionTargetBinding:
        if not isinstance(value, ExecutionTargetBinding):
            raise FrontierError("target must be an ExecutionTargetBinding")
        return value

    @field_validator(
        "price_evidence",
        "account_evidence",
        "durable_record",
        "commit_receipt",
        "recovery_evidence",
        "operator_evidence",
        "capability",
        "claim",
        mode="before",
    )
    @classmethod
    def _optional_artifact_type(cls, value: Any, info: Any) -> Any:
        expected: dict[str, type[_FrontierValue]] = {
            "price_evidence": PriceFreshnessEvidence,
            "account_evidence": AccountReconciliationEvidence,
            "durable_record": DurableSubmissionRecord,
            "commit_receipt": DurableCommitReceipt,
            "recovery_evidence": RecoveryCompleteEvidence,
            "operator_evidence": OperatorExecutionEnableEvidence,
            "capability": BrokerCapabilityDeclaration,
            "claim": AtomicDispatchClaimEvidence,
        }
        if value is not None and not isinstance(value, expected[str(info.field_name)]):
            raise FrontierError(f"{info.field_name} has the wrong artifact type")
        return value

    @field_validator("eligible", mode="before")
    @classmethod
    def _strict_bool(cls, value: Any) -> bool:
        if not isinstance(value, bool):
            raise FrontierError("eligible must be a Boolean")
        return value

    @field_validator("reasons", mode="before")
    @classmethod
    def _canonical_reasons(cls, value: Any) -> tuple[FrontierReason, ...]:
        reasons = cast(
            tuple[FrontierReason, ...],
            _tuple_of(value, FrontierReason, name="reasons"),
        )
        present = set(reasons)
        return tuple(reason for reason in FrontierReason if reason in present)

    @model_validator(mode="after")
    def _replay(self) -> Self:
        expected_submission = (
            self.provenance.pre_dispatch_decision.envelope.submission_id
        )
        if self.submission_id != expected_submission:
            raise FrontierError("decision submission_id contradicts provenance")
        if self.provenance_id != self.provenance.provenance_id:
            raise FrontierError("decision provenance_id contradicts provenance")
        from .evaluate import _expected_reasons

        expected = _expected_reasons(
            self.provenance,
            self.dispatch_timestamp,
            self.target,
            price_evidence=self.price_evidence,
            account_evidence=self.account_evidence,
            durable_record=self.durable_record,
            commit_receipt=self.commit_receipt,
            recovery_evidence=self.recovery_evidence,
            operator_evidence=self.operator_evidence,
            capability=self.capability,
            claim=self.claim,
        )
        if self.reasons != expected:
            raise FrontierError("decision reasons do not match embedded inputs")
        if self.eligible != (expected == ()):
            raise FrontierError("decision eligibility contradicts its reasons")
        return self


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


def _duration_microseconds(value: timedelta) -> str:
    return str(
        value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
    )


def _canonical_portfolio(value: PortfolioState) -> dict[str, Any]:
    return {
        "cash": _canonical_decimal(value.cash),
        "cumulative_fees": _canonical_decimal(value.cumulative_fees),
        "initial_cash": _canonical_decimal(value.initial_cash),
        "last_fill_timestamp": (
            None
            if value.last_fill_timestamp is None
            else _canonical_datetime(value.last_fill_timestamp)
        ),
        "positions": [
            {
                "average_price": _canonical_decimal(position.average_price),
                "quantity": _canonical_decimal(position.quantity),
                "symbol": position.symbol,
            }
            for position in value.positions
        ],
    }


def _canonical_price_fact(value: RiskPriceFact) -> dict[str, Any]:
    return {"price": _canonical_decimal(value.price), "symbol": value.symbol}


def _canonical_value(value: Any) -> Any:
    if isinstance(value, FrontierReason):
        return value.value
    if isinstance(value, _FrontierValue):
        return {
            name: _canonical_value(getattr(value, name))
            for name in type(value).model_fields
        }
    if isinstance(value, FreshRiskProvenance):
        return json.loads(value.model_dump_json())
    if isinstance(value, RiskPriceFact):
        return _canonical_price_fact(value)
    if isinstance(value, PortfolioState):
        return _canonical_portfolio(value)
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, timedelta):
        return _duration_microseconds(value)
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise FrontierError(f"unsupported canonical value {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrontierError(f"JSON object contains duplicate field {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise FrontierError(f"JSON contains invalid constant {value}")


def _load_json_object(value: str, label: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise FrontierError(f"{label} JSON must be a string")
    try:
        data = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_fields,
            parse_constant=_invalid_constant,
        )
    except FrontierError:
        raise
    except (TypeError, ValueError) as exc:
        raise FrontierError(f"{label} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise FrontierError(f"{label} must be a JSON object")
    return data


def _require_fields(
    value: Any, model: type[_FrontierValue], *, label: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(model.model_fields):
        raise FrontierError(f"{label} has unexpected fields")
    return value


def _require_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise FrontierError(f"{label} must be a string")
    return value


def _parse_datetime(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or _CANONICAL_INSTANT.fullmatch(value) is None:
        raise FrontierError(f"{label} is not a canonical UTC instant")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FrontierError(f"{label} is not a valid UTC instant") from exc
    if _canonical_datetime(moment) != value:
        raise FrontierError(f"{label} is not canonical")
    return moment


def _parse_duration(
    value: Any, *, label: str, optional: bool = False
) -> timedelta | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isdigit()
        or (len(value) > 1 and value[0] == "0")
    ):
        raise FrontierError(f"{label} is not a canonical duration")
    try:
        result = timedelta(microseconds=int(value))
    except (OverflowError, ValueError) as exc:
        raise FrontierError(f"{label} is outside the timedelta range") from exc
    if _duration_microseconds(result) != value:
        raise FrontierError(f"{label} is not a canonical duration")
    return result


def _parse_decimal(value: Any, *, label: str) -> Decimal:
    if not isinstance(value, dict) or set(value) != {"coefficient", "exponent", "sign"}:
        raise FrontierError(f"{label} is not a Decimal token")
    coefficient = value["coefficient"]
    exponent = value["exponent"]
    sign = value["sign"]
    if (
        not isinstance(coefficient, str)
        or not coefficient.isascii()
        or isinstance(exponent, bool)
        or not isinstance(exponent, int)
        or isinstance(sign, bool)
        or not isinstance(sign, int)
        or sign not in (0, 1)
    ):
        raise FrontierError(f"{label} is not a canonical Decimal token")
    if coefficient == "0":
        if exponent != 0 or sign != 0:
            raise FrontierError(f"{label} zero is not canonical")
    elif (
        not coefficient
        or not coefficient.isdigit()
        or coefficient[0] == "0"
        or coefficient[-1] == "0"
    ):
        raise FrontierError(f"{label} coefficient is not canonical")
    try:
        number = Decimal((sign, tuple(int(digit) for digit in coefficient), exponent))
    except (DecimalException, OverflowError, ValueError) as exc:
        raise FrontierError(f"{label} is not a representable Decimal") from exc
    if not number.is_finite() or _canonical_decimal(number) != value:
        raise FrontierError(f"{label} is not a canonical finite Decimal")
    return number


def _parse_price_fact(value: Any, *, label: str) -> RiskPriceFact:
    if not isinstance(value, dict) or set(value) != {"price", "symbol"}:
        raise FrontierError(f"{label} has unexpected fields")
    try:
        return RiskPriceFact(
            symbol=_require_string(value["symbol"], label=f"{label}.symbol"),
            price=_parse_decimal(value["price"], label=f"{label}.price"),
        )
    except (RiskProvenanceError, ValidationError, ValueError) as exc:
        raise FrontierError(f"{label} is invalid") from exc


def _parse_prices(value: Any, *, label: str) -> tuple[RiskPriceFact, ...]:
    if not isinstance(value, list):
        raise FrontierError(f"{label} must be an array")
    return tuple(
        _parse_price_fact(item, label=f"{label}[{index}]")
        for index, item in enumerate(value)
    )


def _parse_portfolio(value: Any, *, label: str) -> PortfolioState:
    fields = {
        "cash",
        "cumulative_fees",
        "initial_cash",
        "last_fill_timestamp",
        "positions",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise FrontierError(f"{label} has unexpected fields")
    raw_positions = value["positions"]
    if not isinstance(raw_positions, list):
        raise FrontierError(f"{label}.positions must be an array")
    positions: list[Position] = []
    for index, raw in enumerate(raw_positions):
        item_label = f"{label}.positions[{index}]"
        if not isinstance(raw, dict) or set(raw) != {
            "average_price",
            "quantity",
            "symbol",
        }:
            raise FrontierError(f"{item_label} has unexpected fields")
        try:
            positions.append(
                Position(
                    symbol=_require_string(raw["symbol"], label=f"{item_label}.symbol"),
                    quantity=_parse_decimal(
                        raw["quantity"], label=f"{item_label}.quantity"
                    ),
                    average_price=_parse_decimal(
                        raw["average_price"], label=f"{item_label}.average_price"
                    ),
                )
            )
        except (ValidationError, ValueError) as exc:
            raise FrontierError(f"{item_label} is invalid") from exc
    raw_last_fill = value["last_fill_timestamp"]
    last_fill = (
        None
        if raw_last_fill is None
        else _parse_datetime(raw_last_fill, label=f"{label}.last_fill_timestamp")
    )
    try:
        portfolio = PortfolioState(
            initial_cash=_parse_decimal(
                value["initial_cash"], label=f"{label}.initial_cash"
            ),
            cash=_parse_decimal(value["cash"], label=f"{label}.cash"),
            positions=tuple(positions),
            cumulative_fees=_parse_decimal(
                value["cumulative_fees"], label=f"{label}.cumulative_fees"
            ),
            last_fill_timestamp=last_fill,
        )
    except (ValidationError, ValueError) as exc:
        raise FrontierError(f"{label} is invalid") from exc
    if tuple(positions) != portfolio.positions:
        raise FrontierError(f"{label}.positions is not canonical")
    return portfolio


def _parse_target(value: Any, *, label: str) -> ExecutionTargetBinding:
    data = _require_fields(value, ExecutionTargetBinding, label=label)
    return ExecutionTargetBinding(**data)


def _parse_provenance(value: Any, *, label: str) -> FreshRiskProvenance:
    if not isinstance(value, dict):
        raise FrontierError(f"{label} must be an object")
    try:
        return FreshRiskProvenance.from_json(_canonical_json(value))
    except (RiskProvenanceError, ValidationError, ValueError) as exc:
        raise FrontierError(f"{label} is invalid") from exc


def _parse_frontier_model(
    model: type[_FrontierValue], data: dict[str, Any]
) -> _FrontierValue:
    values = _require_fields(data, model, label=model._label).copy()
    if model is ExecutionTargetBinding:
        return ExecutionTargetBinding(**values)
    if model is PriceFreshnessEvidence:
        values["risk_prices"] = _parse_prices(
            values["risk_prices"], label="risk_prices"
        )
        values["observed_at"] = _parse_datetime(
            values["observed_at"], label="observed_at"
        )
        values["maximum_age"] = _parse_duration(
            values["maximum_age"], label="maximum_age"
        )
        return PriceFreshnessEvidence(**values)
    if model is AccountReconciliationEvidence:
        values["target"] = _parse_target(values["target"], label="target")
        values["observed_portfolio"] = _parse_portfolio(
            values["observed_portfolio"], label="observed_portfolio"
        )
        values["observed_at"] = _parse_datetime(
            values["observed_at"], label="observed_at"
        )
        values["maximum_age"] = _parse_duration(
            values["maximum_age"], label="maximum_age"
        )
        values["mismatch_details"] = (
            tuple(values["mismatch_details"])
            if isinstance(values["mismatch_details"], list)
            else values["mismatch_details"]
        )
        return AccountReconciliationEvidence(**values)
    if model is DurableSubmissionRecord:
        values["target"] = _parse_target(values["target"], label="target")
        values["created_at"] = _parse_datetime(values["created_at"], label="created_at")
        values["updated_at"] = _parse_datetime(values["updated_at"], label="updated_at")
        return DurableSubmissionRecord(**values)
    if model is DurableCommitReceipt:
        values["target"] = _parse_target(values["target"], label="target")
        values["committed_at"] = _parse_datetime(
            values["committed_at"], label="committed_at"
        )
        return DurableCommitReceipt(**values)
    if model is AtomicDispatchClaimEvidence:
        values["target"] = _parse_target(values["target"], label="target")
        values["acquired_at"] = _parse_datetime(
            values["acquired_at"], label="acquired_at"
        )
        values["expires_at"] = (
            None
            if values["expires_at"] is None
            else _parse_datetime(values["expires_at"], label="expires_at")
        )
        return AtomicDispatchClaimEvidence(**values)
    if model is RecoveryCompleteEvidence:
        values["target"] = _parse_target(values["target"], label="target")
        values["unresolved_submission_ids"] = (
            tuple(values["unresolved_submission_ids"])
            if isinstance(values["unresolved_submission_ids"], list)
            else values["unresolved_submission_ids"]
        )
        values["observed_at"] = _parse_datetime(
            values["observed_at"], label="observed_at"
        )
        return RecoveryCompleteEvidence(**values)
    if model is OperatorExecutionEnableEvidence:
        values["target"] = _parse_target(values["target"], label="target")
        values["observed_at"] = _parse_datetime(
            values["observed_at"], label="observed_at"
        )
        values["maximum_age"] = _parse_duration(
            values["maximum_age"], label="maximum_age"
        )
        return OperatorExecutionEnableEvidence(**values)
    if model is BrokerCapabilityDeclaration:
        values["target"] = _parse_target(values["target"], label="target")
        values["uniqueness_window"] = _parse_duration(
            values["uniqueness_window"], label="uniqueness_window", optional=True
        )
        return BrokerCapabilityDeclaration(**values)
    if model is FrontierEligibilityDecision:
        values["provenance"] = _parse_provenance(
            values["provenance"], label="provenance"
        )
        values["dispatch_timestamp"] = _parse_datetime(
            values["dispatch_timestamp"], label="dispatch_timestamp"
        )
        values["target"] = _parse_target(values["target"], label="target")
        nested: dict[str, type[_FrontierValue]] = {
            "price_evidence": PriceFreshnessEvidence,
            "account_evidence": AccountReconciliationEvidence,
            "durable_record": DurableSubmissionRecord,
            "commit_receipt": DurableCommitReceipt,
            "recovery_evidence": RecoveryCompleteEvidence,
            "operator_evidence": OperatorExecutionEnableEvidence,
            "capability": BrokerCapabilityDeclaration,
            "claim": AtomicDispatchClaimEvidence,
        }
        for name, nested_model in nested.items():
            if values[name] is not None:
                if not isinstance(values[name], dict):
                    raise FrontierError(f"{name} must be an object or null")
                values[name] = _parse_frontier_model(nested_model, values[name])
        raw_reasons = values["reasons"]
        if not isinstance(raw_reasons, list) or any(
            not isinstance(item, str) for item in raw_reasons
        ):
            raise FrontierError("reasons must be an array of strings")
        try:
            values["reasons"] = tuple(FrontierReason(item) for item in raw_reasons)
        except ValueError as exc:
            raise FrontierError("reasons contains an unknown reason") from exc
        return FrontierEligibilityDecision(**values)
    raise FrontierError("unsupported frontier model")
