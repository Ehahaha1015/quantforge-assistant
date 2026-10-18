"""Finite stdlib HTTPS transport for the frozen MARKETDATA-002 authority.

One call performs exactly one authenticated GET against the single frozen
Alpaca host and returns one bounded, validated response.  Every network,
framing, content-type and status rule of the frozen contract is enforced
here.  The module raises private internal signals that ``source.py`` maps to
the frozen public errors; no public error type is defined or exported here.

The four inspected response headers are read through ``HTTPResponse.
getheader``, the only response reader the frozen rule R8 permits together
with ``status`` and ``read``.  A single legitimate value of those four
headers can never contain a comma, so a comma-joined duplicate field line
can never satisfy the frozen value grammars and always fails closed, which
is the frozen multiplicity outcome.
"""

from __future__ import annotations

import http.client
import ssl
from collections.abc import Mapping

_HOST = "data.alpaca.markets"
_PORT = 443
_METHOD = "GET"
_TIMEOUT = 5.0
_MAX_BODY = 65536
_READ_LIMIT = 65537
_ACCEPT = "application/json"
_ACCEPT_ENCODING = "identity"
_KEY_ID_HEADER = "APCA-API-KEY-ID"
_SECRET_KEY_HEADER = "APCA-API-SECRET-KEY"
_ACCEPT_HEADER = "Accept"
_ACCEPT_ENCODING_HEADER = "Accept-Encoding"
_HEADER_NAMES = (
    "APCA-API-KEY-ID",
    "APCA-API-SECRET-KEY",
    "Accept",
    "Accept-Encoding",
)
_TARGETS = (
    "/v2/stocks/trades/latest?symbols=AAPL&feed=iex",
    "/v2/stocks/trades/latest?symbols=MSFT&feed=iex",
    "/v2/stocks/trades/latest?symbols=SPY&feed=iex",
    "/v2/stocks/trades/latest?symbols=AAPL,MSFT&feed=iex",
    "/v2/stocks/trades/latest?symbols=AAPL,SPY&feed=iex",
    "/v2/stocks/trades/latest?symbols=MSFT,SPY&feed=iex",
    "/v2/stocks/trades/latest?symbols=AAPL,MSFT,SPY&feed=iex",
)
_ACCEPTED_CONTENT_TYPES = (
    "application/json",
    "application/json; charset=utf-8",
)
_CHUNKED = "chunked"
_MAX_CONTENT_LENGTH_DIGITS = 5


class _TransportSignal(Exception):
    """Internal base for the frozen provider-response signals."""


class _TransportUnavailable(_TransportSignal):
    """Internal signal: the finite transport could not complete."""


class _ResponseMalformed(_TransportSignal):
    """Internal signal: the response violated the frozen framing rules."""


class _SourceRejected(_TransportSignal):
    """Internal signal: the provider rejected authentication or access."""


class _SourceUnavailable(_TransportSignal):
    """Internal signal: the provider resource is unavailable."""


class _SourceRateLimited(_TransportSignal):
    """Internal signal: the provider rate limit was reached."""


class _InvalidTransportRequest(Exception):
    """Internal signal: a non-frozen target or header map reached the seam."""


class TransportResponse:
    """One bounded, validated provider response."""

    __slots__ = (
        "body",
        "content_encoding",
        "content_length",
        "content_type",
        "status",
        "transfer_encoding",
    )

    def __init__(
        self,
        *,
        status: int,
        content_type: str | None,
        content_encoding: str | None,
        content_length: str | None,
        transfer_encoding: str | None,
        body: bytes,
    ) -> None:
        self.status = status
        self.content_type = content_type
        self.content_encoding = content_encoding
        self.content_length = content_length
        self.transfer_encoding = transfer_encoding
        self.body = body


