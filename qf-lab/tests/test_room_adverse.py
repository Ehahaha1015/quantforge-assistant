from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_room import room

from qf_lab.gateway import GatewayError
from qf_lab.protocols import ProtocolProvider
from qf_lab.room import ResearchRoom
from qf_lab.room_gateway import FakeRoomProvider


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
def test_undeclared_control_cannot_poison_history(tmp_path: Path, action: str) -> None:
    root, core = tmp_path / "room", tmp_path / "core"
    root.mkdir()
    core.mkdir()
    app = ResearchRoom(
        root,
        core,
        {
            role: [FakeRoomProvider(role)]
            for role in ("researcher", "critic", "director", "validator")
        },
    )
    with pytest.raises(GatewayError, match="ROOM_NOT_DECLARED"):
        app.control(action)
    assert app.history() == []
    assert app.snapshot()["status"] == "UNDECLARED"


def test_corrupt_leading_event_has_safe_classification(tmp_path: Path) -> None:
    root, core = tmp_path / "room", tmp_path / "core"
    root.mkdir()
    core.mkdir()
    app = ResearchRoom(
        root,
        core,
        {
            role: [FakeRoomProvider(role)]
            for role in ("researcher", "critic", "director", "validator")
        },
    )
    app.event("CONTROL", action="pause")  # Corrupt-store fixture, not product control.
    with pytest.raises(GatewayError, match="ROOM_HISTORY_INVALID"):
        app.snapshot()


def test_capacity_denial_preserves_reason_without_provider_isolation(
    tmp_path: Path,
) -> None:
    class CapacityDenied(FakeRoomProvider):
        def complete_structured(self, *args: Any, **kwargs: Any) -> Any:
            raise GatewayError("MODEL_GLOBAL_CAPACITY_DENIED")

    routes = {
        role: [FakeRoomProvider(role)]
        for role in ("researcher", "critic", "director", "validator")
    }
    routes["director"] = [CapacityDenied("director")]
    app = room(tmp_path, 1, routes)
    result = app.run()
    assert result["status"] == "FAILED"
    assert result["history"][-1]["classification"] == "MODEL_GLOBAL_CAPACITY_DENIED"
    assert not any(r["kind"] == "ROLE_PROVIDER_FAILED" for r in result["history"])
    assert result["calls"][-1]["classification"] == "MODEL_GLOBAL_CAPACITY_DENIED"


@pytest.mark.parametrize("pause", [False, True])
def test_verified_global_capacity_race_waits_without_spend_or_duplicate_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pause: bool
) -> None:
    attempts, sends = 0, 0
    app: ResearchRoom

    def reserve(*args: Any) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if pause:
                app.control("pause")
            raise GatewayError("MODEL_GLOBAL_CAPACITY_DENIED")
        return "offline-capacity-lease"

    class Opener:
        def open(self, request: Any, **kwargs: Any) -> Any:
            nonlocal sends
            sends += 1
            response = io.BytesIO(b'{"data":[]}')
            response.geturl = lambda: request.full_url  # type: ignore[attr-defined]
            return response

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    transport = ProtocolProvider(
        "fixture",
        "OPENAI_COMPATIBLE",
        "https://api.example.com/v1",
        "fixture-model",
        secret=lambda: {"api_key": "offline-capacity-fixture"},
        before_request=reserve,
    )

    class Researcher(FakeRoomProvider):
        def complete_structured(self, *args: Any, **kwargs: Any) -> Any:
            transport._request("/models", None)
            return super().complete_structured(*args, **kwargs)

    routes = {
        role: [FakeRoomProvider(role)]
        for role in ("researcher", "critic", "director", "validator")
    }
    routes["researcher"] = [Researcher("researcher")]
    app = room(tmp_path, 1, routes, calls=6)
    state = app.run()
    if pause:
        assert state["status"] == "PAUSED"
        assert state["budget"]["spent_calls"] == 0
        assert sends == 0
        app.control("resume")
        state = app.run()
    assert state["status"] == "COMPLETE", state["history"][-1]
    # Propose and mutation are distinct paid steps; only the first hit the race.
    assert attempts == 3 and sends == 2
    assert state["budget"]["spent_calls"] == 6
    assert not state["budget"]["unknown_call_ids"]
    assert not any(row["kind"] == "ROLE_PROVIDER_FAILED" for row in state["history"])
    denied = next(row for row in state["calls"] if row.get("send_status"))
    assert denied["send_status"] == "NOT_SENT_VERIFIED"
    assert denied["spend"] == "NO_SPEND"
    requests = [row["call_id"] for row in state["calls"] if row["phase"] == "REQUEST"]
    assert len(requests) == len(set(requests)) == 7
    assert app.run()["budget"]["spent_calls"] == 6 and sends == 2


def test_missing_call_store_is_never_recreated_or_recharged(tmp_path: Path) -> None:
    app = room(tmp_path, 1)
    app.control("pause")
    before = app.history()
    path = app.root / "room-calls.sqlite"
    path.rename(path.with_suffix(".retained"))
    for action in (app.snapshot, app.run, lambda: app.control("resume")):
        with pytest.raises(GatewayError, match="ROOM_CALL_STORE_MISSING"):
            action()
        assert not path.exists()
        assert app.history() == before


def test_replaced_empty_call_store_cannot_reset_completed_spend(tmp_path: Path) -> None:
    app = room(tmp_path, 1)
    assert app.run()["status"] == "COMPLETE"
    path = app.root / "room-calls.sqlite"
    path.rename(path.with_suffix(".retained"))
    # Corrupt restoration with retained policy but missing call rows.
    with sqlite3.connect(path.with_suffix(".retained")) as original:
        policy = original.execute("SELECT payload FROM policy").fetchone()[0]
    with sqlite3.connect(path) as replacement:
        replacement.execute(
            "CREATE TABLE policy (id INTEGER PRIMARY KEY, payload TEXT)"
        )
        replacement.execute("INSERT INTO policy VALUES (1,?)", (policy,))
        replacement.execute(
            "CREATE TABLE calls (seq INTEGER PRIMARY KEY, phase TEXT, payload TEXT)"
        )
    with pytest.raises(GatewayError, match="ROOM_CALL_STORE_HISTORY_MISMATCH"):
        app.run()


def test_persistent_capacity_wait_parks_without_burning_dataset(tmp_path: Path) -> None:
    class Busy(FakeRoomProvider):
        attempts = 0

        def check_capacity(self, *args: Any) -> None:
            self.attempts += 1
            raise GatewayError("MODEL_PROFILE_BUSY")

    busy = Busy("researcher")
    routes = {
        role: [FakeRoomProvider(role)]
        for role in ("researcher", "critic", "director", "validator")
    }
    routes["researcher"] = [busy]
    app = room(tmp_path, 1, routes)
    state = app.run()
    assert state["status"] == "PAUSED"
    assert busy.attempts == 3 and state["budget"]["spent_calls"] == 0
    assert state["history"][-1]["reason"] == "MODEL_CAPACITY_WAIT"
    # Restored capacity with the same frozen route identity, family and budget.
    app.routes["researcher"] = [FakeRoomProvider("researcher")]
    app.control("resume")
    assert app.run()["status"] == "COMPLETE"
