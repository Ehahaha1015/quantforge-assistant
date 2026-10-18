"""Synthetic fixtures for the MARKETDATA-002 suite.

Every value here is synthetic.  No real network call, no real credential and
no Windows Credential Manager access happens anywhere in this suite; the
canary family is used to prove that the revealed credential material never
reaches a durable artifact.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from quantforge.credentials import (
    CredentialFacts,
    CredentialPurpose,
    CredentialReference,
    CredentialScope,
)
from quantforge.credentials.boundary import CredentialBoundary
from quantforge.marketdata import PriceObservationRequest
from quantforge.marketdata_alpaca.transport import TransportResponse

CANARY = "_MARKETDATA_FIXTURE_CANARY_" + "0123456789abcdef0123456789abcdef"
KEY_ID = "synthetic-key-id-001"
SECRET_KEY = CANARY
PROVIDER_ID = "windows-credential-manager-v1"
SCOPE_BACKEND = "alpaca"
SCOPE_ENVIRONMENT = "PRODUCTION"
SCOPE_ACCOUNT_REFERENCE = "alpaca-account-001"
SCOPE_CONSUMER = "marketdata-alpaca-iex-v1"
SCOPE_REFERENCE = "alpaca-iex-key-v1"
LEASE_SECONDS = 900
GENERATION = 1
REQUESTED_AT = datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)

SYMBOLS = ("AAPL", "MSFT", "SPY")
SUBSETS = (
    ("AAPL",),
    ("MSFT",),
    ("SPY",),
    ("AAPL", "MSFT"),
    ("AAPL", "SPY"),
    ("MSFT", "SPY"),
    ("AAPL", "MSFT", "SPY"),
)
TIMESTAMPS = {
    "AAPL": "2026-09-28T14:03:21.123456789Z",
    "MSFT": "2026-09-28T14:03:21.223456789Z",
    "SPY": "2026-09-28T14:03:21.323456789Z",
}
PRICES = {
    "AAPL": 178.26,
    "MSFT": 512.34,
    "SPY": 641.02,
}
SIZES = {"AAPL": 246, "MSFT": 100, "SPY": 172}


def make_scope() -> CredentialScope:
    return CredentialScope(
        schema="credential-scope-v1",
        provider=PROVIDER_ID,
        backend=SCOPE_BACKEND,
        environment=SCOPE_ENVIRONMENT,
        account_reference=SCOPE_ACCOUNT_REFERENCE,
        consumer=SCOPE_CONSUMER,
        purpose=CredentialPurpose.MARKET_DATA_READ,
    )


def make_reference() -> CredentialReference:
    return CredentialReference(value=SCOPE_REFERENCE)


def make_blob(
    *, key_id: str = KEY_ID, secret_key: str = SECRET_KEY
) -> bytes:
    return json.dumps(
        {"key_id": key_id, "secret_key": secret_key},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make_facts(
    scope: CredentialScope,
    reference: CredentialReference,
    *,
    requested_at: datetime = REQUESTED_AT,
) -> CredentialFacts:
    """Independently re-derive the frozen secret-independent facts identity."""
    expires_at = requested_at + timedelta(seconds=LEASE_SECONDS)
    payload = {
        "schema": "credential-facts-v1",
        "provider": scope.provider,
        "scope": {
            "provider": scope.provider,
            "backend": scope.backend,
            "environment": scope.environment,
            "account_reference": scope.account_reference,
            "consumer": scope.consumer,
            "purpose": scope.purpose.value,
        },
        "reference": reference.value,
        "generation": GENERATION,
        "acquired_at": canonical_instant(requested_at),
        "expires_at": canonical_instant(expires_at),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return CredentialFacts(
        schema="credential-facts-v1",
        facts_id="credential-facts-v1-" + digest,
        provider=scope.provider,
        scope=scope,
        reference=reference,
        generation=GENERATION,
        acquired_at=requested_at,
        expires_at=expires_at,
    )


class SyntheticBackend:
    """A deterministic test-only credential backend over synthetic bytes."""

    def __init__(self, *, material: bytes, provider_id: str = PROVIDER_ID) -> None:
        self._material = material
        self._provider_id = provider_id
        self.acquire_calls = 0

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def acquire(
        self,
        scope: CredentialScope,
        reference: CredentialReference,
        *,
        requested_at: datetime,
    ) -> tuple[CredentialFacts, bytes]:
        self.acquire_calls += 1
        facts = make_facts(scope, reference, requested_at=requested_at)
        return (facts, self._material)


def make_boundary(
    *, material: bytes | None = None
) -> CredentialBoundary:
    backend = SyntheticBackend(
        material=make_blob() if material is None else material
    )
    return CredentialBoundary(backend=backend)


def make_trade(
    symbol: str,
    *,
    price: Any = None,
    trade_id: int = 1,
    timestamp: str | None = None,
    update: Any = None,
) -> dict[str, Any]:
    trade: dict[str, Any] = {
        "t": TIMESTAMPS[symbol] if timestamp is None else timestamp,
        "i": trade_id,
        "x": "V",
        "p": PRICES[symbol] if price is None else price,
        "s": SIZES[symbol],
        "c": ["@"],
        "z": "C",
    }
    if update is not None:
        trade["u"] = update
    return trade


def make_payload(
    symbols: tuple[str, ...],
    *,
    trades: dict[str, Any] | None = None,
    currency: Any = None,
) -> dict[str, Any]:
    resolved = (
        {symbol: make_trade(symbol) for symbol in symbols}
        if trades is None
        else trades
    )
    payload: dict[str, Any] = {"trades": resolved}
    if currency is not None:
        payload["currency"] = currency
    return payload


def make_body(
    symbols: tuple[str, ...],
    *,
    trades: dict[str, Any] | None = None,
    currency: Any = None,
) -> bytes:
    payload = make_payload(symbols, trades=trades, currency=currency)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def make_request(symbols: tuple[str, ...]) -> PriceObservationRequest:
    ordered = tuple(sorted(symbols, key=lambda item: item.encode("utf-8")))
    return PriceObservationRequest(
        schema="marketdata-request-v1", symbols=ordered
    )


def make_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str | None = "application/json",
    content_encoding: str | None = None,
    content_length: str | None = None,
    transfer_encoding: str | None = None,
) -> TransportResponse:
    return TransportResponse(
        status=status,
        content_type=content_type,
        content_encoding=content_encoding,
        content_length=content_length,
        transfer_encoding=transfer_encoding,
        body=body,
    )


class RecordingFetch:
    """Deterministic fetch seam double with a call counter."""

    def __init__(
        self,
        *,
        response: TransportResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.calls: list[str] = []

    def __call__(self, target: str) -> TransportResponse:
        self.calls.append(target)
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise AssertionError("no synthetic response configured")
        return self._response


class RecordingTransport:
    """Deterministic transport double for the composition seam."""

    def __init__(
        self,
        *,
        response: TransportResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self, target: str, headers: dict[str, str]
    ) -> TransportResponse:
        self.calls.append((target, dict(headers)))
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise AssertionError("no synthetic response configured")
        return self._response


class FakeResponse:
    """A deterministic stand-in for http.client.HTTPResponse."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
    ) -> None:
        self.status = status
        self._headers = headers
        self._body = body
        self.read_amounts: list[int | None] = []

    def getheader(self, name: str, default: str | None = None) -> str | None:
        values = [
            value
            for header, value in self._headers
            if header.lower() == name.lower()
        ]
        if not values:
            return default
        return ", ".join(values)

    def read(self, amount: int | None = None) -> bytes:
        self.read_amounts.append(amount)
        if amount is None:
            return self._body
        return self._body[:amount]


