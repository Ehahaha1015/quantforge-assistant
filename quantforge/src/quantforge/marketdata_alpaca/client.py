"""Frozen request-target construction and provider response parsing.

Every rule of the frozen response schema, price bound, timestamp grammar and
trade-update decision lives here.  The module imports no network, TLS or
credential module; it raises private internal signals that ``source.py``
maps to the frozen public errors.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .transport import _ResponseMalformed

_TARGET_PREFIX = "/v2/stocks/trades/latest?symbols="
_TARGET_SUFFIX = "&feed=iex"
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
_TIMESTAMP_LENGTH_MIN = 22
_TIMESTAMP_LENGTH_MAX = 30
_TIMESTAMP_DIGIT_POSITIONS = (0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18)
_TIMESTAMP_FRACTION_INDEX = 20
_FRACTION_DIGITS = 6
_MAX_TRADE_ID = 18446744073709551615
_MAX_TRADE_SIZE = 4294967295
_PRICE_CEILING = 10000000
_MAX_COEFFICIENT_DIGITS = 15
_MAX_FRACTIONAL_DIGITS = 6
_REQUIRED_TRADE_MEMBERS = ("t", "i", "x", "p", "s", "c", "z")
_TRADE_MEMBERS = ("t", "i", "x", "p", "s", "c", "z", "u")
_TAPE_CODES = ("A", "B", "C", "N", "O")
_TOP_LEVEL_MEMBERS = ("trades", "currency")
_BASE_CURRENCY = "USD"
_TRADE_UPDATE_CORRECTED = "corrected"


class _CoverageMismatch(Exception):
    """Internal signal: the response symbol coverage is not exact."""


class _InvalidPrice(Exception):
    """Internal signal: a trade price violates the frozen bounds."""


class _InvalidTimestamp(Exception):
    """Internal signal: a provider timestamp violates the frozen grammar."""


class _ParsedTrade:
    """One accepted provider trade."""

    __slots__ = (
        "observed_at",
        "price",
        "provider_timestamp",
        "symbol",
        "trade_id",
    )

    def __init__(
        self,
        *,
        symbol: str,
        price: Decimal,
        trade_id: int,
        provider_timestamp: str,
        observed_at: datetime,
    ) -> None:
        self.symbol = symbol
        self.price = price
        self.trade_id = trade_id
        self.provider_timestamp = provider_timestamp
        self.observed_at = observed_at


def _build_target(symbols: tuple[str, ...]) -> str:
    return _TARGET_PREFIX + ",".join(symbols) + _TARGET_SUFFIX


def _parse_response(
    symbols: tuple[str, ...], body: bytes
) -> tuple[_ParsedTrade, ...]:
    failure: Exception | None = None
    parsed: tuple[_ParsedTrade, ...] | None = None
    try:
        parsed = _parse_document(symbols, body)
    except (
        _ResponseMalformed,
        _CoverageMismatch,
        _InvalidPrice,
        _InvalidTimestamp,
    ):
        raise
    except Exception:  # noqa: BLE001 - frozen deferred response mapping
        failure = _ResponseMalformed()
    if failure is not None:
        raise failure
    if parsed is None:
        raise _ResponseMalformed()
    return parsed


def _parse_document(
    symbols: tuple[str, ...], body: bytes
) -> tuple[_ParsedTrade, ...]:
    document = json.loads(
        body.decode("utf-8"),
        parse_float=Decimal,
        parse_int=int,
        parse_constant=_reject_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )
    if not isinstance(document, dict):
        raise _ResponseMalformed()
    for member in document:
        if member not in _TOP_LEVEL_MEMBERS:
            raise _ResponseMalformed()
    if "trades" not in document:
        raise _ResponseMalformed()
    if "currency" in document:
        currency = document["currency"]
        if not isinstance(currency, str) or currency != _BASE_CURRENCY:
            raise _ResponseMalformed()
    trades = document["trades"]
    if not isinstance(trades, dict):
        raise _ResponseMalformed()
    if len(trades) != len(symbols) or set(trades) != set(symbols):
        raise _CoverageMismatch()
    collected: list[_ParsedTrade] = []
    for symbol in symbols:
        collected.append(_parse_trade(symbol, trades[symbol]))
    return tuple(collected)


def _parse_trade(symbol: str, trade: Any) -> _ParsedTrade:
    if not isinstance(trade, dict):
        raise _ResponseMalformed()
    for member in trade:
        if member not in _TRADE_MEMBERS:
            raise _ResponseMalformed()
    for member in _REQUIRED_TRADE_MEMBERS:
        if member not in trade:
            raise _ResponseMalformed()
    timestamp = trade["t"]
    if not isinstance(timestamp, str):
        raise _ResponseMalformed()
    observed_at = _parse_timestamp(timestamp)
    trade_id = trade["i"]
    if (
        isinstance(trade_id, bool)
        or not isinstance(trade_id, int)
        or trade_id < 0
        or trade_id > _MAX_TRADE_ID
    ):
        raise _ResponseMalformed()
    exchange = trade["x"]
    if not isinstance(exchange, str):
        raise _ResponseMalformed()
    price = _parse_price(trade["p"])
    size = trade["s"]
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > _MAX_TRADE_SIZE
    ):
        raise _ResponseMalformed()
    conditions = trade["c"]
    if not isinstance(conditions, list):
        raise _ResponseMalformed()
    for condition in conditions:
        if not isinstance(condition, str):
            raise _ResponseMalformed()
    tape = trade["z"]
    if not isinstance(tape, str) or tape not in _TAPE_CODES:
        raise _ResponseMalformed()
    if "u" in trade:
        update = trade["u"]
        if not isinstance(update, str):
            raise _ResponseMalformed()
        if update != _TRADE_UPDATE_CORRECTED:
            raise _ResponseMalformed()
    return _ParsedTrade(
        symbol=symbol,
        price=price,
        trade_id=trade_id,
        provider_timestamp=timestamp,
        observed_at=observed_at,
    )


def _parse_price(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise _ResponseMalformed()
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, Decimal):
        number = value
    else:
        raise _ResponseMalformed()
    if not number.is_finite() or number <= 0:
        raise _InvalidPrice()
    if number >= _PRICE_CEILING:
        raise _InvalidPrice()
    _, digits, raw_exponent = number.as_tuple()
    exponent = int(raw_exponent)
    if len(digits) > _MAX_COEFFICIENT_DIGITS:
        raise _InvalidPrice()
    if max(0, -exponent) > _MAX_FRACTIONAL_DIGITS:
        raise _InvalidPrice()
    return number


def _parse_timestamp(value: str) -> datetime:
    if not _valid_timestamp_shape(value):
        raise _InvalidTimestamp()
    head = value[:19]
    fraction = value[_TIMESTAMP_FRACTION_INDEX:-1]
    microseconds = int((fraction + "000000")[:_FRACTION_DIGITS])
    failure: Exception | None = None
    moment: datetime | None = None
    try:
        moment = datetime.strptime(head, _TIMESTAMP_FORMAT).replace(
            tzinfo=UTC
        )
    except ValueError:
        failure = _InvalidTimestamp()
    if failure is not None:
        raise failure
    if moment is None:
        raise _InvalidTimestamp()
    return moment.replace(microsecond=microseconds)


def _valid_timestamp_shape(value: str) -> bool:
    if len(value) < _TIMESTAMP_LENGTH_MIN:
        return False
    if len(value) > _TIMESTAMP_LENGTH_MAX:
        return False
    if value[4] != "-" or value[7] != "-" or value[10] != "T":
        return False
    if value[13] != ":" or value[16] != ":" or value[19] != ".":
        return False
    if value[-1] != "Z":
        return False
    for index in _TIMESTAMP_DIGIT_POSITIONS:
        character = value[index]
        if not character.isascii() or not character.isdigit():
            return False
    for character in value[_TIMESTAMP_FRACTION_INDEX:-1]:
        if not character.isascii() or not character.isdigit():
            return False
    return True


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _ResponseMalformed()
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    raise _ResponseMalformed()
