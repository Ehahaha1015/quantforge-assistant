"""Synthetic fixtures shared by the CREDENTIAL-002 backend suite.

Every value here is synthetic.  No real credential exists anywhere in this
suite, the real Windows Credential Manager is never read and no native call
is ever made against an operator vault.  The canary family deliberately
fails the frozen opaque grammar so it can also be routed through rejected
input channels.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from quantforge.credentials import (
    CredentialPurpose,
    CredentialReference,
    CredentialScope,
)
from quantforge.credentials_windows.native import (
    _Credential,
    _CredentialFailureKind,
    _NativeCredentialFailure,
    _NativeCredentialRecord,
)

CANARY_PREFIX = "_CREDENTIAL_FIXTURE_CANARY_"
CANARY = CANARY_PREFIX + "0123456789abcdef0123456789abcdef"
CANARY_BYTES = CANARY.encode("utf-8")
PROVIDER_ID = "windows-credential-manager-v1"
TARGET_PREFIX = "quantforge-crd-v1"
USER_NAME_PREFIX = "quantforge-crd-v1:g"
REQUESTED_AT = datetime(2030, 1, 1, 9, 30, tzinfo=UTC)
LEASE_SECONDS = 900
GENERATION = 3
USER_NAME = USER_NAME_PREFIX + str(GENERATION)
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
BLOB_MINIMUM = 1
BLOB_MAXIMUM = 2560
GUARD_BYTE = 0xA5


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


def compose_target(
    scope: CredentialScope, reference: CredentialReference
) -> str:
    return (
        f"{TARGET_PREFIX}|{scope.provider}|{scope.backend}|"
        f"{scope.environment}|{scope.account_reference}|{scope.consumer}|"
        f"{scope.purpose.value}|{reference.value}"
    )


def make_record(
    scope: CredentialScope | None = None,
    reference: CredentialReference | None = None,
    *,
    generation: int = GENERATION,
    blob: bytes = CANARY_BYTES,
    user_name: str | None = None,
    target_name: str | None = None,
    type_value: int = CRED_TYPE_GENERIC,
    persist: int = CRED_PERSIST_LOCAL_MACHINE,
) -> _NativeCredentialRecord:
    resolved_scope = make_scope() if scope is None else scope
    resolved_reference = make_reference() if reference is None else reference
    resolved_target = (
        compose_target(resolved_scope, resolved_reference)
        if target_name is None
        else target_name
    )
    resolved_user_name = (
        USER_NAME_PREFIX + str(generation)
        if user_name is None
        else user_name
    )
    return _NativeCredentialRecord(
        type_value=type_value,
        target_name=resolved_target,
        persist=persist,
        user_name=resolved_user_name,
        blob=blob,
    )


def make_failure(kind: _CredentialFailureKind) -> _NativeCredentialFailure:
    return _NativeCredentialFailure(kind)


class RecordingSeam:
    """Deterministic test-only native seam with a read counter."""

    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome
        self.read_calls = 0
        self.targets: list[str] = []

    def read(
        self, target_name: str
    ) -> _NativeCredentialRecord | _NativeCredentialFailure:
        self.read_calls += 1
        self.targets.append(target_name)
        return self._outcome


class RecordingFree:
    """Test-only stand-in for the freed allocation call."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, pointer: Any) -> None:
        self.calls += 1


def guarded_blob(data: bytes) -> tuple[Any, ctypes.POINTER(ctypes.c_ubyte)]:
    """Return (buffer, blob pointer) with one guard byte on each side."""
    size = len(data)
    buffer = (ctypes.c_ubyte * (size + 2))()
    buffer[0] = GUARD_BYTE
    buffer[size + 1] = GUARD_BYTE
    for index, value in enumerate(data):
        buffer[index + 1] = value
    pointer = ctypes.cast(
        ctypes.addressof(buffer) + 1,
        ctypes.POINTER(ctypes.c_ubyte),
    )
    return buffer, pointer


def guarded_credential(
    data: bytes,
    *,
    target_name: str,
    user_name: str = USER_NAME,
    type_value: int = CRED_TYPE_GENERIC,
    persist: int = CRED_PERSIST_LOCAL_MACHINE,
    flags: int = 0,
    attribute_count: int = 0,
) -> tuple[_Credential, Any, ctypes.POINTER(_Credential)]:
    """Return (credential, guard buffer, credential pointer)."""
    buffer, blob_pointer = guarded_blob(data)
    credential = _Credential()
    credential.Flags = flags
    credential.Type = type_value
    credential.TargetName = target_name
    credential.Comment = None
    credential.CredentialBlobSize = len(data)
    credential.CredentialBlob = blob_pointer
    credential.Persist = persist
    credential.AttributeCount = attribute_count
    credential.Attributes = None
    credential.TargetAlias = None
    credential.UserName = user_name
    return credential, buffer, ctypes.pointer(credential)


def canonical_instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_facts_id(
    scope: CredentialScope,
    reference: CredentialReference,
    *,
    generation: int = GENERATION,
    acquired_at: datetime = REQUESTED_AT,
    expires_at: datetime | None = None,
) -> str:
    """Independently re-derive the frozen secret-independent identity."""
    resolved_expiry = (
        acquired_at + timedelta(seconds=LEASE_SECONDS)
        if expires_at is None
        else expires_at
    )
    payload = {
        "schema": "credential-facts-v1",
        "provider": PROVIDER_ID,
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
        "expires_at": canonical_instant(resolved_expiry),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "credential-facts-v1-" + digest
