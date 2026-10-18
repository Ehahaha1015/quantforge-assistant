from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from quantforge.data import BAR_SCHEMA

from qf_platform import forward_paper as forward
from qf_platform.storage import PlatformError


def quote(
    service: forward.ForwardPaper, moment: datetime, price: float = 4.0
) -> tuple[dict[str, pl.DataFrame], dict[str, dict[str, str]]]:
    session = moment.astimezone(forward.ZONE).date()
    frames, receipts = {}, {}
    sources = service.root / "sources"
    sources.mkdir(exist_ok=True)
    for key in forward.ASSETS:
        raw = json.dumps({"session": session.isoformat(), "price": price}).encode()
        identity = hashlib.sha256(raw).hexdigest()
        path = sources / (key + "-" + identity + ".raw")
        path.write_bytes(raw)
        frames[key] = pl.DataFrame(
            {
                "symbol": [key],
                "timestamp": [
                    datetime.combine(session, datetime.min.time(), tzinfo=UTC)
                ],
                "open": [price],
                "close": [price],
                "high": [price + 1],
                "low": [price - 1],
                "volume": [1e7],
            },
            schema=BAR_SCHEMA,
        )
        receipts[key] = {
            "retrieved_at": (moment - timedelta(seconds=1)).isoformat(),
            "artifact": path.name,
            "raw_sha256": identity,
            "transport": "SOFTWARE_FIXTURE",
        }
    return frames, receipts


def service_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[forward.ForwardPaper, datetime]:
    moment = datetime(2026, 10, 14, 9, tzinfo=UTC)
    monkeypatch.setattr(forward, "_now", lambda: moment)
    service = forward.ForwardPaper(
        tmp_path / "forward", fixture=True, usage_root=tmp_path / "usage"
    )
    service.freeze()
    return service, moment


def test_no_same_day_historical_future_or_before_close_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, moment = service_at(tmp_path, monkeypatch)
    frames, receipts = quote(service, moment)
    with pytest.raises(PlatformError, match="FUTURE_SESSION"):
        service.record_frames(frames, receipts)
    later = moment + timedelta(days=1)
    monkeypatch.setattr(forward, "_now", lambda: later)
    with pytest.raises(PlatformError, match="SAME_DAY"):
        service.record_frames(frames, receipts)
    future_frames, future_receipts = quote(service, later + timedelta(days=1))
    with pytest.raises(ValueError):
        service.record_frames(future_frames, future_receipts)
    monkeypatch.setattr(forward, "_now", lambda: later - timedelta(hours=3))
    with pytest.raises(PlatformError, match="AFTER_CLOSE"):
        service.capture()
    assert service.status()["observations"] == 0


def test_duplicate_has_no_new_effect_revision_latches_and_fixture_never_qualifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, moment = service_at(tmp_path, monkeypatch)
    later = moment + timedelta(days=1)
    monkeypatch.setattr(forward, "_now", lambda: later)
    frames, receipts = quote(service, later)
    assert (
        service.record_frames(frames, receipts)["status"]
        == "PAUSED_CASH_SCOPE_NOT_REVIEWED"
    )
    assert service.record_frames(frames, receipts) == {
        "status": "ALREADY_OBSERVED",
        "new_fills": 0,
    }
    with pytest.raises(PlatformError, match="REVISION"):
        service.record_frames(*quote(service, later, 4.2))
    monkeypatch.setattr(forward, "_now", lambda: later + timedelta(days=1))
    with pytest.raises(PlatformError, match="UNRESOLVED"):
        service.record_frames(*quote(service, later + timedelta(days=1)))
    status = service.status()
    assert status["observations"] == 1 and status["execution_sessions"] == 0
    assert not status["minimum_time_met"] and not status["complete"]
    with pytest.raises(PlatformError, match="MODE"):
        forward.ForwardPaper(service.root, usage_root=tmp_path / "usage").freeze()


def coverage(service: forward.ForwardPaper, start: str, end: str) -> Path:
    source = service.root / "test-review.raw"
    source.write_bytes(b"SOFTWARE_FIXTURE_NOT_REAL_CASH_REVIEW")
    path = service.root / ("coverage-" + end + ".json")
    path.write_text(
        json.dumps(
            {
                "qualification": (
                    "HOST_REVIEWED_KNOWN_CASH_SCOPE_NOT_COMPLETE_CORPORATE_ACTIONS"
                ),
                "covered_start": start,
                "covered_end": end,
                "events": [],
                "reviewed_sources": [
                    {
                        "artifact": source.name,
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
    )
    return path


def test_late_cash_review_does_not_backfill_paused_days_and_expiry_preserves_cash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, moment = service_at(tmp_path, monkeypatch)
    for index in (1, 2):
        later = moment + timedelta(days=index)
        monkeypatch.setattr(forward, "_now", lambda at=later: at)
        service.record_frames(*quote(service, later))
    service.review_cash_scope(coverage(service, "2026-10-17", "2026-10-17"))
    later = moment + timedelta(days=3)
    monkeypatch.setattr(forward, "_now", lambda: later)
    assert service.record_frames(*quote(service, later))["new_fills"] == 3
    history = service.ledger.read()
    old_cash = history[-1]["state"]["period"]["cash"]
    assert service.status()["execution_sessions"] == 1
    later = moment + timedelta(days=4)
    monkeypatch.setattr(forward, "_now", lambda: later)
    assert service.record_frames(*quote(service, later))["new_fills"] == 0
    assert service.ledger.read()[-1]["state"]["cash"] == old_cash


def test_network_failure_and_source_identity_change_leave_no_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, moment = service_at(tmp_path, monkeypatch)
    later = moment + timedelta(days=1)
    monkeypatch.setattr(forward, "_now", lambda: later)

    def failure(*args: object) -> object:
        raise OSError("fixture")

    monkeypatch.setattr(forward, "fetch_source", failure)
    with pytest.raises(PlatformError, match="CAPTURE_PAUSED"):
        service.capture()
    monkeypatch.setattr(forward, "source_identity", lambda: "0" * 64)
    with pytest.raises(PlatformError, match="FROZEN_SOURCE"):
        service.record_frames(*quote(service, later))
    assert service.status()["observations"] == 0


def test_freeze_recovers_missing_export_but_rejects_changed_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = service_at(tmp_path, monkeypatch)
    path = service.root / "plan.json"
    original = path.read_bytes()
    path.unlink()
    service.freeze()
    assert path.read_bytes() == original
    path.write_text("{}")
    with pytest.raises(PlatformError, match="PLAN_FILE"):
        service.freeze()