class FakeConnection:
    """A deterministic stand-in for http.client.HTTPSConnection."""

    instances: ClassVar[list[FakeConnection]] = []

    def __init__(
        self,
        *,
        host: str,
        port: int,
        timeout: float,
        context: object,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.requests: list[tuple[Any, ...]] = []
        self.headers: list[tuple[str, str]] = []
        self.ended = 0
        self.responses = 0
        self.closed = 0
        self.response: FakeResponse | None = None
        self.raise_on: str | None = None
        self.close_error: BaseException | None = None
        FakeConnection.instances.append(self)

    def putrequest(
        self,
        method: str,
        target: str,
        *,
        skip_host: bool,
        skip_accept_encoding: bool,
    ) -> None:
        self.requests.append(
            (method, target, skip_host, skip_accept_encoding)
        )
        if self.raise_on == "putrequest":
            raise OSError("synthetic putrequest failure")

    def putheader(self, name: str, value: str) -> None:
        self.headers.append((name, value))
        if self.raise_on == "putheader":
            raise OSError("synthetic putheader failure")

    def endheaders(self) -> None:
        self.ended += 1
        if self.raise_on == "endheaders":
            raise OSError("synthetic endheaders failure")

    def getresponse(self) -> FakeResponse:
        self.responses += 1
        if self.raise_on == "getresponse":
            raise OSError("synthetic getresponse failure")
        if self.response is None:
            raise AssertionError("no synthetic response configured")
        return self.response

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def configure_connection(
    monkeypatch: Any,
    *,
    response: FakeResponse,
    raise_on: str | None = None,
    close_error: BaseException | None = None,
) -> type[FakeConnection]:
    """Replace the stdlib connection class with the deterministic fake."""
    FakeConnection.instances = []

    def _factory(
        *, host: str, port: int, timeout: float, context: object
    ) -> FakeConnection:
        connection = FakeConnection(
            host=host, port=port, timeout=timeout, context=context
        )
        connection.response = response
        connection.raise_on = raise_on
        connection.close_error = close_error
        return connection

    import quantforge.marketdata_alpaca.transport as transport_module

    monkeypatch.setattr(
        transport_module.http.client, "HTTPSConnection", _factory
    )
    return FakeConnection


def make_connection_headers(
    *,
    content_type: str | None = "application/json",
    content_length: str | None = None,
    transfer_encoding: str | None = None,
    content_encoding: str | None = None,
) -> tuple[tuple[str, str], ...]:
    headers: list[tuple[str, str]] = []
    if content_type is not None:
        headers.append(("Content-Type", content_type))
    if content_length is not None:
        headers.append(("Content-Length", content_length))
    if transfer_encoding is not None:
        headers.append(("Transfer-Encoding", transfer_encoding))
    if content_encoding is not None:
        headers.append(("Content-Encoding", content_encoding))
    return tuple(headers)


def make_headers(
    *,
    key_id: str = KEY_ID,
    secret_key: str = SECRET_KEY,
) -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret_key,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
