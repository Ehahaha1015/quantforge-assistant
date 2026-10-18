"""Synthetic fixtures shared by the CREDENTIAL-001 test suite.

Every value here is synthetic.  No real credential exists anywhere in this
suite; the canary family deliberately fails the frozen opaque grammar so it
can also be routed through rejected input channels.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from quantforge.credentials import (
    CredentialFacts,
    CredentialPurpose,
    CredentialReference,
    CredentialScope,
)
from quantforge.frontier import ExecutionTargetBinding

CANARY_PREFIX = "_CREDENTIAL_FIXTURE_CANARY_"
CANARY = CANARY_PREFIX + "0123456789abcdef0123456789abcdef"
CANARY_BYTES = CANARY.encode("utf-8")
_UNSET = object()
REJECTING_PROVIDER_ID = "rejecting-credential-backend-v1"
PROVIDER_ID = "qf-test-provider-v1"
REQUESTED_AT = datetime(2030, 1, 1, 9, 30, tzinfo=UTC)
EXPIRES_AT = REQUESTED_AT + timedelta(hours=4)
GENERATION = 3


def make_target(
    *,
    broker: str = "broker-main",
    environment: str = "PAPER",
    account_reference: str = "acct-001",
    adapter_id: str = "adapter-a",
    adapter_version: str = "v1",
    submission_id: str = "broker1-" + "0" * 32,
) -> ExecutionTargetBinding:
    return ExecutionTargetBinding(
        schema="frontier-target-v1",
        broker=broker,
        environment=environment,
        account_reference=account_reference,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        submission_id=submission_id,
    )


def make_scope(
    *,
    provider: str = PROVIDER_ID,
    backend: str = "broker-main",
    environment: str = "PAPER",
    account_reference: str = "acct-001",
    consumer: str = "broker-adapter",
    purpose: CredentialPurpose = CredentialPurpose.ORDER_SUBMIT,
) -> CredentialScope:
    return CredentialScope(
        schema="credential-scope-v1",
        provider=provider,
        backend=backend,
        environment=environment,
        account_reference=account_reference,
        consumer=consumer,
        purpose=purpose,
    )


def make_reference(value: str = "deploy-ref-001") -> CredentialReference:
    return CredentialReference(value=value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def package_frames_containing_canary(
    error: BaseException, canary: str = CANARY
) -> list[str]:
    """Return package-owned traceback frames whose locals expose the canary."""
    hits: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get("__name__", "")
        if module.startswith("quantforge.credentials") and canary in repr(
            frame.f_locals
        ):
            hits.append(f"{module}:{frame.f_code.co_name}")
        traceback = traceback.tb_next
    return hits


def canonical_instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def facts_payload(
    scope: CredentialScope,
    reference: CredentialReference,
    *,
    generation: int = GENERATION,
    acquired_at: datetime = REQUESTED_AT,
    expires_at: datetime = EXPIRES_AT,
    provider: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": "credential-facts-v1",
        "provider": scope.provider if provider is None else provider,
        "scope": {
            "provider": scope.provider,
            "backend": scope.backend,
            "environment": scope.environment,
            "account_reference": scope.account_reference,
            "consumer": scope.consumer,
            "purpose": scope.purpose.value,
        },
        "reference": reference.value,
        "generation": generation,
        "acquired_at": canonical_instant(acquired_at),
        "expires_at": canonical_instant(expires_at),
    }


def canonical_facts_id(
    scope: CredentialScope,
    reference: CredentialReference,
    *,
    generation: int = GENERATION,
    acquired_at: datetime = REQUESTED_AT,
    expires_at: datetime = EXPIRES_AT,
    provider: str | None = None,
) -> str:
    payload = facts_payload(
        scope,
        reference,
        generation=generation,
        acquired_at=acquired_at,
        expires_at=expires_at,
        provider=provider,
    )
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"credential-facts-v1-{digest}"


def make_facts(
    scope: CredentialScope | None = None,
    reference: CredentialReference | None = None,
    *,
    generation: int = GENERATION,
    acquired_at: datetime = REQUESTED_AT,
    expires_at: datetime = EXPIRES_AT,
    provider: str | None = None,
    facts_id: str | None = None,
) -> CredentialFacts:
    resolved_scope = make_scope() if scope is None else scope
    resolved_reference = make_reference() if reference is None else reference
    resolved_provider = (
        resolved_scope.provider if provider is None else provider
    )
    resolved_facts_id = (
        canonical_facts_id(
            resolved_scope,
            resolved_reference,
            generation=generation,
            acquired_at=acquired_at,
            expires_at=expires_at,
            provider=resolved_provider,
        )
        if facts_id is None
        else facts_id
    )
    return CredentialFacts(
        schema="credential-facts-v1",
        facts_id=resolved_facts_id,
        provider=resolved_provider,
        scope=resolved_scope,
        reference=resolved_reference,
        generation=generation,
        acquired_at=acquired_at,
        expires_at=expires_at,
    )


class RecordingBackend:
    """Deterministic test-only fake backend with call and read counters."""

    def __init__(
        self,
        *,
        provider_id: str = PROVIDER_ID,
        material: bytes | str = CANARY_BYTES,
        result: Any = _UNSET,
        error: BaseException | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._material = material
        self._result = result
        self._error = error
        self.acquire_calls = 0
        self.provider_id_reads = 0

    @property
    def provider_id(self) -> str:
        self.provider_id_reads += 1
        return self._provider_id

    def acquire(
        self,
        scope: CredentialScope,
        reference: CredentialReference,
        *,
        requested_at: datetime,
    ) -> tuple[CredentialFacts, bytes | str]:
        self.acquire_calls += 1
        if self._error is not None:
            raise self._error
        if self._result is not _UNSET:
            return self._result
        facts = make_facts(
            scope,
            reference,
            acquired_at=requested_at,
            expires_at=requested_at + timedelta(hours=4),
        )
        return (facts, self._material)