class HttpsGet:
    """The finite production transport; exactly one request per call."""

    __slots__ = ()

    def get(
        self, target: str, headers: Mapping[str, str]
    ) -> TransportResponse:
        """Perform one frozen HTTPS GET and return a bounded response."""
        _validate_request(target, headers)
        failure: Exception | None = None
        outcome: TransportResponse | None = None
        connection: http.client.HTTPSConnection | None = None
        try:
            context = ssl.create_default_context()
            connection = http.client.HTTPSConnection(
                host=_HOST,
                port=_PORT,
                timeout=_TIMEOUT,
                context=context,
            )
            try:
                connection.putrequest(
                    _METHOD,
                    target,
                    skip_host=False,
                    skip_accept_encoding=True,
                )
                connection.putheader(
                    _KEY_ID_HEADER, headers[_KEY_ID_HEADER]
                )
                connection.putheader(
                    _SECRET_KEY_HEADER, headers[_SECRET_KEY_HEADER]
                )
                connection.putheader(_ACCEPT_HEADER, _ACCEPT)
                connection.putheader(
                    _ACCEPT_ENCODING_HEADER, _ACCEPT_ENCODING
                )
                connection.endheaders()
                response = connection.getresponse()
                outcome = _decode_response(response)
            finally:
                _close(connection)
        except _TransportSignal:
            raise
        except Exception:  # noqa: BLE001 - frozen deferred mapping
            failure = _TransportUnavailable()
        if failure is not None:
            raise failure
        if outcome is None:
            raise _TransportUnavailable()
        return outcome


def _validate_request(target: str, headers: Mapping[str, str]) -> None:
    if target not in _TARGETS:
        raise _InvalidTransportRequest()
    if len(headers) != len(_HEADER_NAMES):
        raise _InvalidTransportRequest()
    for name in _HEADER_NAMES:
        if name not in headers:
            raise _InvalidTransportRequest()
    for name in _HEADER_NAMES:
        value = headers[name]
        if not isinstance(value, str):
            raise _InvalidTransportRequest()
        if "\r" in value or "\n" in value or "\x00" in value:
            raise _InvalidTransportRequest()
    if headers[_ACCEPT_HEADER] != _ACCEPT:
        raise _InvalidTransportRequest()
    if headers[_ACCEPT_ENCODING_HEADER] != _ACCEPT_ENCODING:
        raise _InvalidTransportRequest()


def _close(connection: http.client.HTTPSConnection | None) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except Exception:  # noqa: BLE001 - frozen best-effort close
        return


def _decode_response(response: http.client.HTTPResponse) -> TransportResponse:
    status = response.status
    if status != 200:
        raise _status_signal(status)
    content_type = _header_value(response, "Content-Type")
    content_encoding = _header_value(response, "Content-Encoding")
    content_length = _header_value(response, "Content-Length")
    transfer_encoding = _header_value(response, "Transfer-Encoding")
    if content_type is None:
        raise _ResponseMalformed()
    if content_type.lower() not in _ACCEPTED_CONTENT_TYPES:
        raise _ResponseMalformed()
    if content_encoding is not None and (
        content_encoding.lower() != _ACCEPT_ENCODING
    ):
        raise _ResponseMalformed()
    if content_length is not None and transfer_encoding is not None:
        raise _ResponseMalformed()
    if content_length is None and transfer_encoding is None:
        raise _ResponseMalformed()
    declared: int | None = None
    if content_length is not None:
        declared = _parse_content_length(content_length)
    else:
        if transfer_encoding is None:
            raise _ResponseMalformed()
        if transfer_encoding.lower() != _CHUNKED:
            raise _ResponseMalformed()
    body = response.read(_READ_LIMIT)
    if len(body) > _MAX_BODY:
        raise _ResponseMalformed()
    if declared is not None and len(body) != declared:
        raise _TransportUnavailable()
    return TransportResponse(
        status=status,
        content_type=content_type,
        content_encoding=content_encoding,
        content_length=content_length,
        transfer_encoding=transfer_encoding,
        body=body,
    )


def _header_value(
    response: http.client.HTTPResponse, name: str
) -> str | None:
    value = response.getheader(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ResponseMalformed()
    return value


def _parse_content_length(value: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise _ResponseMalformed()
    if len(value) > 1 and value[0] == "0":
        raise _ResponseMalformed()
    if len(value) > _MAX_CONTENT_LENGTH_DIGITS:
        raise _ResponseMalformed()
    declared = int(value)
    if declared > _MAX_BODY:
        raise _ResponseMalformed()
    return declared


def _status_signal(status: int) -> _TransportSignal:
    if 300 <= status < 400:
        return _ResponseMalformed()
    if status == 401 or status == 403:
        return _SourceRejected()
    if status == 429:
        return _SourceRateLimited()
    return _SourceUnavailable()
