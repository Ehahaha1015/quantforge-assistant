from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from qf_lab.comparison import (
    ARMS,
    Comparison,
    comparison_summary,
    memory_bindings,
    rejection,
    unseen_candidates,
    write_new,
)
from qf_lab.gateway import GatewayError, ResearchDecision, canonical
from qf_lab.room_gateway import FakeRoomProvider


class Core:
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self.candidates = candidates
        self.reproduced: list[str] = []

    def model_context(self, run_id: str) -> dict[str, Any]:
        selected = next(v for v in self.candidates if v["run_id"] == run_id)
        return {
            "evidence_id": selected["evidence_id"],
            "strategy": selected["strategy"],
            "FACT": {
                "final_lockbox_scored": False,
                "screen_pass": False,
                "development_metrics": {"total_return": -0.2},
            },
        }

    def reproduce(self, run_id: str) -> dict[str, Any]:
        self.reproduced.append(run_id)
        return {"result": "EXACT_MATCH"}


class Provider(FakeRoomProvider):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[dict[str, Any]] = []

    def complete_structured(self, context, schema, *, max_output=1800):
        self.contexts.append(deepcopy(context))
        if schema is ResearchDecision:
            choice = context["candidate_space"][0 if context["phase"] == "first" else 1]
            return schema.model_validate(
                {
                    "action": context["allowed_actions"][0],
                    "family": choice["strategy"]["family"],
                    **choice["strategy"]["parameters"],
                    "rationale": "Restricted comparison fixture",
                    "evidence_refs": [v["evidence_id"] for v in context["evidence"]],
                }
            ), {"input_tokens": 0, "output_tokens": 0}
        return super().complete_structured(context, schema, max_output=max_output)


def setup(root: Path) -> tuple[Comparison, Core, Provider, dict[str, Any]]:
    provider = Provider()
    candidates = [
        {
            "id": f"c{i}",
            "run_id": f"run{i}",
            "evidence_id": f"e{i}",
            "strategy": {
                "family": "moving_average_crossover",
                "parameters": {"short_window": i + 1, "long_window": 20},
            },
        }
        for i in range(6)
    ]
    core = Core(candidates)
    plan = {
        "arms": list(ARMS),
        "replicates": 3,
        "views_per_campaign": 2,
        "candidates": candidates,
        "max_long_window": 50,
        "wall_seconds": 300,
        "historical_memory": {"prior": "old opinions"},
        "dataset": {"dataset_id": "fixture", "content_hash": "frozen"},
        "seed": 17,
        "goal": "Restricted causal comparison",
        "provider_identity": {
            "profile_id": provider.profile_id,
            "model": provider.model,
            "endpoint_identity": provider.endpoint_identity,
            "binding_revision": "ENV_REFERENCE",
        },
    }
    comparison = Comparison(root, core, provider)
    comparison.freeze(plan)
    return comparison, core, provider, plan


def test_all_arms_use_same_facts_and_completed_runs_do_not_replay(
    tmp_path: Path,
) -> None:
    comparison, core, provider, _ = setup(tmp_path)
    results = [comparison.run(arm, replicate) for replicate in range(3) for arm in ARMS]
    assert all(v["status"] == "COMPLETE" for v in results)
    assert len(core.reproduced) == 30
    assert len(provider.contexts) == 24
    assert comparison.run("memory", 2) == results[-1]
    assert len(provider.contexts) == 24
    summary = comparison_summary(results)
    assert summary["arms"]["fixed"]["repeated_selections"] == 4
    assert summary["arms"]["critic"]["screen_supported_critic_rejections"] == 3
    assert summary["calls"] == 24
    assert summary["completed"] == 15


def test_only_memory_arm_sees_prior_completed_own_campaigns(tmp_path: Path) -> None:
    comparison, _, provider, _ = setup(tmp_path)
    comparison.run("memory", 0)
    comparison.run("critic", 0)
    comparison.run("memory", 1)
    contexts = provider.contexts
    assert all(v["research_memory"] == {} for v in contexts[3:6])
    prior = contexts[6]["research_memory"]["previous_completed_memory_campaigns"]
    assert len(prior) == 1 and prior[0]["campaign"] == "memory-0"
    assert len(contexts[6]["evidence"]) == 0


