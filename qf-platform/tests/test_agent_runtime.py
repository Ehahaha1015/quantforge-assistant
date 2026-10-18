from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel
from qf_lab.gateway import GatewayError
from qf_lab.room_gateway import FakeRoomProvider, RoomGateway
from test_agent_tools import tools

from qf_platform.agent_runtime import AgentRuntime
from qf_platform.storage import PlatformError

T = TypeVar("T", bound=BaseModel)


class Planner(FakeRoomProvider):
    def __init__(self, *, failure: bool = False) -> None:
        super().__init__("test-only-provider")
        self.count, self.failure = 0, failure
        self.contexts: list[dict[str, Any]] = []

    def complete_structured(
        self, context: dict[str, Any], schema: type[T], *, max_output: int = 1800
    ) -> tuple[T, dict[str, Any]]:
        self.count += 1
        self.contexts.append(context)
        if self.failure:
            raise GatewayError("MODEL_TRANSPORT_UNKNOWN")
        return schema.model_validate(
            {
                "schema_version": "bounded-task-plan-v1",
                "action": "run",
                "rationale": "TEST ONLY two causal fixed baselines; "
                "not a real model response.",
                "evidence_refs": context["reference_ids"],
                "experiments": [
                    {
                        "tool": "run_experiment",
                        "family": "moving_average_crossover",
                        "short_window": 5,
                        "long_window": 20,
                        "fee_bps": 10,
                    },
                    {
                        "tool": "run_experiment",
                        "family": "time_series_momentum",
                        "lookback": 20,
                        "threshold_pct": "0",
                        "fee_bps": 10,
                    },
                ],
            }
        ), {"real_model_call": False, "input_tokens": 100, "output_tokens": 100}


def runtime(root: Path, provider: Planner) -> AgentRuntime:
    source, budget = root / "source", root / "budget"
    source.mkdir()
    budget.mkdir()
    return AgentRuntime(
        tools=tools(source),
        gateway=RoomGateway(budget, calls=2, tokens=40000, cost=Decimal(1)),
        provider=provider,
    )


def test_planning_recovery_reuses_exact_response_without_second_call(
    tmp_path: Path,
) -> None:
    provider = Planner()
    host = runtime(tmp_path, provider)
    task = "task-" + "a" * 32
    goal = "Compare two synthetic fixtures only."
    first = host.plan(task, goal, 2)
    assert (
        first["planner"] == "DETERMINISTIC_TEST_ONLY"
        and first["call"]["paid_model_calls"] == 0
    )
    assert len(first["steps"]) == 4
    assert host.plan(task, goal, 2) == first
    assert provider.count == 1
    with pytest.raises(PlatformError, match="INPUT_CONFLICT"):
        host.plan(task, "Changed research goal after response.", 2)


def test_unknown_model_effect_is_not_replayed_even_with_remaining_budget(
    tmp_path: Path,
) -> None:
    provider = Planner(failure=True)
    host = runtime(tmp_path, provider)
    task = "task-" + "b" * 32
    with pytest.raises(GatewayError, match="TRANSPORT_UNKNOWN"):
        host.plan(task, "Compare two synthetic fixtures only.", 2)
    assert host.budget()["calls_remaining"] == 1
    with pytest.raises(PlatformError, match="NO_RETRY"):
        host.plan(task, "Compare two synthetic fixtures only.", 2)
    assert provider.count == 1


def test_host_retrieval_context_is_frozen_and_fake_stays_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = Planner()
    host = runtime(tmp_path, provider)
    source = {
        "id": "fixture",
        "text": "TEST ONLY permitted source",
        "scope": "DEVELOPMENT",
        "claims": [],
        "content_hash": "1" * 64,
        "source_identity": "2" * 64,
        "permission": "OWNER_AUTHORIZED_LOCAL_PROJECT_CONTENT",
        "available_at": "2020-01-01T00:00:00Z",
    }
    searches: list[str] = []

    def search(query: str, **_: Any) -> dict[str, Any]:
        searches.append(query)
        return {
            "ranking": "SQLITE_FTS5_BM25_FILTER_BEFORE_INDEX",
            "as_of": "2026-10-14T00:00:00Z",
            "matches": [source],
        }

    # Exercise production composition with an explicitly fake provider.
    host._test_provider = None
    monkeypatch.setattr(host, "provider", lambda: provider)
    monkeypatch.setattr("qf_platform.knowledge.query_corpus", search)
    task, goal = "task-" + "c" * 32, "Compare synthetic fixtures with permitted notes."
    first = host.plan(task, goal, 2)
    assert first["planner"] == "DETERMINISTIC_TEST_ONLY"
    assert first["call"]["paid_model_calls"] == 0
    assert first["knowledge"]["grants_authority"] is False
    assert first["knowledge"]["sources"] == [source]
    assert (
        "knowledge-" + source["content_hash"] in provider.contexts[0]["reference_ids"]
    )
    assert host.plan(task, goal, 2) == first
    assert provider.count == 1 and searches == [goal]
