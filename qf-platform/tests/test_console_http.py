"""Offline product HTTP qualification; no real providers or financial sends."""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from qf_platform.bindings import BindingRegistry
from qf_platform.research_room import RoomService
from qf_platform.server import make_server
from qf_platform.supervisor import WorkerSupervisor


@pytest.fixture
def console(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    server = make_server(tmp_path, 0)
    server.supervisor = WorkerSupervisor(tmp_path, domains=("research-rooms",))
    server.supervisor.start()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield tmp_path, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def request(
    port: int, path: str, payload: dict[str, Any] | None = None, mode: str = "RESEARCH"
) -> tuple[int, dict[str, Any]]:
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        client.request(
            "GET" if payload is None else "POST",
            path,
            None if payload is None else json.dumps(payload),
            {"Content-Type": "application/json", "X-QF-Mode": mode},
        )
        response = client.getresponse()
        return response.status, json.loads(response.read())
    finally:
        client.close()


def completed(port: int) -> dict[str, Any]:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status, state = request(port, "/api/room")
        assert status == 200
        if not state["worker_running"]:
            # /api/room reads liveness from the dispatch ledger before it reads
            # the room history, so "not running" must arrive together with a
            # terminal status. A finished dispatch beside READY_OR_RUNNING is the
            # inconsistency that used to make this helper flake.
            assert state["status"] != "READY_OR_RUNNING", state
            assert state["status"] == "COMPLETE", state
            return state
        time.sleep(0.1)
    pytest.fail("Bounded offline research did not complete")


def test_retired_payment_routes_and_binding_are_unavailable(
    console: tuple[Path, int],
) -> None:
    root, port = console
    for path in (
        "/api/merchant",
        "/api/merchant/issue",
        "/api/merchant/callback/test",
        "/api/payments/orders",
        "/api/payments/confirm",
    ):
        assert request(port, path)[0] == 404
        assert request(port, path, {}, "SIMULATION")[0] == 404
    assert request(port, "/api/history?domain=merchant")[0] == 409
    assert not (root / "merchant").exists()
    assert (
        request(
            port,
            "/api/bindings/save",
            {
                "metadata": {"kind": "PAYMENT_PROVIDER", "name": "retired"},
                "secrets": {},
            },
            "CONFIGURATION",
        )[0]
        == 409
    )


def test_http_evidence_selection_review_preserves_original_trials(
    console: tuple[Path, int],
) -> None:
    root, port = console
    status, _ = request(
        port, "/api/room/start", {"mode": "SYNTHETIC", "rounds": 1, "calls": 6}
    )
    assert status == 200
    original = completed(port)
    # The same synthetic dataset remains bound; denial cannot replace the room.
    status, failure = request(
        port, "/api/room/start", {"mode": "SYNTHETIC", "rounds": 1, "calls": 6}
    )
    assert status == 409
    assert failure["error"] == "DATASET_ALREADY_BOUND_OR_UNRESOLVED"
    assert request(port, "/api/room")[1]["history"] == original["history"]
    restarted = RoomService(root, BindingRegistry(root))
    assert restarted.snapshot()["history"] == original["history"]
    references = [r for r in original["history"] if r["kind"] == "EVIDENCE_REFERENCE"]
    assert references
    run = references[0]["run_id"]
    status, evidence = request(port, f"/api/room/evidence?run_id={run}")
    assert status == 200
    assert evidence["mode"] == "SYNTHETIC"
    assert evidence["live_authority"] == "NONE"
    for query in ("run_id=not-owned", f"run_id={run}&run_id={run}", "file=../lockbox"):
        assert request(port, f"/api/room/evidence?{query}")[0] == 409
    assert (
        request(port, "/api/room/select", {"program_id": "../research-core"})[0] == 409
    )
    assert request(port, "/api/room/start", {}, "OPERATIONS")[0] == 403
    status, _ = request(
        port,
        "/api/room/start",
        {"mode": "SYNTHETIC", "review_run_id": run, "rounds": 1, "calls": 3},
    )
    assert status == 200
    review = completed(port)
    assert review["reviews_completed"] == 1
    assert review["budget"]["spent_calls"] == 3
    assert not any(r["kind"] == "EVIDENCE_REFERENCE" for r in review["history"])
    assert request(port, f"/api/room/evidence?run_id={run}")[0] == 200
    status, retained = request(
        port, "/api/room/select", {"program_id": original["declaration"]["program_id"]}
    )
    assert status == 200
    assert retained["history"] == original["history"]
    assert retained["conclusion"] == "NO VALID ALPHA"


def test_http_profile_usage_retains_failed_reservations_across_revisions(
    console: tuple[Path, int],
) -> None:
    root, port = console
    registry = BindingRegistry(root)
    profile = registry.save(
        {
            "kind": "MODEL_PROVIDER",
            "name": "Offline usage fixture",
            "protocol": "OPENAI_COMPATIBLE",
            "base_url": "https://api.example.com/v1",
            "default_model": "fixture-model",
            "budget_calls": 2,
        },
        {"api_key": "offline-fixture-key-never-real"},
    )
    reservation = registry._reserve_model(profile, 100, 100)
    registry._finish_model(profile, reservation, False, 1)
    status, before = request(port, "/api/bindings")
    assert status == 200
    usage = before["usage"][0]
    assert (usage["spent_calls"], usage["calls_remaining"]) == (1, 1)
    assert "NOT_PROVIDER_BILL" in usage["accounting"]
    # Product metadata is intentionally reconstructed from allowed input fields.
    fields = ("kind", "name", "protocol", "base_url", "default_model", "budget_calls")
    metadata = {k: profile[k] for k in fields}
    status, after = request(
        port,
        "/api/bindings/save",
        {"profile_id": profile["id"], "metadata": metadata, "secrets": {}},
        "CONFIGURATION",
    )
    assert status == 200
    assert after["usage"][0]["spent_calls"] == 1
    assert after["operator_enabled"] is False
    assert "offline-fixture-key-never-real" not in json.dumps(after)


def test_failed_first_declaration_does_not_replace_or_restart_select_orphan(
    tmp_path: Path,
) -> None:
    from qf_platform.storage import PlatformError

    registry = BindingRegistry(tmp_path)
    service = RoomService(tmp_path, registry)
    with pytest.raises(PlatformError, match="ROOM_DECLARATION_INVALID"):
        service.start({"mode": "SYNTHETIC", "rounds": 99})
    assert service.snapshot()["status"] == "UNDECLARED"
    assert service.snapshot()["programs"] == []
    assert RoomService(tmp_path, registry).snapshot()["status"] == "UNDECLARED"
    orphan = next(service.rooms.iterdir()).name
    with pytest.raises(PlatformError, match="ROOM_NOT_DECLARED"):
        service.select(orphan)
    assert service.current is None


def test_invalid_cost_returns_bounded_http_denial(console: tuple[Path, int]) -> None:
    _, port = console
    status, result = request(
        port, "/api/room/start", {"mode": "SYNTHETIC", "cost": "abc"}
    )
    assert status == 409
    assert result["error"] == "ROOM_DECLARATION_INVALID"


def test_competing_worker_cannot_write_terminal_failure(tmp_path: Path) -> None:
    from quantforge.product.storage import writer_lock

    service = RoomService(tmp_path, BindingRegistry(tmp_path))
    service.start({"mode": "SYNTHETIC", "rounds": 1, "calls": 6})
    assert service.worker is not None and service.current is not None
    service.worker.join(timeout=45)
    before = service.current.history()
    with writer_lock(service.current.root):
        RoomService._execute(service.current)
    assert service.current.history() == before


def test_worker_route_mismatch_is_retained_without_silent_thread_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = RoomService._execute
    monkeypatch.setattr(RoomService, "_execute", staticmethod(lambda candidate: None))
    service = RoomService(tmp_path, BindingRegistry(tmp_path))
    service.start({"mode": "SYNTHETIC", "rounds": 1, "calls": 6})
    assert service.worker is not None
    service.worker.join(timeout=2)
    assert service.current is not None
    service.current.routes["director"][0].binding_revision = "changed-fixture-route"
    execute(service.current)
    state = service.snapshot()
    assert state["status"] == "FAILED"
    assert state["history"][-1]["classification"] == "ROOM_ROUTES_IMMUTABLE"
    assert state["budget"]["spent_calls"] == 0