def test_researcher_and_critic_first_prompts_match_before_intervention(
    tmp_path: Path,
) -> None:
    comparison, _, provider, _ = setup(tmp_path)
    comparison.run("researcher", 0)
    comparison.run("critic", 0)
    assert provider.contexts[0] == provider.contexts[2]
    assert provider.contexts[1]["critics"] == []
    assert provider.contexts[4]["critics"][0]["verdict"] == "reject"
    assert provider.contexts[3]["budget"]["candidate_views_remaining"] == 1
    assert (
        provider.contexts[3]["selected_proposal"]["family"]
        == "moving_average_crossover"
    )


def test_host_deduplication_uses_identity_and_never_needs_a_model(
    tmp_path: Path,
) -> None:
    _, _, provider, plan = setup(tmp_path)
    prior = [{"strategy": plan["candidates"][0]["strategy"]}]
    assert len(unseen_candidates(plan["candidates"], prior)) == 5
    assert unseen_candidates(plan["candidates"], plan["candidates"]) == []
    assert provider.contexts == []
    alias = [
        {
            "strategy": {
                "family": "bollinger_reversion",
                "parameters": {"window": 20, "k_pct": "200.00"},
            }
        }
    ]
    candidate = [
        {
            "id": "b",
            "strategy": {
                "family": "bollinger_reversion",
                "parameters": {"window": 20, "k_pct": "200.0"},
            },
        }
    ]
    assert unseen_candidates(candidate, alias) == []


def test_memory_bindings_explicitly_distinguish_same_and_other_snapshots(
    tmp_path: Path,
) -> None:
    _, _, _, plan = setup(tmp_path)
    memory = {
        "historical": {
            "lessons": [
                {
                    "program_id": "old",
                    "dataset_id": "fixture",
                    "dataset_content_hash": "frozen",
                },
                {
                    "program_id": "other",
                    "dataset_id": "other",
                    "dataset_content_hash": "other-hash",
                },
            ]
        }
    }
    bound = memory_bindings(plan, memory)
    assert bound["historical_sources"][0]["relation"] == "SAME_DATASET"
    assert (
        bound["historical_sources"][1]["relation"]
        == "DIFFERENT_SNAPSHOT_NOT_PROVEN_INDEPENDENT"
    )
    assert bound["own_prior_campaigns_relation"] == "SAME_DATASET_SAME_PARTITIONS"


def test_duplicate_revision_is_failed_before_second_evidence_view(
    tmp_path: Path,
) -> None:
    comparison, core, provider, _ = setup(tmp_path)
    original = provider.complete_structured

    def repeat(context, schema, *, max_output=1800):
        if schema is ResearchDecision:
            context = deepcopy(context)
            context["candidate_space"][1] = context["candidate_space"][0]
        return original(context, schema, max_output=max_output)

    provider.complete_structured = repeat
    result = comparison.run("researcher", 0)
    assert result["status"] == "FAILED"
    assert result["failure"] == "COMPARISON_REVISION_MUST_BE_DISTINCT"
    assert result["valid_proposals"] == 1
    assert result["budget"]["spent_calls"] == 2
    assert len(core.reproduced) == 1


def test_plan_changes_interruption_and_final_evidence_fail_closed(
    tmp_path: Path,
) -> None:
    comparison, core, provider, plan = setup(tmp_path)
    changed = deepcopy(plan)
    changed["seed"] += 1
    with pytest.raises(GatewayError, match="IMMUTABLE"):
        comparison.freeze(changed)
    directory = tmp_path / "researcher-0"
    directory.mkdir()
    write_new(directory / "intent.json", {"interrupted": True})
    with pytest.raises(GatewayError, match="NO_REPLAY"):
        comparison.run("researcher", 0)
    assert provider.contexts == []
    original = core.model_context

    def final(run_id):
        value = original(run_id)
        value["FACT"]["final_lockbox_scored"] = True
        return value

    core.model_context = final
    result = comparison.run("fixed", 0)
    assert result["status"] == "FAILED"
    assert result["choices"] == []
    assert core.reproduced == []
    (tmp_path / "plan.json").write_text(canonical({"plan": changed, "plan_id": "old"}))
    with pytest.raises(GatewayError, match="PLAN_BINDING_MISMATCH"):
        comparison.run("random", 0)


def test_negative_screen_does_not_use_positive_return_as_success() -> None:
    evidence = {
        "FACT": {"screen_pass": True, "development_metrics": {"total_return": 0.5}}
    }
    assert not rejection(evidence)["reject"]
    assert rejection(evidence)["alpha"] == "NOT_ESTABLISHED"
    evidence["FACT"]["screen_pass"] = False
    assert rejection(evidence)["reject"]
