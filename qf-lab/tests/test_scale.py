from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from qf_lab.gateway import FakeProvider, GatewayError, ResearchDecision
from qf_lab.scale import ScaleController, inspect_scale_history

MANIFEST = "a" * 64


class FakeCore:
    """Independent state machine test seam; never represented as product proof."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.trials: list[dict[str, Any]] = []
        self.paused = False
        self.seal = "SEALED"
        self.recovered: list[str] = []
        self.closed = False
        self.locked = False
        self.bad_context = False

    @contextlib.contextmanager
    def campaign_lock(self) -> Any:
        if self.locked:
            raise ValueError("WORKER_BUSY")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def recover(self, *, locked: bool = False) -> dict[str, Any]:
        assert locked and self.locked
        for call in self.calls:
            if call["status"] is None:
                if not any(t["call_id"] == call["id"] for t in self.trials):
                    self.submit(
                        f"trial-{len(self.trials) + 1}",
                        call["family"],
                        {"crashed_model_attempt": True},
                        call_id=call["id"],
                    )
                call["status"] = "UNKNOWN"
                self.recovered.append(call["id"])
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "program_id": "test-scale",
            "manifest_id": MANIFEST,
            "trials": len(self.trials),
            "model_calls": len(self.calls),
            "family_counts": {
                f: sum(t["family"] == f for t in self.trials) for f in ("a", "b")
            },
            "budget": {"trials": 4, "model_calls": 4, "families": {"a": 2, "b": 2}},
            "benchmark_state": self.seal,
            "paused": self.paused,
        }

    def context(self, family: str) -> dict[str, Any]:
        if self.seal != "SEALED" or self.paused:
            raise ValueError("SEARCH_CLOSED")
        evidence = [
            t["projection"]
            for t in self.trials
            if t["admitted"] and t["family"] == family
        ]
        value = {
            "phase": "next" if evidence else "first",
            "goal": "Test bounded honest SMA",
            "max_long_window": 50,
            "evidence": evidence,
            "family": family,
            "round_budget": 2 - sum(t["family"] == family for t in self.trials),
            "allowed_actions": ["modify", "continue", "branch", "reject"]
            if evidence
            else ["propose", "reject"],
        }
        if self.bad_context:
            value["final_metrics"] = {"sentinel": "SEALED_VALUE"}
        return value

    def reserve_model(self, *, family: str) -> str:
        assert self.seal == "SEALED" and not self.paused
        call = f"model-{len(self.calls) + 1}"
        self.calls.append({"id": call, "status": None, "family": family})
        return call

    def finish_model(
        self, call: str, *, status: str, ledger_id: str | None = None
    ) -> None:
        item = next(v for v in self.calls if v["id"] == call)
        assert item["status"] is None
        item["status"] = status

    def submit(
        self,
        trial_id: str,
        family: str,
        proposal: dict[str, Any],
        *,
        origin: str = "AI",
        call_id: str | None = None,
    ) -> dict[str, Any]:
        assert self.seal == "SEALED" and not self.paused
        admitted = set(proposal) == {"short_window", "long_window"}
        allocation = len(self.trials) + 1
        reference = f"{allocation:064x}"
        projection = {
            "evidence_id": reference,
            "strategy": proposal,
            "metrics": {"total_return": -0.01, "sharpe": -0.5, "max_drawdown": -0.1},
            "limitations": ["Independent test seam; no empirical proof."],
        }
        item = {
            "allocation": allocation,
            "trial_id": trial_id,
            "family": family,
            "proposal": proposal,
            "admitted": admitted,
            "projection": projection,
            "status": "COMPLETED" if admitted else "REJECTED",
            "origin": origin,
            "call_id": call_id,
        }
        self.trials.append(item)
        return {
            "allocation": allocation,
            "admitted": admitted,
            "status": item["status"],
            "evidence_id": reference,
            "run_id": trial_id,
        }

    def pause(self, paused: bool) -> dict[str, Any]:
        self.paused = paused
        return self.status()

    def freeze(self) -> dict[str, Any]:
        assert self.locked
        assert len(self.trials) == 4
        self.seal = "FROZEN_CLOSED"
        return {"status": "FROZEN", "evidence_id": "f" * 64}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> FakeCore:
    value = FakeCore()
    monkeypatch.setattr("qf_lab.scale._open_core", lambda root: value)
    return value


def controller(tmp_path: Path, provider: Any = None) -> ScaleController:
    return ScaleController(
        tmp_path / "core", tmp_path / "lab", provider or FakeProvider()
    )


def test_bounded_pause_resume_reconstructs_core_families_and_counts(
    tmp_path: Path, core: FakeCore
) -> None:
    first = controller(tmp_path).run(steps=2)
    assert first["steps_completed"] == 2 and not first["real_calls_proven"]
    assert [t["family"] for t in core.trials] == ["a", "b"]
    core.pause(True)
    assert controller(tmp_path).run(steps=2)["status"] == "PAUSED"
    assert len(core.calls) == 2
    final = controller(tmp_path).run(steps=2, resume=True, freeze_when_exhausted=True)
    assert final["status"] == "EXHAUSTED" and final["frozen"]["status"] == "FROZEN"
    assert [t["trial_id"] for t in core.trials] == [
        "trial-1",
        "trial-2",
        "trial-3",
        "trial-4",
    ]
    assert [t["family"] for t in core.trials] == ["a", "b", "a", "b"]
    assert core.trials[2]["proposal"] != core.trials[0]["proposal"]
    history = inspect_scale_history(tmp_path / "lab", MANIFEST)
    assert len([v for v in history if v["kind"] == "MODEL_DECISION"]) == 4
    assert len([v for v in history if v["kind"] == "TRIAL_REFERENCE"]) == 4
    with (
        sqlite3.connect(final["history"]) as connection,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        connection.execute("DELETE FROM events")


def test_reservation_precedes_provider_and_unknown_failure_is_counted(
    tmp_path: Path, core: FakeCore
) -> None:
    class Ambiguous(FakeProvider):
        def complete(self, context: dict[str, Any]) -> Any:
            assert len(core.calls) == 1 and core.calls[0]["status"] is None
            raise GatewayError(
                "MODEL_TRANSPORT_UNKNOWN: DO_NOT_PERSIST_SECRET_SENTINEL"
            )

    with pytest.raises(GatewayError, match="NO_RETRY"):
        controller(tmp_path, Ambiguous()).run(steps=4)
    assert len(core.calls) == 1 and core.calls[0]["status"] is None
    assert len(core.trials) == 1 and not core.trials[0]["admitted"]
    history = inspect_scale_history(tmp_path / "lab", MANIFEST)
    assert "DO_NOT_PERSIST_SECRET_SENTINEL" not in json.dumps(history)
    # A new bounded invocation advances the Core attempt/family; it does not retry model-1.
    resumed = controller(tmp_path).run(steps=1)
    assert core.recovered == ["model-1"] and core.calls[0]["status"] == "UNKNOWN"
    assert len(core.calls) == 2 and core.trials[1]["family"] == "b"
    assert resumed["steps_completed"] == 1


def test_process_interrupt_reservation_remains_spent_until_recovery(
    tmp_path: Path, core: FakeCore
) -> None:
    class Interrupted(FakeProvider):
        def complete(self, context: dict[str, Any]) -> Any:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        controller(tmp_path, Interrupted()).run(steps=1)
    assert len(core.calls) == 1 and not core.trials
    assert core.closed and not core.locked
    controller(tmp_path).run(steps=1)
    assert core.calls[0]["status"] == "UNKNOWN" and len(core.calls) == 2
    assert core.trials[0]["status"] == "REJECTED" and core.trials[1]["family"] == "b"


@pytest.mark.parametrize("attack", ["reference", "action", "fact"])
def test_invalid_model_outputs_cannot_become_core_facts_or_free_trials(
    tmp_path: Path, core: FakeCore, attack: str
) -> None:
    class Bad(FakeProvider):
        def complete(self, context: dict[str, Any]) -> Any:
            decision, metadata = super().complete(context)
            if attack == "reference":
                return decision.model_copy(
                    update={"evidence_refs": ("b" * 64,)}
                ), metadata
            if attack == "action":
                return decision.model_copy(update={"action": "freeze"}), metadata
            return ResearchDecision.model_construct(
                action="propose",
                short_window=0,
                long_window=50,
                rationale="Unvalidated fact attempt",
                evidence_refs=(),
            ), metadata

    with pytest.raises(GatewayError, match="NO_RETRY"):
        controller(tmp_path, Bad()).run(steps=1)
    assert len(core.calls) == 1 and len(core.trials) == 1
    assert not core.trials[0]["admitted"]
    assert not any(
        v["kind"] == "MODEL_DECISION"
        for v in inspect_scale_history(tmp_path / "lab", MANIFEST)
    )


def test_sealed_fact_in_context_rejected_before_allocating_or_sending_call(
    tmp_path: Path, core: FakeCore
) -> None:
    core.bad_context = True
    with pytest.raises(GatewayError, match="CONTEXT_SCHEMA"):
        controller(tmp_path).run(steps=1)
    assert core.calls == [] and core.trials == []
    history = inspect_scale_history(tmp_path / "lab", MANIFEST)
    assert "SEALED_VALUE" not in json.dumps(history)


def test_spent_seal_blocks_stale_or_late_response(
    tmp_path: Path, core: FakeCore
) -> None:
    class Late(FakeProvider):
        def complete(self, context: dict[str, Any]) -> Any:
            decision, metadata = super().complete(context)
            core.seal = "SPENT"
            return decision, metadata

    result = controller(tmp_path, Late()).run(steps=2)
    assert result["steps_completed"] == 0 and not core.trials
    assert any(
        v["kind"] == "LATE_DECISION_IGNORED"
        for v in inspect_scale_history(tmp_path / "lab", MANIFEST)
    )
    assert controller(tmp_path).run(steps=1)["status"] == "SEARCH_CLOSED"
    assert len(core.calls) == 1


def test_corrupt_intelligence_history_fails_before_model_call(
    tmp_path: Path, core: FakeCore
) -> None:
    result = controller(tmp_path).run(steps=1)
    with sqlite3.connect(result["history"]) as connection:
        connection.execute("DROP TRIGGER no_update")
        connection.execute("UPDATE events SET payload='{}' WHERE seq=1")
    with pytest.raises(GatewayError, match="INTEGRITY"):
        controller(tmp_path).run(steps=1)
    assert len(core.calls) == 1


def test_second_worker_is_denied_before_recovery(
    tmp_path: Path, core: FakeCore
) -> None:
    with core.campaign_lock(), pytest.raises(ValueError, match="BUSY"):
        controller(tmp_path).run(steps=1)
    assert not core.calls and not core.recovered


def test_rejected_ideas_are_retained_without_execution(
    tmp_path: Path, core: FakeCore
) -> None:
    class Reject(FakeProvider):
        def complete(self, context: dict[str, Any]) -> Any:
            decision, metadata = super().complete(context)
            return decision.model_copy(update={"action": "reject"}), metadata

    result = controller(tmp_path, Reject()).run(steps=1)
    assert result["steps_completed"] == 1 and len(core.calls) == 1
    assert not core.trials[0]["admitted"] and core.trials[0]["proposal"] == {
        "rejected_idea": True
    }
    assert (
        inspect_scale_history(tmp_path / "lab", MANIFEST)[2]["decision"]["action"]
        == "reject"
    )


@pytest.mark.parametrize("steps", [True, 0, -1, 65])
def test_step_budget_is_closed_before_core_work(
    tmp_path: Path, core: FakeCore, steps: Any
) -> None:
    with pytest.raises(GatewayError, match="STEPS_BOUND"):
        controller(tmp_path).run(steps=steps)
    assert not core.calls and not core.trials


def test_non_sma_decision_consumes_reservation_and_cannot_enter_scale_v1(
    tmp_path: Path,
    core: FakeCore,
) -> None:
    class OtherFamily(FakeProvider):
        def complete(self, context: dict[str, Any]) -> Any:
            assert context["allowed_families"] == ["moving_average_crossover"]
            return ResearchDecision.model_validate(
                {
                    "action": "propose",
                    "family": "donchian_breakout",
                    "entry_window": 15,
                    "exit_window": 5,
                    "rationale": "Cross-family scale contract attempt",
                    "evidence_refs": [],
                }
            ), {"real_model_call": False}

    with pytest.raises(GatewayError, match="NO_RETRY"):
        controller(tmp_path, OtherFamily()).run(steps=1)
    assert len(core.calls) == len(core.trials) == 1
    assert core.calls[0]["status"] == "ERROR"
    assert not core.trials[0]["admitted"]


def test_native_core_scale_resume_freezes_with_counted_stress_and_safe_prompts(
    tmp_path: Path,
) -> None:
    """Actual facade integration: all market values are explicit synthetic fixtures."""
    import math
    from datetime import UTC, datetime, timedelta

    import polars as pl
    from quantforge.data import BAR_SCHEMA
    from quantforge.product.scale.service import ScaleService, prepare_program

    root = tmp_path / "native"
    root.mkdir()
    prices = [100.0 + i * 0.3 + math.sin(i / 3) for i in range(105)]
    frames = {
        symbol: pl.DataFrame(
            {
                "symbol": [symbol] * 105,
                "timestamp": [
                    datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i)
                    for i in range(105)
                ],
                "open": prices,
                "close": prices,
                "high": [v + 1 for v in prices],
                "low": [v - 1 for v in prices],
                "volume": [1000.0] * 105,
            },
            schema=BAR_SCHEMA,
        )
        for symbol in ("A", "B")
    }
    prepare_program(
        root,
        frames,
        {"host_fixture": True},
        {
            "program_id": "native",
            "goal": "Bounded synthetic engineering qualification",
            "universe_id": "fixed-A-B",
            "research_end": "2020-03-15T00:00:00+00:00",
            "protocol": {
                "initial_history_bars": 50,
                "window_bars": 10,
                "partition_id": "frozen-date",
            },
            "budgets": {
                "trials": 10,
                "families": {"f": 10},
                "model_calls": 2,
                "wall_seconds": 3600,
                "data_rows_scanned": 100000,
                "benchmark_openings": 1,
                "retries": 0,
            },
        },
        key_path=tmp_path / "host.key",
    )
    one = ScaleController(root, tmp_path / "intelligence", FakeProvider()).run(steps=1)
    assert one["core"]["trials"] == 5 and one["core"]["model_calls"] == 1
    two = ScaleController(root, tmp_path / "intelligence", FakeProvider()).run(
        steps=1, freeze_when_exhausted=True
    )
    assert two["core"]["trials"] == 10 and two["core"]["model_calls"] == 2
    assert two["core"]["benchmark_state"] == "FROZEN_CLOSED"
    assert sum(t["origin"] == "STRESS" for t in two["core"]["trial_history"]) == 8
    with sqlite3.connect(two["call_ledger"]) as connection:
        contexts = [
            json.loads(raw)["context"]
            for (raw,) in connection.execute(
                "SELECT payload FROM calls WHERE phase='REQUEST' ORDER BY seq"
            )
        ]
    assert contexts[0]["evidence"] == []
    assert contexts[1]["evidence"] and all(
        set(item["metrics"]) == {"total_return", "sharpe", "max_drawdown"}
        for item in contexts[1]["evidence"]
    )
    assert "final_metrics" not in json.dumps(contexts) and "key_path" not in json.dumps(
        contexts
    )
    core = ScaleService(root)
    assert core.status()["benchmark_state"] == "FROZEN_CLOSED"
    core.close()
    assert (
        ScaleController(root, tmp_path / "intelligence", FakeProvider()).run(steps=1)[
            "status"
        ]
        == "SEARCH_CLOSED"
    )
