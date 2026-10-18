from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from quantforge.execution_ledger import (
    ExecutionLedgerCorruptionError,
    ExecutionLedgerDeniedError,
)
from quantforge.execution_ledger._canonical import (
    canonical_instant,
    canonical_json,
    check_utf8_bound,
    opaque,
    parse_canonical_instant,
)


def test_canonical_json_sorts_keys_and_compacts() -> None:
    assert canonical_json({"b": 1, "a": [2, 3]}) == '{"a":[2,3],"b":1}'
    assert canonical_json({"z": {"b": 1, "a": 2}}) == '{"z":{"a":2,"b":1}}'


def test_canonical_json_escapes_non_ascii() -> None:
    assert canonical_json({"x": "é"}) == '{"x":"\\u00e9"}'


def test_canonical_json_rejects_nan_and_infinity() -> None:
    with pytest.raises(ExecutionLedgerDeniedError):
        canonical_json({"x": float("nan")})
    with pytest.raises(ExecutionLedgerDeniedError):
        canonical_json({"x": float("inf")})


def test_canonical_json_rejects_non_serializable() -> None:
    with pytest.raises(ExecutionLedgerDeniedError):
        canonical_json({"x": object()})


def test_canonical_instant_normalizes_to_utc_microseconds() -> None:
    moment = datetime(
        2030, 1, 1, 20, 2, 1, 123456,
        tzinfo=timezone(timedelta(hours=8)),
    )
    assert canonical_instant(moment) == "2030-01-01T12:02:01.123456Z"


def test_canonical_instant_rejects_naive_and_non_datetime() -> None:
    with pytest.raises(ExecutionLedgerDeniedError):
        canonical_instant(
            datetime(2030, 1, 1, 12, 2, 1)  # noqa: DTZ001 - intentional naive input
        )
    with pytest.raises(TypeError):
        canonical_instant("2030-01-01T12:02:01.123456Z")  # type: ignore[arg-type]


def test_parse_canonical_instant_round_trip() -> None:
    parsed = parse_canonical_instant("2030-01-01T12:02:01.123456Z")
    assert parsed == datetime(2030, 1, 1, 12, 2, 1, 123456, tzinfo=UTC)
    assert canonical_instant(parsed) == "2030-01-01T12:02:01.123456Z"


@pytest.mark.parametrize(
    "value",
    [
        "2030-01-01T12:02:01Z",
        "2030-01-01T12:02:01.123456+00:00",
        "2030-01-01 12:02:01.123456Z",
        "2030-13-01T12:02:01.123456Z",
        "not-a-time",
    ],
)
def test_parse_canonical_instant_rejects_non_canonical(value: str) -> None:
    with pytest.raises(ExecutionLedgerCorruptionError):
        parse_canonical_instant(value)


def test_parse_canonical_instant_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        parse_canonical_instant(123)  # type: ignore[arg-type]


def test_opaque_accepts_unicode_and_bounds() -> None:
    assert opaque("reference-1", name="reference") == "reference-1"
    assert opaque("é" * 256, name="reference", max_codepoints=256) == "é" * 256


@pytest.mark.parametrize(
    "value",
    ["", "   ", "bad\nvalue", "bad\x00value", "a" * 257],
)
def test_opaque_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ExecutionLedgerDeniedError):
        opaque(value, name="reference", max_codepoints=256)


def test_opaque_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        opaque(None, name="reference")  # type: ignore[arg-type]


def test_check_utf8_bound_counts_bytes_not_code_points() -> None:
    check_utf8_bound("é" * 10, name="payload", limit=20)
    with pytest.raises(ExecutionLedgerDeniedError):
        check_utf8_bound("é" * 10, name="payload", limit=19)
