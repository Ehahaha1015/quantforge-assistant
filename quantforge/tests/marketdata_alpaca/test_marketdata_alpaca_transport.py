"""Synthetic HTTPS request and response authority oracles."""

from __future__ import annotations

import pytest
from _marketdata_alpaca_fixtures import (
    SUBSETS,
    FakeResponse,
    configure_connection,
    make_connection_headers,
    make_headers,
)

from quantforge.marketdata_alpaca import transport


def _get(monkeypatch, *, status=200, headers=None, body=b"{}", **options):
    response = FakeResponse(
        status=status,
        headers=(
            make_connection_headers(content_length=str(len(body)))
            if headers is None else headers
        ),
        body=body,
    )
    fake = configure_connection(monkeypatch, response=response, **options)
    return response, fake


def test_seven_targets_exact_tls_request_and_close(monkeypatch) -> None:
    context = object()
    monkeypatch.setattr(transport.ssl, "create_default_context", lambda: context)
    response, fake = _get(monkeypatch)
    for symbols in SUBSETS:
        target = "/v2/stocks/trades/latest?symbols=" + ",".join(symbols) + "&feed=iex"
        result = transport.HttpsGet().get(target, make_headers())
        assert result.body == b"{}"
        connection = fake.instances[-1]
        assert (connection.host, connection.port, connection.timeout, connection.context) == (
            "data.alpaca.markets", 443, 5.0, context
        )
        assert connection.requests == [("GET", target, False, True)]
        assert connection.headers == list(make_headers().items())
        assert (connection.ended, connection.responses, connection.closed) == (1, 1, 1)
    assert len(fake.instances) == 7
    assert response.read_amounts == [65537] * 7


@pytest.mark.parametrize("target", (
    "https://data.alpaca.markets/v2/stocks/trades/latest?symbols=AAPL&feed=iex",
    "/v2/stocks/trades/latest?symbols=AAPL&feed=sip",
    "/v2/stocks/trades/latest?symbols=QQQ&feed=iex",
    "/v2/orders?symbols=AAPL&feed=iex",
    "//other.example/v2/stocks/trades/latest?symbols=AAPL&feed=iex",
    "http://data.alpaca.markets:80/v2/stocks/trades/latest?symbols=AAPL&feed=iex",
))
def test_target_rejected_before_connection(monkeypatch, target) -> None:
    _, fake = _get(monkeypatch)
    with pytest.raises(transport._InvalidTransportRequest):
        transport.HttpsGet().get(target, make_headers())
    assert fake.instances == []


@pytest.mark.parametrize("mutation", (
    {"Accept": "text/plain"}, {"Accept-Encoding": "gzip"},
    {"APCA-API-KEY-ID": "abc\r\nX: bad"},
    {"APCA-API-SECRET-KEY": "bad\x00value"},
    {"X-Extra": "bad"},
))
def test_header_mutations_rejected_before_connection(monkeypatch, mutation) -> None:
    _, fake = _get(monkeypatch)
    headers = make_headers()
    headers.update(mutation)
    with pytest.raises(transport._InvalidTransportRequest):
        transport.HttpsGet().get(
            "/v2/stocks/trades/latest?symbols=AAPL&feed=iex", headers
        )
    assert fake.instances == []


@pytest.mark.parametrize(("status", "signal"), (
    (301, transport._ResponseMalformed), (307, transport._ResponseMalformed),
    (401, transport._SourceRejected), (403, transport._SourceRejected),
    (429, transport._SourceRateLimited), (404, transport._SourceUnavailable),
    (500, transport._SourceUnavailable),
))
def test_status_is_mapped_before_headers_or_body(monkeypatch, status, signal) -> None:
    response, fake = _get(monkeypatch, status=status, headers=(("Location", "https://elsewhere"),))
    with pytest.raises(signal):
        transport.HttpsGet().get(
            "/v2/stocks/trades/latest?symbols=AAPL&feed=iex", make_headers()
        )
    assert response.read_amounts == []
    assert len(fake.instances) == 1
    assert fake.instances[0].closed == 1
    assert fake.instances[0].requests[0][0] == "GET"


