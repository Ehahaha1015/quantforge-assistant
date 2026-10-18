"""Canonical serialization helpers for the execution ledger.

These helpers deliberately mirror EXECSTATE-001 v2 conventions so ledger
evidence and EXECSTATE evidence canonicalize identically.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from .errors import ExecutionLedgerCorruptionError, ExecutionLedgerDeniedError

_CANONICAL_INSTANT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"\.[0-9]{6}Z$"
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def canonical_json(value: object) -> str:
    """Return the canonical JSON encoding of a JSON-compatible value."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionLedgerDeniedError(
            "value is not canonically serializable"
        ) from exc


def canonical_instant(value: datetime) -> str:
    """Return a canonical UTC instant string for one aware datetime."""

    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionLedgerDeniedError("timestamp must be timezone-aware")
    try:
        moment = value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ExecutionLedgerDeniedError(
            "timestamp is outside the representable UTC range"
        ) from exc
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_canonical_instant(value: str) -> datetime:
    """Parse one canonical UTC instant from persisted ledger bytes."""

    if not isinstance(value, str):
        raise TypeError("instant must be a string")
    if _CANONICAL_INSTANT.fullmatch(value) is None:
        raise ExecutionLedgerCorruptionError(
            "instant is not a canonical UTC instant"
        )
    try:
        return datetime.strptime(
            value, "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=UTC)
    except ValueError as exc:
        raise ExecutionLedgerCorruptionError(
            "instant is not a valid canonical UTC instant"
        ) from exc


def opaque(value: str, *, name: str, max_codepoints: int = 256) -> str:
    """Validate one opaque, bounded, control-free string."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip() or _CONTROL_CHARACTERS.search(value) is not None:
        raise ExecutionLedgerDeniedError(f"{name} is malformed")
    if len(value) > max_codepoints:
        raise ExecutionLedgerDeniedError(
            f"{name} exceeds the contracted bound"
        )
    return value


def check_utf8_bound(value: str, *, name: str, limit: int) -> None:
    """Reject a string whose UTF-8 encoding exceeds the contracted bound."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if len(value.encode("utf-8")) > limit:
        raise ExecutionLedgerDeniedError(
            f"{name} exceeds the contracted bound"
        )
