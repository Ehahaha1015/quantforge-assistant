import json
from datetime import UTC, date, datetime, timedelta

import pytest
from quantforge.strategies.chart_quality import is_session

from qf_platform import chart_forward


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(chart_forward, "SYMBOLS", ("510300", "600519"))
    monkeypatch.setattr(chart_forward, "binding", lambda: {"core": "fixed-fixture"})
    app = chart_forward.DailyForward(tmp_path)
    app.append(
        kind="REGISTRY",
        generation="fixture",
        enabled_at="2026-10-18T08:00:00+00:00",
        binding=chart_forward.binding(),
        symbols=chart_forward.SYMBOLS,
        methods=chart_forward.METHODS,
        maximum_bytes=512 * 1024 * 1024,
    )
    calls = []

    def daily(code, start, end):
        calls.append((code, end))
        days = [
            date(2026, 1, 1) + timedelta(days=i)
            for i in range((end - date(2026, 1, 1)).days + 1)
        ]
        rows = [
            [d.isoformat(), "100", "100", "101", "99", "1000"]
            for d in days
            if is_session(d)
        ]
        return json.dumps(
            {"code": 0, "data": {code: {"day": rows}}}
        ).encode(), "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + code

    def weekly(code, start, cutoff, tf):
        calls.append((code, tf))
        friday = cutoff.date() - timedelta(days=(cutoff.weekday() - 4) % 7)
        rows = [
            [
                (friday - timedelta(weeks=99 - i)).isoformat(),
                "100",
                "100",
                "101",
                "99",
                "1000",
            ]
            for i in range(100)
        ]
        return json.dumps(
            {"code": 0, "data": {code: {"week": rows}}}
        ).encode(), "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + code

    monkeypatch.setattr(chart_forward.market, "fetch_daily", daily)
    monkeypatch.setattr(chart_forward, "fetch_period", weekly)
    return app, calls


def finish_day(app, at):
    for _ in range(60):
        if not app.tick(now=at):
            return
    pytest.fail("Forward day did not converge")


def test_weekend_is_quiet_then_actual_predictions_are_append_only_and_mature(
    tmp_path, monkeypatch
):
    app, calls = setup(tmp_path, monkeypatch)
    assert not app.tick(now=datetime(2026, 10, 18, 9, tzinfo=UTC)) and not calls
    finish_day(app, datetime(2026, 10, 19, 9, tzinfo=UTC))
    before = [r for r in app.ledger.read() if r["kind"] == "PREDICTION_FROZEN"]
    assert len(before) == 6 and len(calls) == 4
    assert all(r["target"]["frozen_at"] == "2026-10-19T09:00:00+00:00" for r in before)
    assert not app.tick(now=datetime(2026, 10, 19, 10, tzinfo=UTC)) and len(calls) == 4
    finish_day(app, datetime(2026, 10, 26, 9, tzinfo=UTC))
    rows = app.ledger.read()
    assert [r for r in rows if r["kind"] == "PREDICTION_FROZEN"][:6] == before
    scores = [r for r in rows if r["kind"] == "SCORE_RECORDED"]
    assert len(scores) == 6 and all(
        r["outcome"]["kind"] == "FORWARD_FROZEN" for r in scores
    )
    assert app.status()["missed_days"] == 4
    assert app.status()["model_calls"] == 0


def test_reserved_unknown_fetch_is_never_automatically_replayed(tmp_path, monkeypatch):
    app, calls = setup(tmp_path, monkeypatch)
    app.append(
        kind="SOURCE_RESERVED",
        generation="fixture",
        day="2026-10-19",
        symbol="510300",
        timeframe="1d",
    )
    assert app.tick(now=datetime(2026, 10, 19, 9, tzinfo=UTC))
    assert not calls
    assert app.ledger.read()[-1]["reason"] == "RECOVERY_UNKNOWN_NO_AUTOMATIC_REPLAY"
    monkeypatch.setattr(chart_forward, "binding", lambda: {"core": "changed"})
    assert not app.tick(now=datetime(2026, 10, 19, 10, tzinfo=UTC))
    assert app.status()["status"] == "PAUSED_CODE_CHANGED"


def test_background_fault_is_paused_without_propagating_to_personal_worker(
    tmp_path, monkeypatch
):
    app, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        app, "tick", lambda: (_ for _ in ()).throw(ValueError("fixture"))
    )
    assert app.idle_tick() is False
    assert app.status()["status"] == "PAUSED_PROCESSING_FAILED"
