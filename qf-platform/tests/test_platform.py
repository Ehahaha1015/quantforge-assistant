from __future__ import annotations

import concurrent.futures
import http.client
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from qf_platform.server import make_server
from qf_platform.service import PlatformService, initial_state, money
from qf_platform.storage import PlatformError


@pytest.fixture
def app(tmp_path: Path) -> PlatformService:
    return PlatformService(tmp_path)


def test_money_rejects_lossy_and_non_money_input() -> None:
    for value in (100.0, True, "NaN", "1e3", "-1", "0", "1.001", " 1", None):
        with pytest.raises(PlatformError):
            money(value)


@pytest.mark.parametrize("operation", ["payment-create", "payment-confirm"])
def test_retired_payment_operations_have_no_effect(
    app: PlatformService, operation: str
) -> None:
    before = app.snapshot()
    with pytest.raises(PlatformError, match="OPERATION_NOT_SUPPORTED"):
        app.operate(operation, {}, "retired-payment")
    after = app.snapshot()
    assert after["version"] == before["version"]
    assert "payment_wallet" not in after
    assert "payment_orders" not in after


def test_funds_routes_and_atomic_overspend(app: PlatformService) -> None:
    funded = app.operate(
        "transfer", {"from": "bank", "to": "sim-a", "amount": "500"}, "t1"
    )
    assert funded["bank_cash"] == "9500.00"
    assert funded["brokers"][0]["cash"] == "500.00"
    assert funded["total_equity"] == "10000.00"
    before = funded["version"]
    for source, target, cash in (
        ("wallet", "sim-a", "1"),
        ("sim-a", "sim-b", "1"),
        ("bank", "real-account", "1"),
        ("bank", "sim-a", "10000"),
    ):
        with pytest.raises(PlatformError):
            app.operate(
                "transfer", {"from": source, "to": target, "amount": cash}, "bad"
            )
        assert app.snapshot()["version"] == before
    returned = app.operate(
        "transfer", {"from": "sim-a", "to": "bank", "amount": "500"}, "t2"
    )
    assert returned["bank_cash"] == "10000.00"


def test_legacy_wallet_history_survives_without_affecting_trading_equity(
    app: PlatformService, tmp_path: Path
) -> None:
    state = initial_state()
    state.update(
        payment_wallet="100.00", payment_orders={}, external_funding="10100.00"
    )
    with app.ledger.transaction() as db:
        app._emit(db, state, "LEGACY_FIXTURE", "Retained legacy funding")
    restarted = PlatformService(tmp_path)
    before = restarted.snapshot()
    assert before["total_equity"] == "10000.00"
    assert before["external_funding"] == "10000.00"
    assert before["net_pnl"] == "0.00"
    after = restarted.operate(
        "transfer", {"from": "bank", "to": "sim-a", "amount": "500"}, "legacy-transfer"
    )
    assert after["total_equity"] == "10000.00"
    assert restarted.ledger.read()[1]["state"]["payment_wallet"] == "100.00"


def test_research_to_multi_broker_fill_pause_and_exit(app: PlatformService) -> None:
    entered = app.operate("cycle", {}, "research1")
    assert entered["fills_total"] == 2
    assert {fill["side"] for fill in entered["fills"]} == {"BUY"}
    assert {fill["account"] for fill in entered["fills"]} == {"sim-a", "sim-b"}
    assert entered["cumulative_fees"] == "10.00"
    assert entered["total_equity"] == "9990.00"
    assert entered["paid_model_calls"] == 0
    assert all("E" not in fill["quantity"] for fill in entered["fills"])
    assert all(
        "E" not in broker["positions"][0]["quantity"] for broker in entered["brokers"]
    )
    assert not entered["research"][0]["real_model_call"]
    assert len(entered["research"][0]["decisions"]) == 3
    assert all(Decimal(fill["quantity"]) % 100 == 0 for fill in entered["fills"])
    assert app.operate("cycle", {}, "research1")["version"] == entered["version"]
    app.operate("pause", {"paused": True}, "pause")
    exited = app.operate("cycle", {}, "research2")
    assert exited["fills_total"] == 4
    assert {fill["side"] for fill in exited["fills"][-2:]} == {"SELL"}
    assert all(not broker["positions"] for broker in exited["brokers"])
    withheld = app.operate("cycle", {}, "research3")
    assert withheld["fills_total"] == 4
    assert withheld["cumulative_fees"] == "20.00"
    assert withheld["real_transport"] == "LOCKED"


def test_global_cap_cannot_be_replaced_by_account_approval(
    app: PlatformService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("qf_platform.service._GROSS_CAP", Decimal("1000"))
    rejected = app.operate("cycle", {}, "cap")
    assert rejected["fills_total"] == 0
    assert any(event["kind"] == "ORDER_REJECTED" for event in rejected["events"])
    assert rejected["total_equity"] == "10000.00"


def test_concurrent_duplicate_requests_send_one_simulated_cycle(
    app: PlatformService,
) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda _: app.operate("cycle", {}, "same-key"), range(4))
        )
    assert all(item["fills_total"] == 2 for item in results)
    assert {item["version"] for item in results} == {results[0]["version"]}
    assert len(list((app.ledger.root / "research").iterdir())) == 1
    with sqlite3.connect(app.ledger.root / "research/same-key/calls.sqlite") as db:
        assert (
            db.execute("SELECT COUNT(*) FROM calls WHERE phase='REQUEST'").fetchone()[0]
            == 3
        )


