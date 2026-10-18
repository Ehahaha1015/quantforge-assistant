import threading
import time
from pathlib import Path

import pytest
from qf_lab.gateway import GatewayError
from qf_lab.room_gateway import FakeRoomProvider
from quantforge.product.storage import writer_lock

from qf_platform.bindings import BindingRegistry
from qf_platform.research_room import RoomService
from qf_platform.room_dispatch import RoomDispatch
from qf_platform.storage import PlatformError, digest


def room(root: Path) -> RoomService:
    return RoomService(root, BindingRegistry(root), external_worker=True)


def test_queue_survives_api_restart_and_runs_without_api_thread(tmp_path: Path) -> None:
    service = room(tmp_path)
    queued = service.start({"mode": "SYNTHETIC", "rounds": 1, "calls": 6})
    assert service.worker is None
    assert queued["dispatch_receipt"]["status"] == "QUEUED"
    assert len(queued["dispatch_receipt"]["event_hash"]) == 64
    assert queued["worker_state"] == "QUEUED" and queued["budget"]["spent_calls"] == 0
    restarted = room(tmp_path)
    assert restarted.snapshot()["declaration"] == queued["declaration"]
    assert restarted.tick()
    completed = service.snapshot()
    assert completed["status"] == completed["worker_state"] == "COMPLETE"
    assert completed["budget"]["spent_cost"] == "0"
    assert all(not row.get("real_model_call", False) for row in completed["calls"])
    assert not restarted.tick()


def test_pause_before_execution_requires_explicit_resume(tmp_path: Path) -> None:
    service = room(tmp_path)
    original = service.start({"mode": "SYNTHETIC", "rounds": 1, "calls": 6})
    service.control("pause")
    assert service.tick()
    paused = service.snapshot()
    assert paused["status"] == paused["worker_state"] == "PAUSED"
    assert paused["budget"]["spent_calls"] == 0
    resumed = service.control("resume")
    assert resumed["declaration"] == original["declaration"]
    assert resumed["worker_state"] == "QUEUED"
    assert service.tick() and service.snapshot()["status"] == "COMPLETE"


def test_parent_stop_requests_pause_at_lab_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = room(tmp_path)
    service.start({"mode": "SYNTHETIC", "rounds": 1, "calls": 6})
    entered, stop = threading.Event(), threading.Event()

    def held(candidate):
        entered.set()
        end = stop.wait(5)
        assert end
        deadline = time.monotonic() + 5
        while candidate.snapshot()["status"] != "PAUSED":
            assert time.monotonic() < deadline
            time.sleep(0.05)

    monkeypatch.setattr(RoomService, "_execute", staticmethod(held))
    worker = threading.Thread(target=lambda: service.tick(stop))
    worker.start()
    assert entered.wait(5)
    stop.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    state = service.snapshot()
    assert state["status"] == state["worker_state"] == "PAUSED"
    assert state["budget"]["spent_calls"] == 0


def test_process_fence_and_declaration_pin_prevent_dispatch(tmp_path: Path) -> None:
    service = room(tmp_path)
    queued = service.start({"mode": "SYNTHETIC", "rounds": 1, "calls": 6})
    with writer_lock(service.dispatch.ledger.root):
        assert not service.tick()
    assert service.snapshot()["budget"]["spent_calls"] == 0
    with pytest.raises(PlatformError, match="ROOM_ALREADY_RUNNING"):
        service.start({"mode": "SYNTHETIC", "rounds": 1, "calls": 6})
    assert (
        queued["declaration"]["program_id"]
        == service.snapshot()["declaration"]["program_id"]
    )


def test_dispatch_reclaim_preserves_job_identity_and_conflicts(tmp_path: Path) -> None:
    dispatch = RoomDispatch(tmp_path)
    queued = dispatch.enqueue("room-" + "a" * 20, digest({"fixture": True}))
    assert (
        dispatch.enqueue(queued["program_id"], queued["declaration_hash"])["job_id"]
        == queued["job_id"]
    )
    claimed = dispatch.claim()
    assert claimed is not None
    recovered = RoomDispatch(tmp_path).claim()
    assert recovered is not None and recovered["job_id"] == claimed["job_id"]
    with pytest.raises(PlatformError, match="COMMIT_CONFLICT"):
        dispatch.finish(claimed, "COMPLETE")
    dispatch.finish(recovered, "FAILED")
    assert not dispatch.active()


def test_unknown_provider_effect_is_retained_and_not_redispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = room(tmp_path)

    class Unknown(FakeRoomProvider):
        count = 0

        def complete_structured(self, *args, **kwargs):
            self.count += 1
            raise GatewayError("MODEL_TRANSPORT_UNKNOWN")

    unknown = Unknown("researcher")
    routes = service._routes(True)
    routes["researcher"] = [unknown]
    monkeypatch.setattr(service, "_routes", lambda synthetic: routes)
    service.start({"mode": "SYNTHETIC", "rounds": 1, "calls": 6})
    assert service.tick()
    state = service.snapshot()
    assert state["status"] == state["worker_state"] == "FAILED"
    assert unknown.count == 1
    assert any(row["phase"] == "ERROR" for row in state["calls"])
    assert not room(tmp_path).tick()
    assert unknown.count == 1
