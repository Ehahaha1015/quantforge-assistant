"""Frozen OPERATOR-001 canonicalization and SQL-normalization rules."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import OperatorArgumentError

OPAQUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
COMMITMENT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
CANONICAL_INSTANT_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


def opaque(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or OPAQUE_PATTERN.fullmatch(value) is None:
        raise OperatorArgumentError(f"{name} is malformed")
    return value


def commitment(value: Any, *, name: str = "request_commitment") -> str:
    if not isinstance(value, str) or COMMITMENT_PATTERN.fullmatch(value) is None:
        raise OperatorArgumentError(f"{name} is malformed")
    return value


def utc(value: Any, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise OperatorArgumentError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise OperatorArgumentError(f"{name} must be timezone-aware")
    try:
        return value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError) as exc:
        raise OperatorArgumentError(
            f"{name} is outside the representable UTC range"
        ) from exc


def canonical_instant(value: Any, *, name: str) -> str:
    moment = utc(value, name=name)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_canonical_instant(value: Any, *, name: str) -> datetime:
    if not isinstance(value, str) or CANONICAL_INSTANT_PATTERN.fullmatch(value) is None:
        raise OperatorArgumentError(f"{name} is not a canonical UTC instant")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise OperatorArgumentError(f"{name} is not a canonical UTC instant") from exc
    if canonical_instant(parsed, name=name) != value:
        raise OperatorArgumentError(f"{name} is not a canonical UTC instant")
    return parsed


def is_canonical_instant(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse_canonical_instant(value, name="instant")
    except OperatorArgumentError:
        return False
    return True


def positive_duration(value: Any, *, name: str) -> timedelta:
    if not isinstance(value, timedelta):
        raise OperatorArgumentError(f"{name} must be a timedelta")
    if value <= timedelta(0):
        raise OperatorArgumentError(f"{name} must be strictly positive")
    return value


def duration_microseconds(value: Any, *, name: str) -> int:
    duration = positive_duration(value, name=name)
    microseconds = (
        duration.days * 86_400_000_000
        + duration.seconds * 1_000_000
        + duration.microseconds
    )
    if microseconds <= 0:
        raise OperatorArgumentError(f"{name} must be strictly positive")
    return microseconds


def microseconds_duration(value: Any, *, name: str) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperatorArgumentError(f"{name} must be an integer")
    if value < 1:
        raise OperatorArgumentError(f"{name} must be at least 1")
    return timedelta(microseconds=value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def sha256_hex_utf8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest32(value: str) -> str:
    return sha256_hex_utf8(value)[:32]


def normalize_sql(sql: str) -> str:
    """Implement the frozen normalize_sql rule of contract section 13."""

    if not isinstance(sql, str) or sql == "":
        raise OperatorArgumentError("sql must be a nonempty string")
    try:
        sql.encode("ascii")
    except UnicodeEncodeError as exc:
        raise OperatorArgumentError("sql must be ASCII") from exc

    # Step 1: remove comments with single-quoted-literal awareness.
    out: list[str] = []
    index = 0
    in_literal = False
    length = len(sql)
    while index < length:
        char = sql[index]
        if in_literal:
            out.append(char)
            if char == "'":
                if index + 1 < length and sql[index + 1] == "'":
                    out.append("'")
                    index += 2
                    continue
                in_literal = False
            index += 1
            continue
        if char == "'":
            in_literal = True
            out.append(char)
            index += 1
            continue
        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            index += 2
            while index < length and sql[index] != "\n":
                index += 1
            if index < length:
                out.append("\n")
                index += 1
            continue
        if char == "/" and index + 1 < length and sql[index + 1] == "*":
            index += 2
            while index + 1 < length and not (
                sql[index] == "*" and sql[index + 1] == "/"
            ):
                index += 1
            index = min(index + 2, length)
            continue
        out.append(char)
        index += 1

    text = "".join(out).strip()
    text = text.removesuffix(";")

    # Step 4: lowercase outside literals, collapse whitespace and remove
    # spaces around the four punctuation characters named by the contract.
    final: list[str] = []
    index = 0
    in_literal = False
    pending_space = False
    length = len(text)
    while index < length:
        char = text[index]
        if in_literal:
            final.append(char)
            if char == "'":
                if index + 1 < length and text[index + 1] == "'":
                    final.append("'")
                    index += 2
                    continue
                in_literal = False
            index += 1
            continue
        if char == "'":
            if pending_space and final and final[-1] not in " (,;":
                final.append(" ")
            pending_space = False
            in_literal = True
            final.append(char)
            index += 1
            continue
        if char.isspace():
            pending_space = True
            index += 1
            continue
        if pending_space:
            if (
                final
                and final[-1] not in " (,;"
                and char not in "),;"
            ):
                final.append(" ")
            pending_space = False
        if char in "(),;":
            while final and final[-1] == " ":
                final.pop()
            final.append(char)
        else:
            final.append(char.lower())
        index += 1
    return "".join(final)


__all__ = [
    "CANONICAL_INSTANT_PATTERN",
    "COMMITMENT_PATTERN",
    "OPAQUE_PATTERN",
    "canonical_instant",
    "canonical_json",
    "commitment",
    "digest32",
    "duration_microseconds",
    "is_canonical_instant",
    "microseconds_duration",
    "normalize_sql",
    "opaque",
    "parse_canonical_instant",
    "positive_duration",
    "sha256_hex_utf8",
    "utc",
]