def test_restart_restores_funds_and_does_not_replay(app: PlatformService) -> None:
    before = app.operate("cycle", {}, "original")
    restarted = PlatformService(app.ledger.root)
    after = restarted.operate("cycle", {}, "original")
    for field in ("version", "ledger_hash", "total_equity", "fills_total", "brokers"):
        assert after[field] == before[field]


def test_incomplete_research_attempt_is_not_blindly_retried(
    app: PlatformService,
) -> None:
    (app.ledger.root / "research/interrupted").mkdir(parents=True)
    with pytest.raises(PlatformError, match="REQUIRES_REVIEW"):
        app.operate("cycle", {}, "interrupted")
    assert app.snapshot()["fills_total"] == 0


def test_backend_autonomy_survives_restart_and_completes_its_budget(
    app: PlatformService,
) -> None:
    app.operate(
        "autonomy", {"enabled": True, "rounds": 2, "interval_seconds": 6}, "batch"
    )
    restarted = PlatformService(app.ledger.root)
    assert restarted.autonomous_tick()
    assert not restarted.autonomous_tick()
    assert restarted.snapshot()["cycle"] == 1
    assert restarted.autonomous_tick(now=datetime.now(UTC) + timedelta(seconds=7))
    final = restarted.snapshot()
    assert final["cycle"] == 2
    assert final["autonomy"]["status"] == "COMPLETED"
    assert final["autonomy"]["remaining"] == 0
    assert final["fills_total"] == 4
    assert not restarted.autonomous_tick(now=datetime.now(UTC) + timedelta(days=1))


def test_backend_autonomy_stops_on_interrupted_attempt(app: PlatformService) -> None:
    app.operate(
        "autonomy", {"enabled": True, "rounds": 1, "interval_seconds": 6}, "batch"
    )
    state = app.ledger.read()[-1]["state"]
    (app.ledger.root / "research" / app._auto_key(state)).mkdir(parents=True)
    assert not app.autonomous_tick()
    assert app.snapshot()["autonomy"]["status"] == "BLOCKED"
    assert app.snapshot()["fills_total"] == 0


def test_stop_rechecked_inside_scheduler_transaction(
    app: PlatformService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.operate(
        "autonomy", {"enabled": True, "rounds": 1, "interval_seconds": 6}, "batch"
    )
    original = app.operate

    def stop_before_cycle(operation: str, payload: dict, key: str) -> dict:
        original("autonomy", {"enabled": False}, "stop")
        return original(operation, payload, key)

    monkeypatch.setattr(app, "operate", stop_before_cycle)
    assert not app.autonomous_tick()
    assert app.snapshot()["autonomy"]["status"] == "STOPPED"
    assert app.snapshot()["cycle"] == 0


def test_autonomous_budget_and_manual_cycle_separation(app: PlatformService) -> None:
    with pytest.raises(PlatformError):
        app.operate(
            "autonomy", {"enabled": True, "rounds": 21, "interval_seconds": 6}, "bad"
        )
    app.operate(
        "autonomy", {"enabled": True, "rounds": 1, "interval_seconds": 6}, "batch"
    )
    with pytest.raises(PlatformError, match="BATCH_RUNNING"):
        app.operate("cycle", {}, "manual")
    app.operate("autonomy", {"enabled": False}, "stop")
    assert not app.autonomous_tick()


def test_ledger_integrity_and_append_only(app: PlatformService) -> None:
    with sqlite3.connect(app.ledger.path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM events")
        db.execute("DROP TRIGGER deny_update")
        db.execute("UPDATE events SET hash='forged' WHERE seq=1")
    with pytest.raises(PlatformError, match="INTEGRITY"):
        app.snapshot()


def test_http_boundary_and_event_stream(tmp_path: Path) -> None:
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(
            "POST", "/api/research/cycle", "{}", {"Content-Type": "application/json"}
        )
        denied = connection.getresponse()
        assert denied.status == 403
        denied.read()
        connection.request(
            "GET", "/api/snapshot", headers={"Origin": "https://evil.example"}
        )
        denied = connection.getresponse()
        assert denied.status == 403
        denied.read()
        headers = {
            "Content-Type": "application/json",
            "X-QF-Mode": "SIMULATION",
            "Idempotency-Key": "http-cycle",
        }
        connection.request("POST", "/api/research/cycle", "{}", headers)
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["fills_total"] == 2
        connection.request("GET", "/api/events")
        stream = connection.getresponse()
        assert stream.status == 200
        assert stream.getheader("Content-Type").startswith("text/event-stream")
        assert stream.readline().startswith(b"id: ")
        assert stream.readline() == b"event: state\n"
        assert json.loads(stream.readline().removeprefix(b"data: "))["fills_total"] == 2
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_stream_cap_allowed_test_origin_and_scheduler_shutdown(tmp_path: Path) -> None:
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    clients: list[http.client.HTTPConnection] = []
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        clients.append(client)
        client.request(
            "GET", "/api/snapshot", headers={"Origin": "http://127.0.0.1:4182"}
        )
        response = client.getresponse()
        assert response.status == 200
        assert (
            response.getheader("Access-Control-Allow-Origin") == "http://127.0.0.1:4182"
        )
        response.read()
        for _ in range(8):
            stream = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            clients.append(stream)
            stream.request("GET", "/api/events")
            response = stream.getresponse()
            assert response.status == 200
            assert response.readline().startswith(b"id:")
        client.request("GET", "/api/events")
        capped = client.getresponse()
        assert capped.status == 429
        assert json.loads(capped.read())["error"] == "STREAM_CAPACITY"
    finally:
        for client in clients:
            client.close()
        server.shutdown()
        server.server_close()
        assert not server.scheduler.is_alive()
