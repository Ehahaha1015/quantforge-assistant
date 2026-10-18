"""Frozen OPERATOR-001 public value objects."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from ..frontier import ExecutionTargetBinding
from .canonical import (
    commitment,
    opaque,
    positive_duration,
    utc,
)
from .errors import (
    OperatorArgumentError,
    OperatorAuthenticationError,
    OperatorError,
)

OPERATOR_MAXIMUM_AGE_CEILING = timedelta(hours=1)


class ControlAction(StrEnum):
    ENABLE = "ENABLE"
    DISABLE = "DISABLE"


class ControlState(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class _OperatorValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    _error_class: ClassVar[type[OperatorError]] = OperatorArgumentError
    _label: ClassVar[str] = "operator value"

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise self._error_class(f"{self._label} is invalid: {exc}") from exc


class OperatorControlScope(_OperatorValue):
    _label = "operator control scope"

    schema: Literal["operator-scope-v1"]  # type: ignore[assignment]
    broker: str
    environment: Literal["PAPER", "SANDBOX", "PRODUCTION"]
    account_reference: str
    adapter_id: str
    adapter_version: str

    @field_validator(
        "broker", "account_reference", "adapter_id", "adapter_version", mode="before"
    )
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return opaque(value, name=str(info.field_name))

    @classmethod
    def from_target(cls, target: ExecutionTargetBinding) -> Self:
        if not isinstance(target, ExecutionTargetBinding):
            raise OperatorArgumentError("target must be an ExecutionTargetBinding")
        return cls(
            schema="operator-scope-v1",
            broker=target.broker,
            environment=target.environment,
            account_reference=target.account_reference,
            adapter_id=target.adapter_id,
            adapter_version=target.adapter_version,
        )

    def matches(self, target: ExecutionTargetBinding) -> bool:
        if not isinstance(target, ExecutionTargetBinding):
            return False
        return (
            self.broker == target.broker
            and self.environment == target.environment
            and self.account_reference == target.account_reference
            and self.adapter_id == target.adapter_id
            and self.adapter_version == target.adapter_version
        )


class OperatorControlRequest(_OperatorValue):
    _label = "operator control request"

    schema: Literal["operator-request-v1"]  # type: ignore[assignment]
    request_id: str
    action: ControlAction
    scope: OperatorControlScope
    operator_reference: str
    control_source: str
    observed_at: datetime
    maximum_age: timedelta

    @field_validator(
        "request_id", "operator_reference", "control_source", mode="before"
    )
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return opaque(value, name=str(info.field_name))

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed_at(cls, value: Any) -> datetime:
        return utc(value, name="observed_at")

    @field_validator("maximum_age", mode="before")
    @classmethod
    def _maximum_age(cls, value: Any) -> timedelta:
        return positive_duration(value, name="maximum_age")


class OperatorControlPolicy(_OperatorValue):
    _label = "operator control policy"

    schema: Literal["operator-policy-v1"]  # type: ignore[assignment]
    maximum_age_upper_bound: timedelta

    @field_validator("maximum_age_upper_bound", mode="before")
    @classmethod
    def _bound(cls, value: Any) -> timedelta:
        return positive_duration(value, name="maximum_age_upper_bound")

    @model_validator(mode="after")
    def _ceiling(self) -> Self:
        if self.maximum_age_upper_bound > OPERATOR_MAXIMUM_AGE_CEILING:
            raise OperatorArgumentError(
                "maximum_age_upper_bound exceeds the contract ceiling"
            )
        return self


class OperatorAuthenticationProof(_OperatorValue):
    _label = "operator authentication proof"
    _error_class = OperatorAuthenticationError

    schema: Literal["operator-proof-v1"]  # type: ignore[assignment]
    proof_id: str
    operator_reference: str
    control_source: str
    action: ControlAction
    scope: OperatorControlScope
    method: Literal["LOCAL_VERIFIER"]
    request_commitment: str

    @field_validator(
        "proof_id", "operator_reference", "control_source", mode="before"
    )
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return opaque(value, name=str(info.field_name))

    @field_validator("request_commitment", mode="before")
    @classmethod
    def _commitment(cls, value: Any) -> str:
        return commitment(value)


class VerifiedOperatorIdentity(_OperatorValue):
    _label = "verified operator identity"
    _error_class = OperatorAuthenticationError

    schema: Literal["operator-verified-v1"]  # type: ignore[assignment]
    operator_reference: str
    control_source: str
    verifier_id: str
    verified_at: datetime
    proof_id: str

    @field_validator(
        "operator_reference", "control_source", "verifier_id", "proof_id",
        mode="before",
    )
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return opaque(value, name=str(info.field_name))

    @field_validator("verified_at", mode="before")
    @classmethod
    def _verified_at(cls, value: Any) -> datetime:
        return utc(value, name="verified_at")


class OperatorControlState(_OperatorValue):
    _label = "operator control state"

    scope: OperatorControlScope
    scope_key: str
    state: ControlState
    control_sequence: int
    action_id: str
    state_version_id: str
    observed_at: datetime
    maximum_age: timedelta

    @field_validator("scope_key", "action_id", "state_version_id", mode="before")
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return opaque(value, name=str(info.field_name))

    @field_validator("control_sequence", mode="before")
    @classmethod
    def _sequence(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise OperatorArgumentError("control_sequence must be at least 1")
        return value

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed_at(cls, value: Any) -> datetime:
        return utc(value, name="observed_at")

    @field_validator("maximum_age", mode="before")
    @classmethod
    def _maximum_age(cls, value: Any) -> timedelta:
        return positive_duration(value, name="maximum_age")


class OperatorControlAuditEvent(_OperatorValue):
    _label = "operator control audit event"

    control_sequence: int
    audit_id: str
    request_id: str
    action_id: str
    scope_key: str
    broker: str
    environment: Literal["PAPER", "SANDBOX", "PRODUCTION"]
    account_reference: str
    adapter_id: str
    adapter_version: str
    requested_action: ControlAction
    resulting_state: ControlState
    state_version_id: str
    operator_reference: str
    control_source: str
    verifier_id: str
    proof_id: str
    verified_at: datetime
    observed_at: datetime
    maximum_age: timedelta
    committed_at: datetime
    request_commitment: str

    @field_validator(
        "audit_id",
        "request_id",
        "action_id",
        "scope_key",
        "broker",
        "account_reference",
        "adapter_id",
        "adapter_version",
        "state_version_id",
        "operator_reference",
        "control_source",
        "verifier_id",
        "proof_id",
        mode="before",
    )
    @classmethod
    def _opaque_fields(cls, value: Any, info: Any) -> str:
        return opaque(value, name=str(info.field_name))

    @field_validator("control_sequence", mode="before")
    @classmethod
    def _sequence(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise OperatorArgumentError("control_sequence must be at least 1")
        return value

    @field_validator("verified_at", "observed_at", "committed_at", mode="before")
    @classmethod
    def _instants(cls, value: Any, info: Any) -> datetime:
        return utc(value, name=str(info.field_name))

    @field_validator("maximum_age", mode="before")
    @classmethod
    def _maximum_age(cls, value: Any) -> timedelta:
        return positive_duration(value, name="maximum_age")

    @field_validator("request_commitment", mode="before")
    @classmethod
    def _commitment(cls, value: Any) -> str:
        return commitment(value)

    @property
    def scope(self) -> OperatorControlScope:
        return OperatorControlScope(
            schema="operator-scope-v1",
            broker=self.broker,
            environment=self.environment,
            account_reference=self.account_reference,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
        )


class OperatorControlOutcome(_OperatorValue):
    _label = "operator control outcome"

    replay: bool
    action_id: str
    original_control_sequence: int | None
    original_state_version_id: str | None
    original_audit_id: str | None
    original_resulting_state: ControlState | None
    current_effective_state: ControlState | None

    @field_validator("action_id", mode="before")
    @classmethod
    def _action_id(cls, value: Any) -> str:
        return opaque(value, name="action_id")

    @field_validator(
        "original_state_version_id", "original_audit_id", mode="before"
    )
    @classmethod
    def _optional_opaque(cls, value: Any, info: Any) -> str | None:
        if value is None:
            return None
        return opaque(value, name=str(info.field_name))

    @field_validator("original_control_sequence", mode="before")
    @classmethod
    def _optional_sequence(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise OperatorArgumentError(
                "original_control_sequence must be at least 1"
            )
        return value


__all__ = [
    "OPERATOR_MAXIMUM_AGE_CEILING",
    "ControlAction",
    "ControlState",
    "OperatorAuthenticationProof",
    "OperatorControlAuditEvent",
    "OperatorControlOutcome",
    "OperatorControlPolicy",
    "OperatorControlRequest",
    "OperatorControlScope",
    "OperatorControlState",
    "VerifiedOperatorIdentity",
]
