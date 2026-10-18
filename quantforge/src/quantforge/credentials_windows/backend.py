"""Concrete Windows Credential Manager backend for CREDENTIAL-002.

The backend validates the caller's scope and reference, composes the exact
frozen vault address, performs exactly one native read through the injected
seam and returns the frozen safe facts plus the raw secret bytes for the
CREDENTIAL-001 boundary to wrap.  It never caches, retries, falls back,
logs or constructs secret-bearing objects.  See ``tasks/CREDENTIAL-002.md``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Final

from quantforge.credentials import (
    CredentialArgumentError,
    CredentialFacts,
    CredentialProviderError,
    CredentialPurpose,
    CredentialReference,
    CredentialScope,
    CredentialUnavailableError,
)

from .native import (
    CredentialReadSeam,
    _CredentialFailureKind,
    _generation_from_user_name,
    _NativeCredentialFailure,
    _NativeCredentialRecord,
)

_PROVIDER_ID: Final = "windows-credential-manager-v1"
_SCHEMA: Final = "credential-facts-v1"
_TARGET_PREFIX: Final = "quantforge-crd-v1"
_TARGET_SEPARATOR: Final = "|"
_OPAQUE_TAIL: Final = "._:/-"
_OPAQUE_MINIMUM: Final = 1
_OPAQUE_MAXIMUM: Final = 128
_COMPOSED_LIMIT: Final = 1024
_LEASE_SECONDS: Final = 900
_INSTANT_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"
_FACTS_ID_PREFIX: Final = "credential-facts-v1-"
_ENVIRONMENTS: Final = ("PAPER", "SANDBOX", "PRODUCTION")
_PURPOSES: Final = (
    "MARKET_DATA_READ",
    "ACCOUNT_READ",
    "ORDER_SUBMIT",
    "ORDER_QUERY",
    "ORDER_CANCEL",
)
_TYPE_MESSAGE: Final = (
    "credential backend requires exact scope, reference and datetime values"
)
_ADDRESS_MESSAGE: Final = "credential store address is malformed"
_MISSING_MESSAGE: Final = "credential store entry is missing"
_IDENTITY_MESSAGE: Final = (
    "credential store entry is not available to this logon identity"
)
_UNAVAILABLE_MESSAGE: Final = "credential store is unavailable"
_MALFORMED_MESSAGE: Final = "credential store entry is malformed"
_READ_FAILURE_MESSAGE: Final = "credential store read failed"
_BACKEND_TEXT: Final = "<WindowsCredentialManagerBackend redacted>"


def _is_letter(character: str) -> bool:
    """Return True for exactly the ASCII letters of the frozen grammar."""
    return "A" <= character <= "Z" or "a" <= character <= "z"


def _is_digit(character: str) -> bool:
    """Return True for exactly the ASCII digits of the frozen grammar."""
    return "0" <= character <= "9"


def _is_opaque(value: object) -> bool:
    """Match the frozen opaque grammar exactly, without any Unicode help."""
    if not isinstance(value, str):
        return False
    if len(value) < _OPAQUE_MINIMUM or len(value) > _OPAQUE_MAXIMUM:
        return False
    first = value[0]
    if not (_is_letter(first) or _is_digit(first)):
        return False
    for character in value[1:]:
        if _is_letter(character) or _is_digit(character):
            continue
        if character not in _OPAQUE_TAIL:
            return False
    return True


def _validated_components(
    scope: CredentialScope, reference: CredentialReference
) -> None:
    """Reject any malformed component before any native access."""
    for component in (
        scope.provider,
        scope.backend,
        scope.account_reference,
        scope.consumer,
        reference.value,
    ):
        if not _is_opaque(component):
            raise CredentialArgumentError(_ADDRESS_MESSAGE)
    if scope.environment not in _ENVIRONMENTS:
        raise CredentialArgumentError(_ADDRESS_MESSAGE)
    if not isinstance(scope.purpose, CredentialPurpose):
        raise CredentialArgumentError(_ADDRESS_MESSAGE)
    if scope.purpose.value not in _PURPOSES:
        raise CredentialArgumentError(_ADDRESS_MESSAGE)


def _compose_target(
    scope: CredentialScope, reference: CredentialReference
) -> str:
    """Compose the exact frozen address from the validated components."""
    return _TARGET_SEPARATOR.join(
        (
            _TARGET_PREFIX,
            scope.provider,
            scope.backend,
            scope.environment,
            scope.account_reference,
            scope.consumer,
            scope.purpose.value,
            reference.value,
        )
    )


def _raise_for_failure(failure: _NativeCredentialFailure) -> None:
    """Map one internal failure kind to the frozen sanitized error."""
    kind = failure.kind
    if kind is _CredentialFailureKind.MISSING:
        raise CredentialUnavailableError(_MISSING_MESSAGE)
    if kind is _CredentialFailureKind.ACCESS_DENIED:
        raise CredentialUnavailableError(_IDENTITY_MESSAGE)
    if kind is _CredentialFailureKind.STORE_UNAVAILABLE:
        raise CredentialUnavailableError(_UNAVAILABLE_MESSAGE)
    if kind is _CredentialFailureKind.MALFORMED:
        raise CredentialProviderError(_MALFORMED_MESSAGE)
    raise CredentialProviderError(_READ_FAILURE_MESSAGE)


def _canonical_instant(value: datetime) -> str:
    """Render one instant exactly as the frozen canonical payload does."""
    return value.astimezone(UTC).strftime(_INSTANT_FORMAT)


def _facts_identity(
    scope: CredentialScope,
    reference: CredentialReference,
    generation: int,
    acquired_at: datetime,
    expires_at: datetime,
) -> str:
    """Re-derive the frozen secret-independent facts identity."""
    payload = {
        "schema": _SCHEMA,
        "provider": _PROVIDER_ID,
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
        "acquired_at": _canonical_instant(acquired_at),
        "expires_at": _canonical_instant(expires_at),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return _FACTS_ID_PREFIX + digest


class WindowsCredentialManagerBackend:
    """The first approved concrete production credential backend."""

    __slots__ = ("_seam",)

    def __init__(self, *, seam: CredentialReadSeam) -> None:
        self._seam = seam

    @property
    def provider_id(self) -> str:
        """The frozen credential-system provider identity."""
        return _PROVIDER_ID

    def acquire(
        self,
        scope: CredentialScope,
        reference: CredentialReference,
        *,
        requested_at: datetime,
    ) -> tuple[CredentialFacts, bytes]:
        """Run the frozen B0..B12 acquisition sequence exactly once."""
        if (
            type(scope) is not CredentialScope
            or type(reference) is not CredentialReference
            or not isinstance(requested_at, datetime)
        ):
            raise TypeError(_TYPE_MESSAGE)
        if requested_at.utcoffset() != timedelta(0):
            raise CredentialArgumentError(_ADDRESS_MESSAGE)
        _validated_components(scope, reference)
        target = _compose_target(scope, reference)
        if len(target) > _COMPOSED_LIMIT:
            raise CredentialArgumentError(_ADDRESS_MESSAGE)
        outcome = self._seam.read(target)
        if isinstance(outcome, _NativeCredentialFailure):
            _raise_for_failure(outcome)
        if not isinstance(outcome, _NativeCredentialRecord):
            raise CredentialProviderError(_READ_FAILURE_MESSAGE)
        if outcome.target_name != target:
            raise CredentialProviderError(_MALFORMED_MESSAGE)
        generation = _generation_from_user_name(outcome.user_name)
        if generation is None:
            raise CredentialProviderError(_MALFORMED_MESSAGE)
        expires_at = requested_at + timedelta(seconds=_LEASE_SECONDS)
        facts_id = _facts_identity(
            scope, reference, generation, requested_at, expires_at
        )
        facts = CredentialFacts(
            schema=_SCHEMA,
            facts_id=facts_id,
            provider=_PROVIDER_ID,
            scope=scope,
            reference=reference,
            generation=generation,
            acquired_at=requested_at,
            expires_at=expires_at,
        )
        return (facts, outcome.blob)

    def __repr__(self) -> str:
        return _BACKEND_TEXT

    def __str__(self) -> str:
        return _BACKEND_TEXT
