"""Decision-time visible daily closes in one declared share unit; no cash TR claim."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Context, Decimal, localcontext
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from quantforge.data import logical_content_hash

from .share_adjustments import ShareAdjustment
from .trend_refinement import _frame

ZONE = ZoneInfo("Asia/Shanghai")


def causal_close_window(
    bars: pl.DataFrame,
    *,
    as_of: datetime,
    lookback: int,
    adjustments: tuple[ShareAdjustment, ...],
) -> dict[str, Any]:
    """Historical metadata is not PIT-certified; only declared visible sessions enter."""
    if (
        not isinstance(as_of, datetime)
        or as_of.tzinfo is None
        or as_of.utcoffset() is None
        or type(lookback) is not int
        or not 1 <= lookback <= 1000
    ):
        raise ValueError("aware decision time and bounded close window required")
    # Daily timestamps label sessions, not close availability. Do not use today's
    # close in a morning decision merely because its bar label is UTC midnight.
    eligible = [
        i
        for i, stamp in enumerate(bars["timestamp"])
        if datetime.combine(stamp.date(), time(16), ZONE) <= as_of
    ]
    if len(eligible) < lookback:
        raise ValueError("insufficient closed and declared available sessions")
    visible = _frame(bars[eligible])
    sessions = [stamp.date() for stamp in visible["timestamp"]]
    if len(sessions) != len(set(sessions)):
        raise ValueError("one close per daily session required")
    symbol = str(visible["symbol"][0])
    if any(event.symbol != symbol for event in adjustments):
        raise ValueError("matching share unit metadata required")
    if len({event.record_session for event in adjustments}) != len(adjustments):
        raise ValueError("conflicting share unit transitions")
    reference = sessions[-1]
    usable = tuple(
        event
        for event in adjustments
        if event.available_from <= as_of and event.quotes_from <= reference
    )
    if any(not event.verified for event in usable):
        raise ValueError("unverified visible share unit transition")
    window = visible.tail(lookback)
    values, provenance = [], []
    with localcontext(Context(prec=50)):
        for stamp, raw in zip(window["timestamp"], window["close"], strict=True):
            day: date = stamp.date()
            factor = Decimal(1)
            refs = []
            for event in sorted(usable, key=lambda e: e.quotes_from):
                if day < event.quotes_from:
                    factor *= event.ratio
                    refs.append(event.event_id)
            normalized = Decimal(str(raw)) / factor
            values.append(str(normalized))
            provenance.append(
                {
                    "session": day.isoformat(),
                    "raw_close": str(raw),
                    "unit_factor": str(factor),
                    "event_refs": refs,
                    "available_at": datetime.combine(day, time(16), ZONE).isoformat(),
                }
            )
    return {
        "schema_version": "causal-unit-close-window-v1",
        "symbol": symbol,
        "as_of": as_of.astimezone(UTC).isoformat(),
        "unit_reference_session": reference.isoformat(),
        "price_basis": "CAUSAL_SHARE_UNIT_NORMALIZED_PRICE_NOT_CASH_TOTAL_RETURN",
        "closes": values,
        "provenance": provenance,
        "visible_source_hash": logical_content_hash(visible),
        "total_return_qualified": False,
        "pit_source_revision_qualified": False,
        "integrity_scope": "VISIBLE_ROWS_ONLY; WHOLE_SNAPSHOT_INGESTION_IS_SEPARATE",
        "availability": "DECLARED_DAILY_16_00_SHANGHAI_NOT_EXCHANGE_ATTESTATION",
    }


def unit_momentum(
    bars: pl.DataFrame,
    *,
    as_of: datetime,
    lookback: int,
    threshold_pct: Decimal,
    adjustments: tuple[ShareAdjustment, ...],
) -> dict[str, Any]:
    if type(lookback) is not int or not 1 <= lookback <= 999:
        raise ValueError("bounded integer momentum lookback required")
    if (
        not isinstance(threshold_pct, Decimal)
        or not threshold_pct.is_finite()
        or not 0 <= threshold_pct <= 5
    ):
        raise ValueError("exact bounded momentum threshold in percent required")
    window = causal_close_window(
        bars, as_of=as_of, lookback=lookback + 1, adjustments=adjustments
    )
    with localcontext(Context(prec=50)):
        change = (
            Decimal(window["closes"][-1]) / Decimal(window["closes"][0]) - 1
        ) * 100
    return {
        "schema_version": "causal-unit-momentum-intent-v1",
        "window": window,
        "lookback": lookback,
        "threshold_pct": str(threshold_pct),
        "change_pct": str(change),
        "value": int(change > threshold_pct),
        "execution": "NONE_INTENT_ONLY_NOT_BACKTEST001_OR_FILLS",
    }
