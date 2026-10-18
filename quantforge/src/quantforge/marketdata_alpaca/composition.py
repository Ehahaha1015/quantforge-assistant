"""The single composition site of the frozen MARKETDATA-002 authority.

The composition owns the exact credential scope and reference literals, the
single reveal site, the credential-blob validation and the construction of
the finite production transport and source.  It exports exactly one name.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from quantforge.credentials import (
    CredentialPurpose,
    CredentialReference,
    CredentialScope,
)
from quantforge.credentials_windows import build_credential_boundary

from .source import (
    AlpacaIexLatestTradeSource,
    _create_source,
    _CredentialMaterialMalformed,
)
from .transport import HttpsGet, TransportResponse

_CREDENTIAL_INSTANT = datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)
_PROVIDER = "windows-credential-manager-v1"
_BACKEND = "alpaca"
_ENVIRONMENT: Literal["PRODUCTION"] = "PRODUCTION"
_ACCOUNT_REFERENCE = "alpaca-account-001"
_CONSUMER = "marketdata-alpaca-iex-v1"
_PURPOSE = "MARKET_DATA_READ"
_REFERENCE = "alpaca-iex-key-v1"
_SCOPE_SCHEMA: Literal["credential-scope-v1"] = "credential-scope-v1"
_KEY_ID_MEMBER = "key_id"
_SECRET_KEY_MEMBER = "secret_key"
_CREDENTIAL_MEMBERS = ("key_id", "secret_key")
_MINIMUM_MEMBER_LENGTH = 1
_MAXIMUM_MEMBER_LENGTH = 128
_MINIMUM_BLOB_BYTES = 1
_MAXIMUM_BLOB_BYTES = 2560
_ACCEPT = "application/json"
_ACCEPT_ENCODING = "identity"
_KEY_ID_HEADER = "APCA-API-KEY-ID"
_SECRET_KEY_HEADER = "APCA-API-SECRET-KEY"
_ACCEPT_HEADER = "Accept"
_ACCEPT_ENCODING_HEADER = "Accept-Encoding"
_PRINTABLE_ASCII_MINIMUM = " "
_PRINTABLE_ASCII_MAXIMUM = "~"


class _CredentialSession:
    """One credentialed request per call; the single reveal site."""

    __slots__ = ("_boundary", "_reference", "_scope", "_transport")

    def __init__(
        self,
        *,
        boundary: Any,
        scope: CredentialScope,
        reference: CredentialReference,
        transport: HttpsGet,
    ) -> None:
        self._boundary = boundary
        self._scope = scope
        self._reference = reference
        self._transport = transport

    def get(self, target: str) -> TransportResponse:
        lease = self._boundary.acquire(
            self._scope,
            self._reference,
            requested_at=_CREDENTIAL_INSTANT,
        )
        try:
            material = lease.reveal(evaluation_time=_CREDENTIAL_INSTANT)
            headers = _request_headers(material.as_bytes())
            return self._transport.get(target, headers)
        finally:
            lease.discard()


def _build_source(
    *, boundary: Any, transport: HttpsGet
) -> AlpacaIexLatestTradeSource:
    scope = CredentialScope(
        schema=_SCOPE_SCHEMA,
        provider=_PROVIDER,
        backend=_BACKEND,
        environment=_ENVIRONMENT,
        account_reference=_ACCOUNT_REFERENCE,
        consumer=_CONSUMER,
        purpose=CredentialPurpose(_PURPOSE),
    )
    reference = CredentialReference(value=_REFERENCE)
    session = _CredentialSession(
        boundary=boundary,
        scope=scope,
        reference=reference,
        transport=transport,
    )
    return _create_source(fetch=session.get)


def build_alpaca_iex_source() -> AlpacaIexLatestTradeSource:
    """Return a fresh finite source over the production credential path."""
    return _build_source(
        boundary=build_credential_boundary(),
        transport=HttpsGet(),
    )


def _request_headers(raw: object) -> dict[str, str]:
    failure: Exception | None = None
    headers: dict[str, str] | None = None
    try:
        document = _credential_document(raw)
        headers = {
            _KEY_ID_HEADER: document[_KEY_ID_MEMBER],
            _SECRET_KEY_HEADER: document[_SECRET_KEY_MEMBER],
            _ACCEPT_HEADER: _ACCEPT,
            _ACCEPT_ENCODING_HEADER: _ACCEPT_ENCODING,
        }
    except _CredentialMaterialMalformed:
        raise
    except Exception:  # noqa: BLE001 - frozen deferred credential mapping
        failure = _CredentialMaterialMalformed()
    if failure is not None:
        raise failure
    if headers is None:
        raise _CredentialMaterialMalformed()
    return headers


def _credential_document(raw: object) -> dict[str, str]:
    if not isinstance(raw, bytes):
        raise _CredentialMaterialMalformed()
    if len(raw) < _MINIMUM_BLOB_BYTES or len(raw) > _MAXIMUM_BLOB_BYTES:
        raise _CredentialMaterialMalformed()
    parsed = json.loads(
        raw.decode("utf-8"),
        parse_constant=_reject_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )
    if not isinstance(parsed, dict):
        raise _CredentialMaterialMalformed()
    if set(parsed) != set(_CREDENTIAL_MEMBERS):
        raise _CredentialMaterialMalformed()
    document: dict[str, str] = {}
    for member in _CREDENTIAL_MEMBERS:
        value = parsed[member]
        if not isinstance(value, str) or not _printable_ascii(value):
            raise _CredentialMaterialMalformed()
        document[member] = value
    return document


def _printable_ascii(value: str) -> bool:
    if len(value) < _MINIMUM_MEMBER_LENGTH:
        return False
    if len(value) > _MAXIMUM_MEMBER_LENGTH:
        return False
    return all(
        _PRINTABLE_ASCII_MINIMUM <= character <= _PRINTABLE_ASCII_MAXIMUM
        for character in value
    )


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _CredentialMaterialMalformed()
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    raise _CredentialMaterialMalformed()
