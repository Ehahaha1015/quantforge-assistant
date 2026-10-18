"""Synthetic acquisition, reveal, discard and sanitized credential failures."""

from __future__ import annotations

import pytest
from _marketdata_alpaca_fixtures import (
    CANARY,
    PROVIDER_ID,
    REQUESTED_AT,
    RecordingTransport,
    SyntheticBackend,
    make_blob,
    make_body,
    make_facts,
    make_headers,
    make_reference,
    make_request,
    make_response,
    make_scope,
)

from quantforge.credentials import (
    CredentialBoundary,
    CredentialProviderError,
    CredentialProviderIdentityError,
    CredentialScopeMismatchError,
    CredentialUnavailableError,
)
from quantforge.marketdata import PriceCoverageError, PriceSourceError
from quantforge.marketdata_alpaca.composition import _build_source


def _source(material: bytes = make_blob()):
    backend = SyntheticBackend(material=material)
    boundary = CredentialBoundary(backend=backend)
    body = make_body(("AAPL",))
    transport = RecordingTransport(
        response=make_response(body, content_length=str(len(body)))
    )
    return _build_source(boundary=boundary, transport=transport), backend, transport


def test_scope_reference_instant_and_one_acquire_one_request() -> None:
    backend = SyntheticBackend(material=make_blob())

    class CountingBoundary(CredentialBoundary):
        def __init__(self) -> None:
            super().__init__(backend=backend)
            self.calls = []
            self.lease = None

        def acquire(self, scope, reference, *, requested_at):
            self.calls.append((scope, reference, requested_at))
            lease = super().acquire(
                scope, reference, requested_at=requested_at
            )
            self.lease = lease
            return lease

    boundary = CountingBoundary()
    body = make_body(("AAPL",))
    transport = RecordingTransport(
        response=make_response(body, content_length=str(len(body)))
    )
    observation = _build_source(
        boundary=boundary, transport=transport
    ).observe(make_request(("AAPL",)))
    assert len(boundary.calls) == backend.acquire_calls == len(transport.calls) == 1
    assert boundary.calls[0] == (
        make_scope(),
        make_reference(),
        REQUESTED_AT,
    )
    assert transport.calls == [
        (
            "/v2/stocks/trades/latest?symbols=AAPL&feed=iex",
            make_headers(),
        )
    ]
    assert boundary.lease is not None
    with pytest.raises(Exception, match="discarded"):
        boundary.lease.reveal(evaluation_time=REQUESTED_AT)
    assert CANARY not in repr(observation)
    assert CANARY not in observation.model_dump_json()


@pytest.mark.parametrize(
    "blob",
    (
        b"\xff",
        b"{}",
        b'{"key_id":"x","secret_key":"y","extra":1}',
        b'{"key_id":"x","secret_key":"y","secret_key":"z"}',
        b'{"key_id":42,"secret_key":"x"}',
        b'{"key_id":"x","secret_key":"\\u0000"}',
        b'{"key_id":"x","secret_key":"y"}' + b" " * 2560,
        b"[]",
        b'{"key_id":"x","secret_key":""}',
    ),
    ids=(
        "invalid_utf8",
        "missing_members",
        "extra_member",
        "duplicate_member",
        "non_string_member",
        "control_character",
        "overlong_blob",
        "non_object",
        "empty_member",
    ),
)
def test_bad_blob_sanitized_and_no_request(blob: bytes) -> None:
    source, backend, transport = _source(blob)
    with pytest.raises(PriceSourceError) as info:
        source.observe(make_request(("AAPL",)))
    assert str(info.value) == "price observation credential is malformed"
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert backend.acquire_calls == 1
    assert transport.calls == []


@pytest.mark.parametrize(
    "symbols",
    (("MSFT", "AAPL"), ("AAPL", "AAPL"), ("QQQ",)),
)
def test_request_rejection_precedes_acquisition(symbols) -> None:
    source, backend, transport = _source()
    request = type(make_request(("AAPL",))).model_construct(
        schema="marketdata-request-v1", symbols=symbols
    )
    with pytest.raises(PriceCoverageError):
        source.observe(request)
    assert backend.acquire_calls == 0
    assert transport.calls == []


@pytest.mark.parametrize(
    "error",
    (
        CredentialUnavailableError("credential store is unavailable"),
        CredentialProviderError("synthetic credential backend failed"),
    ),
    ids=("unavailable", "provider_error"),
)
def test_upstream_credential_errors_propagate_unchanged(error) -> None:
    class ErrorBackend(SyntheticBackend):
        def acquire(self, scope, reference, *, requested_at):
            self.acquire_calls += 1
            raise error

    backend = ErrorBackend(material=make_blob())
    transport = RecordingTransport()
    source = _build_source(
        boundary=CredentialBoundary(backend=backend), transport=transport
    )
    with pytest.raises(type(error)) as info:
        source.observe(make_request(("AAPL",)))
    assert info.value is error
    assert backend.acquire_calls == 1
    assert transport.calls == []


def test_wrong_provider_id_rejected_before_backend_read() -> None:
    backend = SyntheticBackend(
        material=make_blob(), provider_id="wrong-provider"
    )
    source = _build_source(
        boundary=CredentialBoundary(backend=backend),
        transport=RecordingTransport(),
    )
    with pytest.raises(CredentialProviderIdentityError):
        source.observe(make_request(("AAPL",)))
    assert backend.acquire_calls == 0


def test_wrong_facts_scope_and_reference_rejected_before_request() -> None:
    class BadFactsBackend(SyntheticBackend):
        def __init__(self, change):
            super().__init__(material=make_blob())
            self.change = change

        def acquire(self, scope, reference, *, requested_at):
            self.acquire_calls += 1
            from quantforge.credentials import CredentialReference, CredentialScope

            if self.change == "reference":
                reference = CredentialReference(value="wrong-ref")
            else:
                scope = CredentialScope(
                    schema="credential-scope-v1",
                    provider=PROVIDER_ID,
                    backend="wrong",
                    environment="PRODUCTION",
                    account_reference="alpaca-account-001",
                    consumer="marketdata-alpaca-iex-v1",
                    purpose=scope.purpose,
                )
            return (
                make_facts(scope, reference, requested_at=requested_at),
                make_blob(),
            )

    for change in ("scope", "reference"):
        backend = BadFactsBackend(change)
        transport = RecordingTransport()
        source = _build_source(
            boundary=CredentialBoundary(backend=backend),
            transport=transport,
        )
        expected = (
            CredentialScopeMismatchError
            if change == "scope"
            else CredentialProviderError
        )
        with pytest.raises(expected):
            source.observe(make_request(("AAPL",)))
        assert backend.acquire_calls == 1
        assert transport.calls == []
