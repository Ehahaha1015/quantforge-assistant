from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from quantforge.data import BAR_SCHEMA, ParquetDataCatalog

from qf_lab.gateway import GatewayError, ResearchDecision, canonical
from qf_lab.room import ResearchRoom
from qf_lab.room_gateway import FakeRoomProvider, RoomGateway


def room(
    tmp_path: Path,
    rounds: int = 3,
    routes: Any = None,
    n: int = 100,
    source: str = "synthetic-fixture",
    calls: int = 32,
    goal: Any = "A bounded causal family test",
) -> ResearchRoom:
    core, lab = tmp_path / "core", tmp_path / "room"
    core.mkdir()
    lab.mkdir()
    start = datetime(2020, 1, 1, tzinfo=UTC)
    prices = [100.0 + (i % 17) + i * 0.1 for i in range(n)]
    snapshot = ParquetDataCatalog(core / "catalog").write(
        "synthetic-fixture",
        pl.DataFrame(
            {
                "symbol": ["IBM"] * n,
                "timestamp": [start + timedelta(days=i) for i in range(n)],
                "open": prices,
                "close": [v + 1 for v in prices],
                "high": [v + 2 for v in prices],
                "low": [v - 2 for v in prices],
                "volume": [1000.0] * n,
            },
            schema=BAR_SCHEMA,
        ),
        source=source,
        schema_version="bars-v1",
        as_of=start + timedelta(days=n),
    )
    routes = routes or {
        role: [FakeRoomProvider(role)]
        for role in ("researcher", "critic", "director", "validator")
    }
    result = ResearchRoom(lab, core, routes)
    result.declare(
        program_id="roomtest",
        goal=goal,
        dataset_id=snapshot.dataset_id,
        rounds=rounds,
        calls=calls,
    )
    return result


def test_goal_whitespace_is_normalized_before_paid_calls_and_core_admission(
    tmp_path: Path,
) -> None:
    app = room(tmp_path, rounds=1, calls=6, goal="  A causal qualification  ")
    state = app.run()
    assert state["status"] == "COMPLETE"
    assert state["rounds_completed"] == 1
    assert state["declaration"]["goal"] == "A causal qualification"
    assert all(
        v["context"]["goal"] == state["declaration"]["goal"]
        for v in state["calls"]
        if v["phase"] == "REQUEST"
    )


@pytest.mark.parametrize("goal", [None, "  ", "contains\x00control", "contains\ncontrol"])
def test_invalid_goal_denies_before_declaration_or_call_store(
    tmp_path: Path, goal: Any,
) -> None:
    with pytest.raises(GatewayError, match="ROOM_DECLARATION_INVALID"):
        room(tmp_path, rounds=1, calls=6, goal=goal)
    assert not (tmp_path / "room" / "room-calls.sqlite").exists()
    with (tmp_path / "room" / "room-history.sqlite").open("rb") as database:
        assert database.read(16) == b"SQLite format 3\x00"


def test_minimum_budget_denies_before_declaration_and_fake_mode_keeps_data_origin(
    tmp_path: Path,
) -> None:
    short = tmp_path / "short"
    short.mkdir()
    with pytest.raises(GatewayError, match="ROOM_CALL_BUDGET_INSUFFICIENT"):
        room(short, rounds=3, calls=16)
    root = tmp_path / "historical"
    root.mkdir()
    app = room(root, rounds=1, source="official-retained-daily-fixture", calls=6)
    declaration = app.snapshot()["declaration"]
    assert declaration["mode"] == "HISTORICAL"
    assert declaration["provider_mode"] == "DETERMINISTIC_FAKE"
    assert app.run()["status"] == "COMPLETE"


def test_expired_pause_resume_denies_without_control_or_terminal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = room(tmp_path)
    app.control("pause")
    before = app.history()
    later = datetime.now(UTC) + timedelta(hours=2)

    class Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return later

    monkeypatch.setattr("qf_lab.room.datetime", Later)
    with pytest.raises(GatewayError, match="ROOM_WALL_BUDGET_EXHAUSTED"):
        app.control("resume")
    assert app.history() == before
    assert app.snapshot()["status"] == "PAUSED"


@pytest.mark.parametrize("n", [34, 35, 79, 80])
def test_core_observation_floor_before_declaration_or_model_spend(
    tmp_path: Path, n: int
) -> None:
    if n < 80:
        with pytest.raises(GatewayError, match="ROOM_DATASET_INSUFFICIENT"):
            room(tmp_path, rounds=1, n=n)
        app = ResearchRoom(
            tmp_path / "room",
            tmp_path / "core",
            {
                role: [FakeRoomProvider(role)]
                for role in ("researcher", "critic", "director", "validator")
            },
        )
        assert app.history() == []
    else:
        assert room(tmp_path, rounds=1, n=n).run()["status"] == "COMPLETE"


