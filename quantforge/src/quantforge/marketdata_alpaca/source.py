"""Frozen PRICE-001 adapter for the Alpaca IEX latest-trade source.

The adapter validates the requested symbol set, obtains exactly one
credentialed response through the injected fetch seam, parses it and
constructs the frozen verified observation.  Both identity derivations of
the frozen contract are repeated here byte-for-byte and are proven equal to
the PRICE-001 derivations by a test-side oracle.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from quantforge.marketdata import (
    PriceArgumentError,
    PriceCoverageError,
    PriceFactError,
    PriceObservationRequest,
    PriceSourceError,
    PriceTimestampError,
    VerifiedPriceObservation,
)
from quantforge.provenance import RiskPriceFact

from . import client, transport
from .transport import TransportResponse

_SOURCE_ID = "alpaca-iex-latest-trade-v1"
_FEED = "iex"
_SYMBOLS = ("AAPL", "MSFT", "SPY")
_OBSERVATION_SCHEMA = "marketdata-observation-v1"
_COMPOSITE_SCHEMA = "marketdata-alpaca-observation-v1"
_COMPOSITE_PREFIX = "alpaca-iex-trade-v1-"
_OBSERVATION_PREFIX = "marketdata-obs-v1-"
_MAXIMUM_AGE_SECONDS = 15
_MICROSECONDS_PER_SECOND = 1000000
_MICROSECONDS_PER_DAY = 86400000000
_COVERAGE_MESSAGE = (
    "observation symbol coverage does not match the required symbols"
)
_MALFORMED_MESSAGE = "price observation response was malformed"
_TRANSPORT_MESSAGE = "price observation transport failed"
_REJECTED_MESSAGE = "price observation source rejected"
_UNAVAILABLE_MESSAGE = "price observation source is unavailable"
_RATE_LIMITED_MESSAGE = "price observation source is rate limited"
_CREDENTIAL_MESSAGE = "price observation credential is malformed"
_PRICE_MESSAGE = "risk price must be a finite positive Decimal"
_TIMESTAMP_MESSAGE = "observed_at is malformed"
_REQUEST_AUTHORITY_MESSAGE = "price observation request authority is malformed"


@dataclass(frozen=True, slots=True)
class _StaleDiagnosticSnapshot:
    aggregate_age_category: Literal[
        "FUTURE", "FRESH", "STALE_THRESHOLD", "STALE"
    ]
    per_symbol_relation: tuple[
        tuple[
            str,
            Literal["FUTURE", "FRESH", "STALE_THRESHOLD", "STALE"],
        ],
        ...,
    ]


@dataclass(frozen=True, slots=True)
class _CapturedSmokeObservation:
    observation: VerifiedPriceObservation
    diagnostic: _StaleDiagnosticSnapshot


class _CredentialMaterialMalformed(Exception):
    """Internal signal: the revealed credential blob is not the frozen JSON."""


class AlpacaIexLatestTradeSource:
    """The frozen PriceObservationSource adapter.

    The class is intentionally not exported and is constructed only by the
    module-private factory below, which also binds the fetch seam.
    """

    __slots__ = ("_fetch",)

    def __init__(self) -> None:
        self._fetch: Callable[[str], TransportResponse] | None = None

    @property
    def source_id(self) -> str:
        return _SOURCE_ID

    def observe(
        self, request: PriceObservationRequest
    ) -> VerifiedPriceObservation:
        parsed = self._fetch_and_parse(request)
        return _build_observation(parsed)

    def _capture_smoke_observation(
        self,
        request: PriceObservationRequest,
        *,
        dispatch_timestamp: datetime,
    ) -> _CapturedSmokeObservation:
        parsed = self._fetch_and_parse(request)
        observation = _build_observation(parsed)
        diagnostic = _build_diagnostic(
            parsed,
            aggregate_observed_at=observation.observed_at,
            dispatch_timestamp=dispatch_timestamp,
        )
        return _CapturedSmokeObservation(
            observation=observation,
            diagnostic=diagnostic,
        )

    def _fetch_and_parse(
        self, request: PriceObservationRequest
    ) -> tuple[client._ParsedTrade, ...]:
        symbols = _requested_symbols(request)
        target = client._build_target(symbols)
        source_fetch = self._fetch
        if source_fetch is None:
            raise PriceSourceError(_UNAVAILABLE_MESSAGE)
        failure: Exception | None = None
        parsed: tuple[client._ParsedTrade, ...] | None = None
        try:
            fetched = source_fetch(target)
            parsed = client._parse_response(symbols, fetched.body)
        except transport._TransportUnavailable:
            failure = PriceSourceError(_TRANSPORT_MESSAGE)
        except transport._ResponseMalformed:
            failure = PriceSourceError(_MALFORMED_MESSAGE)
        except transport._SourceRejected:
            failure = PriceSourceError(_REJECTED_MESSAGE)
        except transport._SourceUnavailable:
            failure = PriceSourceError(_UNAVAILABLE_MESSAGE)
        except transport._SourceRateLimited:
            failure = PriceSourceError(_RATE_LIMITED_MESSAGE)
        except transport._InvalidTransportRequest:
            failure = PriceArgumentError(_REQUEST_AUTHORITY_MESSAGE)
        except client._CoverageMismatch:
            failure = PriceCoverageError(_COVERAGE_MESSAGE)
        except client._InvalidPrice:
            failure = PriceFactError(_PRICE_MESSAGE)
        except client._InvalidTimestamp:
            failure = PriceTimestampError(_TIMESTAMP_MESSAGE)
        except _CredentialMaterialMalformed:
            failure = PriceSourceError(_CREDENTIAL_MESSAGE)
        if failure is not None:
            raise failure from None
        if parsed is None:
            raise PriceSourceError(_MALFORMED_MESSAGE)
        return parsed


def _create_source(
    *, fetch: Callable[[str], TransportResponse]
) -> AlpacaIexLatestTradeSource:
    source = AlpacaIexLatestTradeSource()
    source._fetch = fetch
    return source


def _requested_symbols(
    request: PriceObservationRequest,
) -> tuple[str, ...]:
    symbols = request.symbols
    if not isinstance(symbols, tuple) or not symbols:
        raise PriceCoverageError(_COVERAGE_MESSAGE)
    seen: list[str] = []
    for symbol in symbols:
        if not isinstance(symbol, str) or symbol not in _SYMBOLS:
            raise PriceCoverageError(_COVERAGE_MESSAGE)
        if symbol in seen:
            raise PriceCoverageError(_COVERAGE_MESSAGE)
        seen.append(symbol)
    ordered = tuple(sorted(symbols, key=_symbol_bytes))
    if ordered != symbols:
        raise PriceCoverageError(_COVERAGE_MESSAGE)
    return symbols


def _symbol_bytes(symbol: str) -> bytes:
    return symbol.encode("utf-8")


def _build_observation(
    parsed: tuple[client._ParsedTrade, ...],
) -> VerifiedPriceObservation:
    observed_at = min(trade.observed_at for trade in parsed)
    risk_prices = tuple(
        RiskPriceFact(symbol=trade.symbol, price=trade.price)
        for trade in parsed
    )
    maximum_age = timedelta(seconds=_MAXIMUM_AGE_SECONDS)
    source_observation_id = _source_observation_id(parsed)
    observation_id = _observation_id(
        market_source=_SOURCE_ID,
        source_observation_id=source_observation_id,
        observed_at=observed_at,
        maximum_age=maximum_age,
        risk_prices=risk_prices,
    )
    return VerifiedPriceObservation(
        schema=_OBSERVATION_SCHEMA,
        observation_id=observation_id,
        market_source=_SOURCE_ID,
        source_observation_id=source_observation_id,
        observed_at=observed_at,
        maximum_age=maximum_age,
        risk_prices=risk_prices,
    )


def _build_diagnostic(
    parsed: tuple[client._ParsedTrade, ...],
    *,
    aggregate_observed_at: datetime,
    dispatch_timestamp: datetime,
) -> _StaleDiagnosticSnapshot:
    return _StaleDiagnosticSnapshot(
        aggregate_age_category=_age_category(
            dispatch_timestamp, aggregate_observed_at
        ),
        per_symbol_relation=tuple(
            (
                trade.symbol,
                _age_category(dispatch_timestamp, trade.observed_at),
            )
            for trade in parsed
        ),
    )


def _age_category(
    dispatch_timestamp: datetime, observed_at: datetime
) -> Literal["FUTURE", "FRESH", "STALE_THRESHOLD", "STALE"]:
    age = dispatch_timestamp - observed_at
    age_microseconds = (
        age.days * _MICROSECONDS_PER_DAY
        + age.seconds * _MICROSECONDS_PER_SECOND
        + age.microseconds
    )
    if age_microseconds < 0:
        return "FUTURE"
    if age_microseconds <= 15_000_000:
        return "FRESH"
    if age_microseconds == 15_000_001:
        return "STALE_THRESHOLD"
    return "STALE"


def _source_observation_id(
    parsed: tuple[client._ParsedTrade, ...],
) -> str:
    trades = [
        {
            "symbol": trade.symbol,
            "trade_id": trade.trade_id,
            "provider_timestamp": trade.provider_timestamp,
        }
        for trade in parsed
    ]
    payload = {
        "schema": _COMPOSITE_SCHEMA,
        "source": _SOURCE_ID,
        "feed": _FEED,
        "trades": trades,
    }
    return _COMPOSITE_PREFIX + _sha256(_canonical_json(payload))


def _observation_id(
    *,
    market_source: str,
    source_observation_id: str,
    observed_at: datetime,
    maximum_age: timedelta,
    risk_prices: tuple[RiskPriceFact, ...],
) -> str:
    payload = {
        "schema": _OBSERVATION_SCHEMA,
        "market_source": market_source,
        "source_observation_id": source_observation_id,
        "observed_at": _canonical_instant(observed_at),
        "maximum_age_microseconds": _duration_microseconds(maximum_age),
        "risk_prices": _canonical_price_facts(risk_prices),
    }
    return _OBSERVATION_PREFIX + _sha256(_canonical_json(payload))


def _canonical_price_facts(
    risk_prices: tuple[RiskPriceFact, ...],
) -> list[dict[str, object]]:
    return [
        {"price": _canonical_decimal(fact.price), "symbol": fact.symbol}
        for fact in risk_prices
    ]


def _canonical_decimal(value: Decimal) -> dict[str, object]:
    sign, raw_digits, raw_exponent = value.as_tuple()
    if value.is_zero():
        return {"coefficient": "0", "exponent": 0, "sign": 0}
    digits = list(raw_digits)
    exponent = int(raw_exponent)
    trailing = 0
    for digit in reversed(digits):
        if digit != 0:
            break
        trailing += 1
    if trailing:
        del digits[len(digits) - trailing :]
        exponent += trailing
    return {
        "coefficient": "".join(str(digit) for digit in digits),
        "exponent": exponent,
        "sign": int(sign),
    }


def _duration_microseconds(value: timedelta) -> int:
    return (
        value.days * _MICROSECONDS_PER_DAY
        + value.seconds * _MICROSECONDS_PER_SECOND
        + value.microseconds
    )


def _canonical_instant(value: datetime) -> str:
    moment = value.astimezone(UTC)
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T"
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}."
        f"{moment.microsecond:06d}Z"
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
