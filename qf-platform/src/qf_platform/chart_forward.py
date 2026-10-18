"""One-source-per-idle-tick forward facts; no models, messaging or historical fills."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
from quantforge.product.context import source_identity
from quantforge.product.storage import local_root, writer_lock
from quantforge.strategies.chart_evaluation import (
    assess_observation,
    classification_metrics,
    freeze_target,
)
from quantforge.strategies.chart_quality import is_session
from quantforge.strategies.chart_synthesis import synthesize
from quantforge.strategies.daily_experiments import relative_confirmation
from quantforge.strategies.daily_five import prediction, prepare_inputs

from . import chart_algorithms as market
from .chart_market_cache import cached_fetch
from .chart_timeframes import ZONE, cutoff_at, fetch_period, parse_period
from .history_cache import runtime_identity
from .storage import Ledger, PlatformError, digest

SYMBOLS = (
    "510300",
    "600519",
    "000001",
    "600000",
    "000002",
    "600036",
    "601318",
    "600030",
    "601166",
    "601398",
    "601857",
    "600028",
    "000333",
    "000651",
    "002415",
    "510050",
    "600276",
    "600309",
    "600900",
    "601012",
    "002594",
    "300750",
    "159915",
    "588000",
)
METHODS = ("BASELINE_V2", "REGIME_ROUTER", "RELATIVE_CONFIRMATION")


def binding() -> dict[str, str]:
    return {
        **{"runtime_" + key: value for key, value in runtime_identity().items()},
        "core": source_identity(),
        **{
            name: hashlib.sha256(
                Path(__file__).with_name(name + ".py").read_bytes()
            ).hexdigest()
            for name in (
                "chart_forward",
                "chart_algorithms",
                "chart_timeframes",
                "daily_release",
                "history_cache",
                "chart_market_cache",
                "storage",
            )
        },
    }


class DailyForward:
    def __init__(self, root: Path) -> None:
        directory = local_root(root) / "daily-forward"
        directory.mkdir(exist_ok=True)
        self.root = local_root(directory)
        self.data = directory / "market-data"
        self.data.mkdir(exist_ok=True)
        self.ledger = Ledger(directory)
        self.next_check = 0.0

    def append(self, **value: Any) -> None:
        with self.ledger.transaction() as db:
            self.ledger.append(db, **value)

    def enable(self, *, rebind: bool = False) -> dict[str, Any]:
        with writer_lock(self.root):
            prior = [r for r in self.ledger.read() if r["kind"] == "REGISTRY"]
            if prior and not rebind:
                if prior[-1]["binding"] != binding():
                    raise PlatformError("FORWARD_EXPLICIT_REBIND_REQUIRED")
                return prior[-1]
            at = datetime.now(UTC)
            value = {
                "kind": "REGISTRY",
                "generation": digest({"at": at.isoformat(), "binding": binding()}),
                "binding": binding(),
                "enabled_at": at.isoformat(),
                "symbols": SYMBOLS,
                "methods": METHODS,
                "horizon_bars": 5,
                "model_calls": 0,
                "maximum_bytes": 512 * 1024 * 1024,
                "predecessor": prior[-1]["generation"] if prior else None,
            }
            self.append(**value)
            return value

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        rows = self.ledger.read()
        registries = [r for r in rows if r["kind"] == "REGISTRY"]
        reg = registries[-1] if registries else None
        facts = [r for r in rows if reg and r.get("generation") == reg["generation"]]
        forecasts = [r for r in facts if r["kind"] == "PREDICTION_FROZEN"]
        scores = [r for r in facts if r["kind"] == "SCORE_RECORDED"]
        matured = [
            r
            for r in scores
            if r["outcome"]["status"] == "MATURED"
            and r["outcome"]["kind"] == "FORWARD_FROZEN"
        ]
        lookup = {r["prediction_id"]: r for r in forecasts}
        current_day = (now or datetime.now(UTC)).astimezone(ZONE).date()
        storage = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        return {
            "schema_version": "daily-forward-status-v1",
            "status": "DISABLED"
            if reg is None
            else "PAUSED_PROCESSING_FAILED"
            if any(r["kind"] == "PROCESSING_FAILED" for r in facts)
            else "PAUSED_CALENDAR_UNVERIFIED"
            if is_session(current_day, current_day) is None
            else "PAUSED_STORAGE_LIMIT"
            if storage > reg["maximum_bytes"] - 2 * 1024 * 1024
            else "ACTIVE"
            if reg["binding"] == binding()
            else "PAUSED_CODE_CHANGED",
            "enabled_at": reg["enabled_at"] if reg else None,
            "generation": reg["generation"] if reg else None,
            "frozen": len(forecasts),
            "matured": len(matured),
            "pending": len(forecasts) - len(scores),
            "missed_days": sum(r["kind"] == "DAY_MISSED" for r in facts),
            "failed_sources": sum(r["kind"] == "SOURCE_FAILED" for r in facts),
            "metrics": {
                kind: {
                    method: classification_metrics(
                        [
                            r["outcome"]["actual"]
                            for r in matured
                            if r["method"] == method
                            and r["symbol"].startswith(("5", "15", "16"))
                            == (kind == "ETF")
                        ],
                        [
                            lookup[r["prediction_id"]]["target"]["direction"]
                            for r in matured
                            if r["method"] == method
                            and r["symbol"].startswith(("5", "15", "16"))
                            == (kind == "ETF")
                        ],
                    )
                    for method in METHODS
                }
                for kind in ("STOCK", "ETF")
            },
            "model_calls": 0,
            "storage_bytes": storage,
            "maximum_bytes": reg["maximum_bytes"] if reg else None,
            "evaluation_basis": "RAW_UNADJUSTED_NOT_TOTAL_RETURN",
            "execution_qualified": False,
            "forecast_validated": False,
            "note": (
                "只统计启用后实际按时落盘的判断；缺失不补造。"
                "股票与ETF分别统计，原始明细保留；"
                "当前汇总仅为记录进度，不是发布验收。"
            ),
        }

    def _frame(self, source: dict[str, Any]) -> Any:
        path = self.data / (source["raw_sha256"] + ".json")
        if (
            path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != source["raw_sha256"]
        ):
            raise ValueError("Forward raw source differs")
        start, end = (
            date.fromisoformat(source["start"]),
            datetime.fromisoformat(source["cutoff"]),
        )
        raw = path.read_bytes()
        code = market.symbol_code(source["symbol"])
        return (
            market.parse_daily(raw, code, start, end.date())
            if source["timeframe"] == "1d"
            else parse_period(raw, code, start, end, "1w")[0]
        )

    def idle_tick(self) -> bool:
        """A forward-data fault must not stop unrelated chart/personal work."""
        try:
            return self.tick()
        except (
            ValueError,
            OSError,
            KeyError,
            TypeError,
            sqlite3.Error,
            pl.exceptions.PolarsError,
        ) as error:
            self.next_check = time.monotonic() + 300
            try:
                with writer_lock(self.root):
                    registries = [
                        r for r in self.ledger.read() if r["kind"] == "REGISTRY"
                    ]
                    if registries:
                        self.append(
                            kind="PROCESSING_FAILED",
                            generation=registries[-1]["generation"],
                            reason=type(error).__name__,
                        )
            except (ValueError, OSError, sqlite3.Error):
                pass
            return False

    def tick(self, *, now: datetime | None = None) -> bool:
        if now is None and time.monotonic() < self.next_check:
            return False
        self.next_check = time.monotonic() + 60
        at = now or datetime.now(UTC)
        if at.tzinfo is None:
            raise ValueError("aware forward clock required")
        with writer_lock(self.root):
            rows = self.ledger.read()
            registries = [r for r in rows if r["kind"] == "REGISTRY"]
            if not registries or registries[-1]["binding"] != binding():
                return False
            reg = registries[-1]
            events = [r for r in rows if r.get("generation") == reg["generation"]]
            if any(r["kind"] == "PROCESSING_FAILED" for r in events):
                return False
            common = {"generation": reg["generation"]}
            if (
                sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
                > reg["maximum_bytes"] - 2 * 1024 * 1024
            ):
                return False
            local = at.astimezone(ZONE)
            today = local.date()
            previous = max(
                [
                    date.fromisoformat(r["day"])
                    for r in events
                    if r["kind"] == "DAY_MISSED"
                ]
                + [
                    datetime.fromisoformat(reg["enabled_at"]).astimezone(ZONE).date()
                    - timedelta(days=1)
                ]
            )
            day = previous + timedelta(days=1)
            while day < today:
                if is_session(day, today) and not any(
                    r["kind"] == "DAY_COMPLETE" and r["day"] == day.isoformat()
                    for r in events
                ):
                    self.append(
                        kind="DAY_MISSED",
                        day=day.isoformat(),
                        reason="Missed original recording window; no backfill",
                        **common,
                    )
                    return True
                day += timedelta(days=1)
            if is_session(today, today) is not True or not day_time(
                16, 5
            ) <= local.time().replace(tzinfo=None) < day_time(23):
                return False
            day_id = today.isoformat()
            todays = [r for r in events if r.get("day") == day_id]
            if any(r["kind"] == "DAY_COMPLETE" for r in todays):
                return False
            sources = {
                (r["symbol"], r["timeframe"]): r
                for r in todays
                if r["kind"] == "SOURCE_SAVED"
            }
            for symbol in SYMBOLS:
                for tf in ("1d", "1w"):
                    key = (symbol, tf)
                    if key in sources or any(
                        r["kind"] == "SOURCE_FAILED"
                        and (r["symbol"], r["timeframe"]) == key
                        for r in todays
                    ):
                        continue
                    if any(
                        r["kind"] == "SOURCE_RESERVED"
                        and (r["symbol"], r["timeframe"]) == key
                        for r in todays
                    ):
                        self.append(
                            kind="SOURCE_FAILED",
                            day=day_id,
                            symbol=symbol,
                            timeframe=tf,
                            reason="RECOVERY_UNKNOWN_NO_AUTOMATIC_REPLAY",
                            **common,
                        )
                        return True
                    self.append(
                        kind="SOURCE_RESERVED",
                        day=day_id,
                        symbol=symbol,
                        timeframe=tf,
                        **common,
                    )
                    cutoff = cutoff_at({"timeframe": tf, "period_end": day_id}, at)
                    start = today - timedelta(days=1200 if tf == "1d" else 4500)
                    code = market.symbol_code(symbol)

                    def fetch_source(
                        code: str = code,
                        start: date = start,
                        cutoff: datetime = cutoff,
                        tf: str = tf,
                    ) -> tuple[bytes, str]:
                        return (
                            market.fetch_daily(code, start, cutoff.date())
                            if tf == "1d"
                            else fetch_period(code, start, cutoff, tf)
                        )

                    failed_source = None
                    try:
                        raw, url, retrieved, _ = cached_fetch(
                            self.data,
                            {
                                "forward_day": day_id,
                                "symbol": symbol,
                                "timeframe": tf,
                                "cutoff": cutoff.isoformat(),
                            },
                            fetch_source,
                        )
                        source = {
                            "adjustment": "RAW",
                            "volume_unit": "VENDOR_UNVERIFIED",
                            "corporate_actions_verified": False,
                            "day": day_id,
                            "symbol": symbol,
                            "timeframe": tf,
                            "start": start.isoformat(),
                            "cutoff": cutoff.isoformat(),
                            "raw_sha256": hashlib.sha256(raw).hexdigest(),
                            "url": url,
                            "retrieved_at": retrieved,
                        }
                        failed_source = source
                        frame = self._frame(source)
                        if (
                            tf == "1d"
                            and frame["timestamp"][-1].astimezone(ZONE).date() != today
                        ):
                            raise ValueError(
                                "Latest daily bar is not today's closed bar"
                            )
                        self.append(kind="SOURCE_SAVED", **source, **common)
                    except (
                        ValueError,
                        OSError,
                        KeyError,
                        TypeError,
                        pl.exceptions.PolarsError,
                    ) as error:
                        self.append(
                            kind="SOURCE_FAILED",
                            day=day_id,
                            symbol=symbol,
                            timeframe=tf,
                            reason=type(error).__name__,
                            source=failed_source,
                            **common,
                        )
                    return True
            for symbol in SYMBOLS:
                if any(
                    r["kind"] == "PREDICTION_BATCH_DONE" and r["symbol"] == symbol
                    for r in todays
                ):
                    continue
                if (symbol, "1d") not in sources or ("510300", "1d") not in sources:
                    self.append(
                        kind="PREDICTION_BATCH_DONE",
                        day=day_id,
                        symbol=symbol,
                        reason="DAILY_SOURCE_UNAVAILABLE",
                        **common,
                    )
                    return True
                frame = self._frame(sources[(symbol, "1d")])
                benchmark = self._frame(sources[("510300", "1d")])
                inputs = prepare_inputs(frame, benchmark, as_of=today)
                if not inputs["features"]:
                    self.append(
                        kind="PREDICTION_BATCH_DONE",
                        day=day_id,
                        symbol=symbol,
                        reason="INSUFFICIENT_HISTORY",
                        **common,
                    )
                    return True
                weekly = (
                    self._frame(sources[(symbol, "1w")])
                    if (symbol, "1w") in sources
                    else None
                )
                contexts = [
                    {
                        "timeframe": "1w",
                        "status": "AVAILABLE" if weekly is not None else "UNAVAILABLE",
                        "synthesis": synthesize(weekly, "1w")
                        if weekly is not None
                        else None,
                    }
                ]
                decisions = {
                    method: prediction(inputs, contexts, method)
                    for method in METHODS[:2]
                }
                decisions["RELATIVE_CONFIRMATION"] = relative_confirmation(
                    decisions["REGIME_ROUTER"], inputs["features"], contexts
                )
                frozen_at = datetime.now(UTC) if now is None else at
                if (
                    frozen_at.astimezone(ZONE).date() != today
                    or frozen_at.astimezone(ZONE).hour >= 23
                ):
                    self.append(
                        kind="PREDICTION_BATCH_DONE",
                        day=day_id,
                        symbol=symbol,
                        reason="MISSED_RECORDING_WINDOW",
                        **common,
                    )
                    return True
                for method, decision in decisions.items():
                    prediction_id = digest(
                        {
                            "generation": reg["generation"],
                            "day": day_id,
                            "symbol": symbol,
                            "method": method,
                        }
                    )
                    if any(
                        r["kind"] == "PREDICTION_FROZEN"
                        and r["prediction_id"] == prediction_id
                        for r in todays
                    ):
                        continue
                    target = freeze_target(
                        direction=decision["direction"],
                        timeframe="1d",
                        horizon=5,
                        origin=frame["timestamp"][-1],
                        reference=Decimal(inputs["features"]["close"]),
                        atr=Decimal(inputs["features"]["atr14"]),
                        frozen_at=frozen_at,
                    )
                    self.append(
                        kind="PREDICTION_FROZEN",
                        day=day_id,
                        symbol=symbol,
                        method=method,
                        prediction_id=prediction_id,
                        target=target,
                        decision=decision,
                        source=sources[(symbol, "1d")],
                        **common,
                    )
                self.append(
                    kind="PREDICTION_BATCH_DONE", day=day_id, symbol=symbol, **common
                )
                return True
            scored = {
                r["prediction_id"] for r in events if r["kind"] == "SCORE_RECORDED"
            }
            for fact in events:
                if (
                    fact["kind"] != "PREDICTION_FROZEN"
                    or fact["prediction_id"] in scored
                    or (fact["symbol"], "1d") not in sources
                ):
                    continue
                frame = self._frame(sources[(fact["symbol"], "1d")])
                outcome = assess_observation(fact["target"], frame, at)
                if outcome["status"] != "PENDING":
                    self.append(
                        kind="SCORE_RECORDED",
                        prediction_id=fact["prediction_id"],
                        method=fact["method"],
                        symbol=fact["symbol"],
                        outcome=outcome,
                        **common,
                    )
                    return True
            self.append(kind="DAY_COMPLETE", day=day_id, **common)
            return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("action", choices=("enable", "status", "tick"))
    parser.add_argument("--rebind", action="store_true")
    args = parser.parse_args()
    app = DailyForward(args.root)
    result = (
        app.enable(rebind=args.rebind)
        if args.action == "enable"
        else app.status()
        if args.action == "status"
        else {"changed": app.tick()}
    )
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
