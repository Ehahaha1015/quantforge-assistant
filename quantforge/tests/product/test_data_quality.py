from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
import pytest

from quantforge.product.data_quality import (
    MarketSource,
    SessionEvidence,
    audit_bars,
    compare_prices,
)
from tests.product.test_strategy_optimization import bars


def source(*, basis: str = "RAW_UNADJUSTED") -> MarketSource:
    return MarketSource(
        "TEST",
        "https://data.example/quotes",
        datetime(2026, 10, 13, tzinfo=UTC),
        "a" * 64,
        basis,
        "LOTS",
        "OPERATOR_DECLARATION",  # type: ignore[arg-type]
    )


def test_documented_suspension_does_not_fabricate_bars() -> None:
    frame = bars(180)
    sessions = frame["timestamp"].dt.date().cast(pl.String).to_list()
    missing = frame.filter(pl.col("timestamp") != frame["timestamp"][20])
    proof = SessionEvidence(
        "TEST",
        date.fromisoformat(sessions[20]),
        "DOCUMENTED_SUSPENSION",
        "https://issuer.example/announcement.pdf",
        "b" * 64,
        date.fromisoformat(sessions[19]),
    )
    report = audit_bars(
        missing,
        symbol="TEST",
        source=source(),
        reference_sessions=sessions,
        session_evidence=(proof,),
    )
    assert report["observations"] == 179
    assert report["missing_sessions"][0]["classification"] == "DOCUMENTED_SUSPENSION"
    assert not report["missing_sessions"][0]["filled"]
    assert report["price_research_allowed"]
    assert not report["total_return_qualified"] and not report["live_eligible"]


def test_unknown_gap_and_partial_session_are_downgraded() -> None:
    frame = bars(180)
    sessions = frame["timestamp"].dt.date().cast(pl.String).to_list()
    missing = frame.filter(pl.col("timestamp") != frame["timestamp"][20])
    proof = SessionEvidence(
        "TEST",
        date.fromisoformat(sessions[21]),
        "PARTIAL_SESSION",
        "https://issuer.example/resumption",
        "b" * 64,
        date.fromisoformat(sessions[21]),
    )
    report = audit_bars(
        missing,
        symbol="TEST",
        source=source(),
        reference_sessions=sessions,
        session_evidence=(proof,),
    )
    assert report["status"] == "REVIEW_REQUIRED"
    assert {value["code"] for value in report["issues"]} == {
        "UNEXPLAINED_SESSION_GAP",
        "INTRADAY_OPEN_NOT_QUALIFIED",
    }
    assert not report["partial_sessions"][0]["intraday_availability_verified"]


@pytest.mark.parametrize("damage", ["nan", "duplicate", "ohlc"])
def test_critical_corruption_blocks_research(damage: str) -> None:
    frame = bars(180)
    if damage == "nan":
        frame = frame.with_columns(pl.lit(float("nan")).alias("close"))
    elif damage == "duplicate":
        frame = pl.concat([frame, frame.head(1)])
    else:
        frame = frame.with_columns(pl.lit(1.0).alias("high"))
    report = audit_bars(frame, symbol="TEST", source=source(), reference_sessions=[])
    assert report["status"] == "BLOCKED"
    assert not report["price_research_allowed"]


def test_unknown_basis_blocks_and_price_jumps_remain_visible() -> None:
    frame = (
        bars(180)
        .with_row_index("index")
        .with_columns(
            *[
                pl.when(pl.col("index") >= 100)
                .then(pl.col(name) * 2)
                .otherwise(pl.col(name))
                .alias(name)
                for name in ("open", "high", "low", "close")
            ]
        )
        .drop("index")
    )
    report = audit_bars(
        frame, symbol="TEST", source=source(basis="UNKNOWN"), reference_sessions=[]
    )
    assert report["status"] == "BLOCKED"
    assert len(report["large_changes"]) == 1
    assert not report["price_research_allowed"]


def test_cross_source_discrepancies_and_no_price_imputation() -> None:
    primary = bars(180)
    secondary = primary.slice(20, 5).with_columns(pl.col("close") + 0.01)
    result = compare_prices(primary, secondary)
    assert result["overlap_sessions"] == 5
    assert len(result["discrepancies"]) == 5
    assert len(result["primary_only"]) == 175
    assert result["status"] == "DISCREPANCIES"
    assert not result["volume_compared"] and not result["exchange_authenticated"]
    assert compare_prices(primary, primary)["status"] == "MATCHED_WITHIN_TOLERANCE"


def test_invalid_source_identity_and_audit_policy() -> None:
    with pytest.raises(ValueError):
        MarketSource(
            "BAD",
            "http://data.example",
            datetime.now(UTC),
            "a" * 64,
            "RAW_UNADJUSTED",
            "LOTS",
            "VENDOR_RESPONSE",
        )
    with pytest.raises(ValueError):
        audit_bars(
            bars(180),
            symbol="TEST",
            source=source(),
            reference_sessions=[],
            jump_limit=Decimal("NaN"),
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://",
        "https://user:password@data.example/x",
        "https://data.example/x#secret",
        "https://data.example/\nquotes",
        "http://data.example/x",
        "not-a-url",
    ],
)
def test_source_identity_uses_structured_parsing_without_transport(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError):
        replace(source(), endpoint=endpoint)
