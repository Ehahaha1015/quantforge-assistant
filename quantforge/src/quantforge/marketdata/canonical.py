"""Deterministic canonicalization and identity derivation (contract section 9)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

_OBSERVATION_SCHEMA = "marketdata-observation-v1"
_EVIDENCE_SCHEMA = "marketdata-price-evidence-v1"
_OBSERVATION_PREFIX = "marketdata-obs-v1-"
_EVIDENCE_PREFIX = "marketdata-price-v1-"
_MICROSECONDS_PER_SECOND = 1_000_000
_MICROSECONDS_PER_DAY = 86_400 * _MICROSECONDS_PER_SECOND


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_decimal(value: Decimal) -> dict[str, int | str]:
    """The frozen provenance Decimal projection (trailing zeros stripped)."""
    sign, raw_digits, raw_exponent = value.as_tuple()
    if value.is_zero():
        return {"coefficient": "0", "exponent": 0, "sign": 0}
    digits = list(raw_digits)
    exponent = int(raw_exponent)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return {
        "coefficient": "".join(str(digit) for digit in digits),
        "exponent": exponent,
        "sign": int(sign),
    }


def _canonical_instant(value: datetime) -> str:
    moment = value.astimezone(UTC)
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T"
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}."
        f"{moment.microsecond:06d}Z"
    )


def _duration_microseconds(value: timedelta) -> int:
    return (
        value.days * _MICROSECONDS_PER_DAY
        + value.seconds * _MICROSECONDS_PER_SECOND
        + value.microseconds
    )


def _canonical_price_facts(risk_prices: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [
        {"price": _canonical_decimal(fact.price), "symbol": fact.symbol}
        for fact in risk_prices
    ]


def _observation_payload(
    *,
    market_source: str,
    source_observation_id: str,
    observed_at: datetime,
    maximum_age: timedelta,
    risk_prices: tuple[Any, ...],
) -> dict[str, Any]:
    return {
        "schema": _OBSERVATION_SCHEMA,
        "market_source": market_source,
        "source_observation_id": source_observation_id,
        "observed_at": _canonical_instant(observed_at),
        "maximum_age_microseconds": _duration_microseconds(maximum_age),
        "risk_prices": _canonical_price_facts(risk_prices),
    }


def _observation_id(
    *,
    market_source: str,
    source_observation_id: str,
    observed_at: datetime,
    maximum_age: timedelta,
    risk_prices: tuple[Any, ...],
) -> str:
    payload = _observation_payload(
        market_source=market_source,
        source_observation_id=source_observation_id,
        observed_at=observed_at,
        maximum_age=maximum_age,
        risk_prices=risk_prices,
    )
    return _OBSERVATION_PREFIX + _sha256_hex(_canonical_json(payload))


def _evidence_id(
    *,
    market_source: str,
    source_observation_id: str,
    observed_at: datetime,
    maximum_age: timedelta,
    risk_prices: tuple[Any, ...],
    provenance_id: str,
    price_set_commitment: str,
    submission_id: str,
) -> str:
    payload: dict[str, Any] = {
        "schema": _EVIDENCE_SCHEMA,
        "market_source": market_source,
        "source_observation_id": source_observation_id,
        "observed_at": _canonical_instant(observed_at),
        "maximum_age_microseconds": _duration_microseconds(maximum_age),
        "risk_prices": _canonical_price_facts(risk_prices),
        "provenance_id": provenance_id,
        "price_set_commitment": price_set_commitment,
        "submission_id": submission_id,
    }
    return _EVIDENCE_PREFIX + _sha256_hex(_canonical_json(payload))


__all__ = []