def test_three_rounds_director_negative_results_memory_and_no_final_leak(
    tmp_path: Path,
) -> None:
    app = room(tmp_path)
    result = app.run()
    assert result["status"] == "COMPLETE", json.dumps(result["history"][-3:], indent=2)
    assert result["rounds_completed"] == 3
    assert result["budget"]["spent_calls"] == 18
    assert result["mode"] == "SYNTHETIC"
    assert result["conclusion"] == "NO VALID ALPHA"
    assert result["history"][-2]["director"]["next"] == "reject"
    assert len(app.memory()["FACT_MEMORY"]) == 6
    assert app.memory()["INTELLIGENCE_MEMORY"]["failures"]
    assert canonical(app.memory()) == canonical(app.memory())
    before = result["budget"]["spent_calls"]
    assert app.run()["budget"]["spent_calls"] == before
    for request in result["calls"]:
        if request["phase"] == "REQUEST":
            assert "final_metrics" not in canonical(request["context"])
            for evidence in request["context"].get("evidence", []):
                assert evidence["FACT"]["final_lockbox_scored"] is False


def test_pause_resume_and_recover_committed_call_without_retry(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app = room(tmp_path, 1)
    app.control("pause")
    assert app.run()["status"] == "PAUSED"
    assert app.snapshot()["budget"]["spent_calls"] == 0
    app.control("resume")

    class Crash(BaseException):
        pass

    original = app.event

    def crash_event(kind: str, **values: Any) -> None:
        if kind == "MODEL_DECISION":
            raise Crash
        original(kind, **values)

    monkeypatch.setattr(app, "event", crash_event)
    with pytest.raises(Crash):
        app.run()
    assert app.snapshot()["budget"]["spent_calls"] == 1
    monkeypatch.setattr(app, "event", original)
    result = app.run()
    assert result["status"] == "COMPLETE"
    assert result["budget"]["spent_calls"] == 6


def test_budget_is_durable_and_rejects_expansion(tmp_path: Path) -> None:
    gateway = RoomGateway(tmp_path, calls=1, tokens=20000, cost=Decimal(1))
    context = {
        "phase": "first",
        "allowed_families": ["moving_average_crossover"],
        "max_long_window": 20,
        "allowed_actions": ["propose"],
        "evidence": [],
    }
    gateway.call("researcher", FakeRoomProvider(), context, ResearchDecision)
    with pytest.raises(GatewayError, match="BUDGET_EXHAUSTED"):
        gateway.call("researcher", FakeRoomProvider(), context, ResearchDecision)
    with pytest.raises(GatewayError, match="IMMUTABLE"):
        RoomGateway(tmp_path, calls=2, tokens=20000, cost=Decimal(1))


def test_ambiguous_call_keeps_reservation_and_never_retries(tmp_path: Path) -> None:
    app = room(tmp_path, 1)

    class BrokenProvider(FakeRoomProvider):
        def complete_structured(self, *args: Any, **kwargs: Any) -> Any:
            raise TimeoutError("a-key-must-never-be-preserved")

    app.routes["researcher"] = [BrokenProvider("researcher")]
    result = app.run()
    assert result["status"] == "FAILED"
    assert result["budget"]["spent_calls"] == 1
    assert result["budget"]["spent_tokens"] > 0
    assert "a-key-must-never" not in canonical(result)
    assert app.run()["budget"]["spent_calls"] == 1


def test_failed_researcher_isolated_healthy_pool_continues_and_revision_frozen(
    tmp_path: Path,
) -> None:
    class Failed(FakeRoomProvider):
        sends = 0

        def complete_structured(self, *args: Any, **kwargs: Any) -> Any:
            self.sends += 1
            raise GatewayError("MODEL_AUTH")

    failed = Failed("primary-researcher")
    routes = {
        role: [FakeRoomProvider(role)]
        for role in ("researcher", "critic", "director", "validator")
    }
    routes["researcher"] = [failed, FakeRoomProvider("secondary-researcher")]
    app = room(tmp_path, routes=routes)
    result = app.run()
    assert result["status"] == "COMPLETE"
    assert result["rounds_completed"] == 3
    assert failed.sends == 1
    assert result["budget"]["spent_calls"] == 19
    assert any(
        v["kind"] == "ROLE_PROVIDER_FAILED"
        for v in app.memory()["INTELLIGENCE_MEMORY"]["failures"]
    )
    requests = [
        v
        for v in result["calls"]
        if v["phase"] == "REQUEST" and v["context"].get("round") == 2
    ]
    assert any(v["context"].get("director_next_hypothesis") for v in requests)
    assert all(
        v["context"]["intelligence_memory"]["negative_result_count"] > 0
        for v in requests
    )


def test_changed_binding_revision_cannot_resume_a_declared_real_route(
    tmp_path: Path,
) -> None:
    app = room(tmp_path, 1)
    app.routes["researcher"][0].binding_revision = "rotated"  # type: ignore[attr-defined]
    with pytest.raises(GatewayError, match="ROUTES_IMMUTABLE"):
        app.run()
    assert app.snapshot()["budget"]["spent_calls"] == 0
    assert app.snapshot()["status"] == "FAILED"


def test_known_failed_primary_then_pause_resume_reuses_fallback_without_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failed(FakeRoomProvider):
        sends = 0

        def complete_structured(self, *args: Any, **kwargs: Any) -> Any:
            self.sends += 1
            raise GatewayError("MODEL_AUTH")

    failed = Failed("director-primary")
    routes = {
        role: [FakeRoomProvider(role)]
        for role in ("researcher", "critic", "director", "validator")
    }
    routes["director"] = [failed, FakeRoomProvider("director-fallback")]
    app = room(tmp_path, rounds=1, routes=routes)
    original = app._experiment
    paused = False

    def pause_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal paused
        if not paused:
            paused = True
            app.control("pause")
        return original(*args, **kwargs)

    monkeypatch.setattr(app, "_experiment", pause_once)
    first = app.run()
    assert first["status"] == "PAUSED"
    assert first["budget"]["spent_calls"] == 3
    app.control("resume")
    result = app.run()
    assert result["status"] == "COMPLETE"
    assert result["budget"]["spent_calls"] == 7
    assert failed.sends == 1


def test_director_and_validator_fallback_is_frozen_bounded_and_isolated(
    tmp_path: Path,
) -> None:
    class Failed(FakeRoomProvider):
        sends = 0

        def complete_structured(self, *args: Any, **kwargs: Any) -> Any:
            self.sends += 1
            raise GatewayError("MODEL_RESPONSE_SCHEMA")

    director, validator = Failed("director-primary"), Failed("validator-primary")
    routes = {
        role: [FakeRoomProvider(role)]
        for role in ("researcher", "critic", "director", "validator")
    }
    routes["director"] = [director, FakeRoomProvider("director-fallback")]
    routes["validator"] = [validator, FakeRoomProvider("validator-fallback")]
    app = room(tmp_path, 1, routes)
    result = app.run()
    assert result["status"] == "COMPLETE", result["history"][-1]
    assert result["budget"]["spent_calls"] == 8
    assert director.sends == validator.sends == 1
    assert (
        len([v for v in result["history"] if v["kind"] == "ROLE_PROVIDER_FAILED"]) == 2
    )
    assert app.run()["budget"]["spent_calls"] == 8


def test_existing_research_family_is_denied_before_any_new_model_call(
    tmp_path: Path,
) -> None:
    app = room(tmp_path, 1)
    assert app.run()["status"] == "COMPLETE"
    new_root = tmp_path / "new-campaign"
    new_root.mkdir()
    new = ResearchRoom(new_root, app.core_root, app.routes)
    with pytest.raises(GatewayError, match="DATASET_ALREADY_BOUND"):
        new.declare(
            program_id="new-campaign",
            goal="Never reset a retained exposure budget",
            dataset_id="synthetic-fixture",
            rounds=1,
        )
    gateway = RoomGateway(new_root, calls=32, tokens=250000, cost=Decimal(1))
    assert gateway.budget()["spent_calls"] == 0


def test_retained_evidence_review_never_resets_or_adds_experiment_trials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantforge.product.service import ResearchService

    app = room(tmp_path, 1)
    original = app.run()
    assert original["status"] == "COMPLETE"
    reference = next(
        v for v in original["history"] if v["kind"] == "EVIDENCE_REFERENCE"
    )
    root = tmp_path / "retained-review"
    root.mkdir()
    review = ResearchRoom(root, app.core_root, app.routes)
    review.declare(
        program_id="retained-review",
        dataset_id="synthetic-fixture",
        goal="Critique retained evidence without new trials or alpha claims",
        rounds=1,
        calls=3,
        review_run_id=reference["run_id"],
    )
    original_event = review.event

    def crash_before_complete(kind: str, **values: Any) -> Any:
        if kind == "COMPLETE":
            raise KeyboardInterrupt("offline crash between review and completion")
        return original_event(kind, **values)

    monkeypatch.setattr(review, "event", crash_before_complete)
    with pytest.raises(KeyboardInterrupt):
        review.run()
    assert review.snapshot()["reviews_completed"] == 1
    monkeypatch.setattr(review, "event", original_event)
    state = review.run()
    assert state["status"] == "COMPLETE", state["history"][-1]
    assert state["budget"]["spent_calls"] == 3
    assert state["reviews_completed"] == 1
    assert state["history"][-2]["kind"] == "RETAINED_EVIDENCE_REVIEW"
    assert not any(row["kind"] == "EXPERIMENT_PREPARE" for row in state["history"])
    core = ResearchService(app.core_root)
    try:
        assert len(core.list_runs()) == 2
    finally:
        core.close()
    assert app.snapshot()["history"] == original["history"]
