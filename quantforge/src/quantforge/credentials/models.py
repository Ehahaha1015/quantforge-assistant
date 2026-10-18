"""Safe, non-secret credential values and deterministic facts identity.

These models validate caller-supplied declarations.  They hold no secret
material, grant no acquisition authority and never derive identity from a
secret value.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..frontier import ExecutionTargetBinding
from .errors import CredentialArgumentError

_OPAQUE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_FACTS_ID_PATTERN = re.compile(r"credential-facts-v1-[0-9a-f]{64}")
_CANONICAL_INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _utc_instant(value: Any) -> datetime:
    """Return the UTC-normalized instant, or fail closed."""
    normalized: datetime | None = None
    if isinstance(value, datetime) and value.tzinfo is not None:
        try:
            normalized = value.astimezone(UTC)
        except (OverflowError, TypeError, ValueError):
            normalized = None
    if normalized is None:
        raise CredentialArgumentError(
            "credential instant must be a timezone-aware datetime inside the "
            "representable UTC range"
        )
    return normalized


def _canonical_instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_CANONICAL_INSTANT_FORMAT)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _scope_payload(scope: CredentialScope) -> dict[str, str]:
    return {
        "provider": scope.provider,
        "backend": scope.backend,
        "environment": scope.environment,
        "account_reference": scope.account_reference,
        "consumer": scope.consumer,
        "purpose": scope.purpose.value,
    }


def _facts_payload(facts: CredentialFacts) -> dict[str, Any]:
    return {
        "schema": facts.schema,
        "provider": facts.provider,
        "scope": _scope_payload(facts.scope),
        "reference": facts.reference.value,
        "generation": facts.generation,
        "acquired_at": _canonical_instant(facts.acquired_at),
        "expires_at": _canonical_instant(facts.expires_at),
    }


def _compute_facts_id(facts: CredentialFacts) -> str:
    """Re-derive the deterministic facts identity from non-secret fields."""
    canonical = _canonical_json(_facts_payload(facts))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"credential-facts-v1-{digest}"


class CredentialPurpose(StrEnum):
    """Closed capability purpose of one credential acquisition."""

    MARKET_DATA_READ = "MARKET_DATA_READ"
    ACCOUNT_READ = "ACCOUNT_READ"
    ORDER_SUBMIT = "ORDER_SUBMIT"
    ORDER_QUERY = "ORDER_QUERY"
    ORDER_CANCEL = "ORDER_CANCEL"


class _CredentialValue(BaseModel):
    """Strict, frozen, extra-forbidding base for safe credential values."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    _label: ClassVar[str] = "credential value"

    def __init__(self, **data: Any) -> None:
        malformed = False
        try:
            super().__init__(**data)
        except ValidationError:
            malformed = True
        # Drop any rejected input from this frame before failing closed.
        data = {}
        if malformed:
            raise CredentialArgumentError(f"{self._label} is malformed")


class CredentialReference(_CredentialValue):
    """Non-secret opaque identifier naming the credential to obtain.

    ``value`` is the canonical opaque reference string; it is not authority
    by itself and carries no secret-derived component.
    """

    _label = "credential reference"

    value: str

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        if _OPAQUE_PATTERN.fullmatch(value) is None:
            raise ValueError("credential reference is malformed")
        return value


class CredentialScope(_CredentialValue):
    """Exact non-secret context one credential acquisition is bound to."""

    _label = "credential scope"

    schema: Literal["credential-scope-v1"]  # type: ignore[assignment]
    provider: str
    backend: str
    environment: Literal["PAPER", "SANDBOX", "PRODUCTION"]
    account_reference: str
    consumer: str
    purpose: CredentialPurpose

    @field_validator("provider", "backend", "account_reference", "consumer")
    @classmethod
    def _opaque_fields(cls, value: str) -> str:
        if _OPAQUE_PATTERN.fullmatch(value) is None:
            raise ValueError("credential scope field is malformed")
        return value

    @classmethod
    def from_target(
        cls,
        target: ExecutionTargetBinding,
        *,
        provider: str,
        consumer: str,
        purpose: CredentialPurpose,
    ) -> CredentialScope:
        """Bind a broker target's frozen identity into a credential scope."""
        if not isinstance(target, ExecutionTargetBinding):
            raise CredentialArgumentError(
                "credential scope target must be an ExecutionTargetBinding"
            )
        if purpose == CredentialPurpose.MARKET_DATA_READ:
            raise CredentialArgumentError(
                "a broker target cannot authorise market-data reading"
            )
        return cls(
            schema="credential-scope-v1",
            provider=provider,
            backend=target.broker,
            environment=target.environment,
            account_reference=target.account_reference,
            consumer=consumer,
            purpose=purpose,
        )

    def matches(self, target: ExecutionTargetBinding) -> bool:
        """Exact field equality against the frozen target binding."""
        if not isinstance(target, ExecutionTargetBinding):
            return False
        return (
            self.backend == target.broker
            and self.environment == target.environment
            and self.account_reference == target.account_reference
        )


class CredentialFacts(_CredentialValue):
    """Safe, non-secret metadata of exactly one credential acquisition."""

    _label = "credential facts"

    schema: Literal["credential-facts-v1"]  # type: ignore[assignment]
    facts_id: str
    provider: str
    scope: CredentialScope
    reference: CredentialReference
    generation: int
    acquired_at: datetime
    expires_at: datetime

    @field_validator("facts_id")
    @classmethod
    def _validate_facts_id(cls, value: str) -> str:
        if _FACTS_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("credential facts identity is malformed")
        return value

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        if _OPAQUE_PATTERN.fullmatch(value) is None:
            raise ValueError("credential facts provider is malformed")
        return value

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: int) -> int:
        if isinstance(value, bool) or value < 1:
            raise ValueError("credential facts generation is malformed")
        return value

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def _validate_instants(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("credential instant must be timezone-aware")
        return value
