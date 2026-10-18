"""Immutable-source market data audit; documents classify gaps, never add prices."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import polars as pl

from quantforge.data import logical_content_hash, prepare_bars

from .source_urls import bounded_https_source

_SHA = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class MarketSource:
    provider: str
    endpoint: str
    retrieved_at: datetime
    raw_sha256: str
    price_basis: Literal["RAW_UNADJUSTED", "DECLARED_ADJUSTED", "UNKNOWN"]
    volume_unit: Literal["LOTS", "SHARES", "UNKNOWN"]
    evidence_level: Literal["VENDOR_RESPONSE", "OPERATOR_DECLARATION"]

    def __post_init__(self) -> None:
        if (
            not self.provider.strip()
            or not bounded_https_source(self.endpoint)
            or not _SHA.fullmatch(self.raw_sha256)
        ):
            raise ValueError("bounded HTTPS source identity required")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("aware retrieval timestamp required")
        if self.price_basis not in {"RAW_UNADJUSTED", "DECLARED_ADJUSTED", "UNKNOWN"}:
            raise ValueError("declared price basis required")
        if self.volume_unit not in {"LOTS", "SHARES", "UNKNOWN"}:
            raise ValueError("declared volume units required")
        if self.evidence_level not in {"VENDOR_RESPONSE", "OPERATOR_DECLARATION"}:
            raise ValueError("supported source evidence level required")

    def to_json(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "retrieved_at": self.retrieved_at.astimezone(UTC).isoformat(),
            "exchange_authenticated": False,
        }


@dataclass(frozen=True)
class SessionEvidence:
    symbol: str
    session: date
    kind: Literal["DOCUMENTED_SUSPENSION", "PARTIAL_SESSION", "UNKNOWN"]
    source_url: str
    source_sha256: str
    published_date: date
    precision: Literal["DATE_ONLY"] = "DATE_ONLY"

    def __post_init__(self) -> None:
        if (
            not self.symbol
            or self.kind
            not in {
                "DOCUMENTED_SUSPENSION",
                "PARTIAL_SESSION",
                "UNKNOWN",
            }
            or not bounded_https_source(self.source_url)
            or not _SHA.fullmatch(self.source_sha256)
            or self.precision != "DATE_ONLY"
        ):
            raise ValueError("source-bound session evidence required")

    def to_json(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "session": self.session.isoformat(),
            "published_date": self.published_date.isoformat(),
            "intraday_availability_verified": False,
        }


def _dates(frame: pl.DataFrame) -> list[str]:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return [
        (epoch + timedelta(microseconds=int(value))).date().isoformat()
        for value in frame["timestamp"].cast(pl.Int64)
    ]


def audit_bars(
    bars: pl.DataFrame,
    *,
    symbol: str,
    source: MarketSource,
    reference_sessions: list[str],
    session_evidence: tuple[SessionEvidence, ...] = (),
    jump_limit: Decimal = Decimal("0.20"),
) -> dict[str, Any]:
    if (
        not isinstance(source, MarketSource)
        or not symbol
        or not isinstance(jump_limit, Decimal)
        or not jump_limit.is_finite()
        or not 0 < jump_limit < 1
    ):
        raise ValueError("explicit source and finite audit policy required")
    references = sorted(
        {date.fromisoformat(value).isoformat() for value in reference_sessions}
    )
    issues: list[dict[str, Any]] = []
    try:
        frame = prepare_bars(bars, as_of=source.retrieved_at)
        if frame.is_empty() or frame["symbol"].unique().to_list() != [symbol]:
            raise ValueError("nonempty matching symbol required")
        sessions = _dates(frame)
        if len(set(sessions)) != len(sessions):
            raise ValueError("one bar per session required")
    except (ValueError, TypeError):
        return {
            "schema_version": "market-quality-v1",
            "symbol": symbol,
            "status": "BLOCKED",
            "source": source.to_json(),
            "issues": [{"severity": "BLOCK", "code": "INVALID_OR_CORRUPT_BARS"}],
            "price_research_allowed": False,
            "total_return_qualified": False,
            "live_eligible": False,
        }
    covered = {value for value in references if sessions[0] <= value <= sessions[-1]}
    missing = sorted(covered - set(sessions))
    known = {
        value.session.isoformat(): value
        for value in session_evidence
        if value.symbol == symbol
    }
    if len(known) != sum(value.symbol == symbol for value in session_evidence):
        raise ValueError("conflicting session evidence")
    gaps = []
    for session in missing:
        proof = known.get(session)
        classification = proof.kind if proof else "UNKNOWN"
        gaps.append(
            {
                "session": session,
                "classification": classification,
                "evidence": proof.to_json() if proof else None,
                "filled": False,
            }
        )
        if classification != "DOCUMENTED_SUSPENSION":
            issues.append(
                {
                    "severity": "REVIEW",
                    "code": "UNEXPLAINED_SESSION_GAP",
                    "session": session,
                }
            )
    partial = [
        value.to_json()
        for value in session_evidence
        if value.symbol == symbol
        and value.kind == "PARTIAL_SESSION"
        and value.session.isoformat() in sessions
    ]
    for value in partial:
        issues.append(
            {
                "severity": "REVIEW",
                "code": "INTRADAY_OPEN_NOT_QUALIFIED",
                "session": value["session"],
            }
        )
    jumps = []
    prices = [Decimal(str(value)) for value in frame["close"]]
    for index in range(1, len(prices)):
        change = prices[index] / prices[index - 1] - 1
        if abs(change) > jump_limit:
            jumps.append({"session": sessions[index], "return": str(change)})
    if jumps:
        issues.append(
            {"severity": "REVIEW", "code": "LARGE_PRICE_CHANGE", "count": len(jumps)}
        )
    zero_volume = [
        sessions[index] for index, value in enumerate(frame["volume"]) if value == 0
    ]
    if zero_volume:
        issues.append(
            {"severity": "REVIEW", "code": "ZERO_VOLUME", "count": len(zero_volume)}
        )
    if source.price_basis == "UNKNOWN":
        issues.append({"severity": "BLOCK", "code": "PRICE_BASIS_UNKNOWN"})
    if source.volume_unit == "UNKNOWN":
        issues.append({"severity": "REVIEW", "code": "VOLUME_UNIT_UNKNOWN"})
    if frame.height < 80:
        issues.append({"severity": "BLOCK", "code": "INSUFFICIENT_RESEARCH_ROWS"})
    blocked = any(value["severity"] == "BLOCK" for value in issues)
    return {
        "schema_version": "market-quality-v1",
        "symbol": symbol,
        "status": "BLOCKED"
        if blocked
        else "REVIEW_REQUIRED"
        if issues
        else "EXPLORATORY_PRICE_READY",
        "source": source.to_json(),
        "content_hash": logical_content_hash(frame),
        "observations": frame.height,
        "start": sessions[0],
        "end": sessions[-1],
        "sessions": sessions,
        "missing_sessions": gaps,
        "partial_sessions": partial,
        "large_changes": jumps,
        "zero_volume_sessions": zero_volume,
        "audit_policy": {"absolute_close_return_review_limit": str(jump_limit)},
        "issues": issues,
        "price_research_allowed": not blocked,
        "total_return_qualified": False,
        "live_eligible": False,
        "limitations": [
            "Reference sessions are supplied data, not a certified exchange calendar.",
            "Evidence classification is source-bound host attestation; no price imputation.",
            "Date-only publication does not prove information availability before an open.",
            "Raw or declared-adjusted prices do not establish dividend total-return quality.",
            "Vendor quotes are not exchange-authenticated execution evidence.",
        ],
    }


def compare_prices(
    primary: pl.DataFrame,
    secondary: pl.DataFrame,
    *,
    tolerance: Decimal = Decimal("0.001"),
) -> dict[str, Any]:
    if not isinstance(tolerance, Decimal) or not tolerance.is_finite() or tolerance < 0:
        raise ValueError("exact nonnegative price tolerance required")
    left: dict[tuple[str, str], dict[str, Decimal]] = {}
    right: dict[tuple[str, str], dict[str, Decimal]] = {}
    for frame, target in ((primary, left), (secondary, right)):
        canonical = prepare_bars(frame, as_of=datetime.max.replace(tzinfo=UTC))
        dates = _dates(canonical)
        for index, day in enumerate(dates):
            key = (str(canonical["symbol"][index]), day)
            if key in target:
                raise ValueError("duplicate comparison session")
            target[key] = {
                name: Decimal(str(canonical[name][index]))
                for name in ("open", "high", "low", "close")
            }
    common = sorted(set(left) & set(right))
    discrepancies = []
    for key in common:
        for field in left[key]:
            difference = abs(left[key][field] - right[key][field])
            if difference > tolerance:
                discrepancies.append(
                    {
                        "symbol": key[0],
                        "session": key[1],
                        "field": field,
                        "primary": str(left[key][field]),
                        "secondary": str(right[key][field]),
                        "absolute_difference": str(difference),
                    }
                )
    return {
        "schema_version": "cross-source-prices-v1",
        "status": "NO_OVERLAP"
        if not common
        else "DISCREPANCIES"
        if discrepancies
        else "MATCHED_WITHIN_TOLERANCE",
        "overlap_sessions": len(common),
        "tolerance": str(tolerance),
        "discrepancies": discrepancies,
        "primary_only": [
            {"symbol": key[0], "session": key[1]}
            for key in sorted(set(left) - set(right))
        ],
        "secondary_only": [
            {"symbol": key[0], "session": key[1]}
            for key in sorted(set(right) - set(left))
        ],
        "volume_compared": False,
        "exchange_authenticated": False,
    }