@pytest.mark.parametrize("headers", (
    (), (("Content-Type", "text/plain"), ("Content-Length", "2")),
    (("Content-Type", "application/json; charset=latin1"), ("Content-Length", "2")),
    (("Content-Type", "application/json, application/json"), ("Content-Length", "2")),
    (("Content-Type", "application/json"), ("Content-Encoding", "gzip"), ("Content-Length", "2")),
    (("Content-Type", "application/json"), ("Content-Encoding", "identity"), ("Content-Encoding", "identity"), ("Content-Length", "2")),
    (("Content-Type", "application/json"), ("Content-Type", "application/json"), ("Content-Length", "2")),
    (("Content-Type", "application/json"), ("Transfer-Encoding", "gzip")),
    (("Content-Type", "application/json"),),
    (("Content-Type", "application/json"), ("Content-Length", "2"), ("Transfer-Encoding", "chunked")),
    (("Content-Type", "application/json"), ("Content-Length", "2"), ("Content-Length", "2")),
    (("Content-Type", "application/json"), ("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")),
    (("Content-Type", "application/json"), ("Content-Length", "02")),
    (("Content-Type", "application/json"), ("Content-Length", "65537")),
    (("Content-Type", "application/json"), ("Content-Length", "999999")),
))
def test_malformed_headers_fail_before_read(monkeypatch, headers) -> None:
    response, fake = _get(monkeypatch, headers=headers)
    with pytest.raises(transport._ResponseMalformed):
        transport.HttpsGet().get(
            "/v2/stocks/trades/latest?symbols=AAPL&feed=iex", make_headers()
        )
    assert response.read_amounts == []
    assert fake.instances[0].closed == 1


@pytest.mark.parametrize(("headers", "body", "signal"), (
    (make_connection_headers(content_length="65536"), b"x" * 65536, None),
    (make_connection_headers(content_length="65537"), b"x" * 65537, transport._ResponseMalformed),
    (make_connection_headers(transfer_encoding="chunked"), b"x" * 65536, None),
    (make_connection_headers(transfer_encoding="chunked"), b"x" * 65537, transport._ResponseMalformed),
    (make_connection_headers(content_length="3"), b"xx", transport._TransportUnavailable),
    (make_connection_headers(content_length="0"), b"", None),
    (make_connection_headers(content_type="APPLICATION/JSON; CHARSET=UTF-8", content_length="2", content_encoding="IDENTITY"), b"{}", None),
), ids=("cl_exact", "cl_oversize", "chunk_exact", "chunk_oversize", "early_eof", "empty", "casefold"))
def test_body_bound_framing_and_early_eof(monkeypatch, headers, body, signal) -> None:
    response, fake = _get(monkeypatch, headers=headers, body=body)
    target = "/v2/stocks/trades/latest?symbols=AAPL&feed=iex"
    if signal is None:
        assert transport.HttpsGet().get(target, make_headers()).body == body
    else:
        with pytest.raises(signal):
            transport.HttpsGet().get(target, make_headers())
    assert response.read_amounts == ([] if headers[1][1] == "65537" else [65537])
    assert fake.instances[0].closed == 1


@pytest.mark.parametrize("raise_on", ("putrequest", "putheader", "endheaders", "getresponse"))
def test_failure_closes_once_without_retry(monkeypatch, raise_on) -> None:
    _, fake = _get(monkeypatch, raise_on=raise_on, close_error=OSError("synthetic close"))
    with pytest.raises(transport._TransportUnavailable) as info:
        transport.HttpsGet().get(
            "/v2/stocks/trades/latest?symbols=AAPL&feed=iex", make_headers()
        )
    assert info.value.__cause__ is None
    assert len(fake.instances) == 1
    assert fake.instances[0].closed == 1
