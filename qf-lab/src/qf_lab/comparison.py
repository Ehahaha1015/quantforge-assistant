"""Frozen, evidence-matched component comparison; no new Core trial authority."""

from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from quantforge.product.storage import local_root, writer_lock

from .context_view import bounded_context
from .gateway import (
    FAMILIES,
    GatewayError,
    ResearchDecision,
    canonical,
    strategy_identity,
)
from .room_gateway import CriticDecision, RoomGateway, StructuredProvider

ARMS = ("fixed", "random", "researcher", "critic", "memory")
CALLS = {"fixed": 0, "random": 0, "researcher": 2, "critic": 3, "memory": 3}


class EvidenceReader(Protocol):
    def model_context(self, run_id: str) -> dict[str, Any]: ...
    def reproduce(self, run_id: str) -> dict[str, Any]: ...


def write_new(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical(value) + "\n")


def read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise GatewayError("COMPARISON_STORE_REDIRECTED")
    return dict(json.loads(path.read_text(encoding="utf-8")))


def rejection(evidence: dict[str, Any]) -> dict[str, Any]:
    """Predeclared mechanical negative screen, never a model/alpha certification."""
    facts = evidence["FACT"]
    reasons = []
    if not facts["screen_pass"]:
        reasons.append("CORE_DRAWDOWN_SCREEN_FAILED")
    if facts["development_metrics"]["total_return"] <= 0:
        reasons.append("NONPOSITIVE_DEVELOPMENT_RETURN_AFTER_DECLARED_FEES")
    return {"reject": bool(reasons), "reasons": reasons, "alpha": "NOT_ESTABLISHED"}


def candidate_id(proposal: ResearchDecision, candidates: list[dict[str, Any]]) -> str:
    matches = [
        v["id"]
        for v in candidates
        if strategy_identity(v["strategy"]) == strategy_identity(proposal.strategy)
    ]
    if len(matches) != 1:
        raise GatewayError("COMPARISON_CANDIDATE_OUTSIDE_FROZEN_SPACE")
    return str(matches[0])


def memory_bindings(plan: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
    """Host labels dataset identity; historical opinions cannot declare a new sample."""
    current = plan["dataset"]
    historical = memory.get("historical", {})
    sources = {}
    for item in historical.get("lessons", []) + historical.get("prior_hypotheses", []):
        source_id = item["program_id"]
        sources[source_id] = {
            "program_id": source_id,
            "dataset_id": item["dataset_id"],
            "dataset_content_hash": item["dataset_content_hash"],
            "relation": "SAME_DATASET"
            if item["dataset_id"] == current["dataset_id"]
            or item["dataset_content_hash"] == current["content_hash"]
            else "DIFFERENT_SNAPSHOT_NOT_PROVEN_INDEPENDENT",
        }
    return {
        "current_dataset_id": current["dataset_id"],
        "content_hash": current["content_hash"],
        "historical_sources": [sources[k] for k in sorted(sources)],
        "own_prior_campaigns_relation": "SAME_DATASET_SAME_PARTITIONS",
        "interpretation": "Repeated campaigns are not independent datasets; host identity labels override model guesses",
    }


def unseen_candidates(
    candidates: list[dict[str, Any]],
    prior_choices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cheap host-side exclusion for a future plan, without model calls or new trials.

    The frozen comparison deliberately retains/counts repeated selections.
    This helper never silently changes its candidate universe or its results.
    """
    seen = {strategy_identity(v["strategy"]) for v in prior_choices}
    return [v for v in candidates if strategy_identity(v["strategy"]) not in seen]


def comparison_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = {}
    for arm in ARMS:
        rows = sorted(
            (v for v in results if v["arm"] == arm), key=lambda v: v["replicate"]
        )
        choices = [c for v in rows for c in v["choices"]]
        seen: set[str] = set()
        duplicates = 0
        within = 0
        for row in rows:
            ids = [v["candidate_id"] for v in row["choices"]]
            within += len(ids) - len(set(ids))
            for identity in ids:
                duplicates += identity in seen
                seen.add(identity)
        finished = sum(v["status"] == "COMPLETE" for v in rows)
        cost = sum((Decimal(v["budget"]["spent_cost"]) for v in rows), Decimal(0))
        rejected = sum(v["screen"]["reject"] for v in choices)
        supported_critics = sum(
            (v.get("critic") or {}).get("verdict") == "reject"
            and bool(v["choices"])
            and v["choices"][0]["screen"]["reject"]
            for v in rows
        )
        unsupported_critics = sum(
            (v.get("critic") or {}).get("verdict") == "reject"
            and bool(v["choices"])
            and not v["choices"][0]["screen"]["reject"]
            for v in rows
        )
        improvements = [
            v["choices"][1]["development_return"]
            - v["choices"][0]["development_return"]
            for v in rows
            if len(v["choices"]) == 2
        ]
        summaries[arm] = {
            "attempted_campaigns": len(rows),
            "completed_campaigns": finished,
            "technical_failures": sum(v["status"] == "FAILED" for v in rows),
            "blocked_campaigns": sum(v["status"] == "BLOCKED" for v in rows),
            "selected_candidates": len(choices),
            "unique_candidates": len(seen),
            "repeated_selections": duplicates,
            "within_campaign_duplicates": within,
            "repeat_rate": duplicates / len(choices) if choices else None,
            "screen_supported_negative_selections": rejected,
            "unique_screen_supported_negatives": len(
                {v["candidate_id"] for v in choices if v["screen"]["reject"]}
            ),
            "screen_supported_critic_rejections": supported_critics,
            "unsupported_critic_rejections": unsupported_critics,
            "critic_rationale_semantically_certified": False,
            "calls": sum(v["budget"]["spent_calls"] for v in rows),
            "declared_role_cost_usd": str(cost),
            "cost_per_completed_campaign": str(cost / finished) if finished else None,
            "cost_per_unique_negative": str(
                cost
                / len({v["candidate_id"] for v in choices if v["screen"]["reject"]})
            )
            if any(v["screen"]["reject"] for v in choices)
            else None,
            "elapsed_seconds": sum(v["elapsed_seconds"] for v in rows),
            "revision_return_changes_descriptive": improvements,
            "distinct_revision_count": sum(
                len(v["choices"]) == 2
                and v["choices"][0]["candidate_id"] != v["choices"][1]["candidate_id"]
                for v in rows
            ),
            "valid_proposals": sum(v["valid_proposals"] for v in rows),
            "proposal_opportunities": 2 * len(rows),
            "not_sent_or_not_reached_are_not_valid_proposals": True,
        }
    return {
        "schema_version": "research-component-comparison-v1",
        "arms": summaries,
        "campaigns": len(results),
        "completed": sum(v["status"] == "COMPLETE" for v in results),
        "technical_failures": sum(v["status"] == "FAILED" for v in results),
        "calls": sum(v["budget"]["spent_calls"] for v in results),
        "declared_role_cost_usd": str(
            sum((Decimal(v["budget"]["spent_cost"]) for v in results), Decimal(0))
        ),
        "same_history_repetitions_are_not_independent_market_samples": True,
        "alpha": "NOT_ESTABLISHED",
        "final_holdout_opened": False,
        "rationale_semantic_accuracy_requires_external_review": True,
    }


class Comparison:
    def __init__(
        self, root: Path, core: EvidenceReader, provider: StructuredProvider
    ) -> None:
        self.root = local_root(root)
        self.core, self.provider = core, provider

    def freeze(self, plan: dict[str, Any]) -> None:
        if (
            plan["arms"] != list(ARMS)
            or not 1 <= plan["replicates"] <= 5
            or plan["views_per_campaign"] != 2
            or len(plan["candidates"]) != 6
            or len({v["id"] for v in plan["candidates"]}) != 6
            or len({strategy_identity(v["strategy"]) for v in plan["candidates"]}) != 6
            or plan["wall_seconds"] < 30
        ):
            raise GatewayError("COMPARISON_PLAN_INVALID")
        for candidate in plan["candidates"]:
            proposal = ResearchDecision.model_validate(
                {
                    **candidate["strategy"]["parameters"],
                    "family": candidate["strategy"]["family"],
                    "action": "propose",
                    "rationale": "Frozen scripted candidate space",
                    "evidence_refs": [],
                }
            )
            if proposal.warmup > plan["max_long_window"]:
                raise GatewayError("COMPARISON_PLAN_INVALID")
        identity = hashlib.sha256(canonical(plan).encode()).hexdigest()
        document = {"plan_id": identity, "plan": plan}
        path = self.root / "plan.json"
        with writer_lock(self.root):
            if path.exists():
                if read(path) != document:
                    raise GatewayError("COMPARISON_PLAN_IMMUTABLE")
            else:
                write_new(path, document)

    def _evidence(self, candidate: dict[str, Any]) -> dict[str, Any]:
        evidence = self.core.model_context(candidate["run_id"])
        if (
            evidence["evidence_id"] != candidate["evidence_id"]
            or strategy_identity(evidence["strategy"])
            != strategy_identity(candidate["strategy"])
            or evidence["FACT"]["final_lockbox_scored"]
        ):
            raise GatewayError("COMPARISON_EVIDENCE_BINDING_MISMATCH")
        if self.core.reproduce(candidate["run_id"])["result"] != "EXACT_MATCH":
            raise GatewayError("CORE_REPRODUCTION_MISMATCH")
        return evidence

    def memory(self, plan: dict[str, Any], replicate: int) -> dict[str, Any]:
        # Only prior completed campaigns in this arm; no other arm's outcomes.
        prior = []
        for index in range(replicate):
            path = self.root / f"memory-{index}" / "result.json"
            if path.is_file():
                value = read(path)
                if value["status"] == "COMPLETE":
                    prior.append(
                        {
                            "campaign": f"memory-{index}",
                            "choices": value["choices"],
                            "critic": value.get("critic"),
                            "result_sha256": hashlib.sha256(
                                canonical(value).encode()
                            ).hexdigest(),
                        }
                    )
        return {
            "historical": plan["historical_memory"],
            "previous_completed_memory_campaigns": prior,
            "authority": "Historical opinions and retained evidence references, no alpha or permissions",
        }

    def run(self, arm: str, replicate: int) -> dict[str, Any]:
        frozen = read(self.root / "plan.json")
        plan = frozen["plan"]
        if hashlib.sha256(canonical(plan).encode()).hexdigest() != frozen["plan_id"]:
            raise GatewayError("COMPARISON_PLAN_BINDING_MISMATCH")
        if plan.get("provider_identity") != {
            "profile_id": self.provider.profile_id,
            "model": self.provider.model,
            "endpoint_identity": self.provider.endpoint_identity,
            "binding_revision": getattr(
                self.provider, "binding_revision", "ENV_REFERENCE"
            ),
        }:
            raise GatewayError("COMPARISON_PROVIDER_BINDING_MISMATCH")
        if (
            arm not in ARMS
            or type(replicate) is not int
            or not 0 <= replicate < plan["replicates"]
        ):
            raise GatewayError("COMPARISON_CAMPAIGN_INVALID")
        directory = self.root / f"{arm}-{replicate}"
        directory.mkdir(exist_ok=True)
        local_root(directory)
        with writer_lock(directory):
            if (directory / "result.json").exists():
                return read(directory / "result.json")
            if (directory / "intent.json").exists():
                # Interrupted paid activity is retained, never silently replayed.
                raise GatewayError("COMPARISON_INTERRUPTED_RETAINED_NO_REPLAY")
            gateway = RoomGateway(
                directory,
                calls=max(1, CALLS[arm]),
                tokens=120000,
                cost=Decimal("0.10"),
            )
            memory = self.memory(plan, replicate) if arm == "memory" else {}
            write_new(
                directory / "intent.json",
                {
                    "plan_id": frozen["plan_id"],
                    "arm": arm,
                    "replicate": replicate,
                    "declared_at": datetime.now(UTC).isoformat(),
                    "memory": memory,
                    "budget": gateway.policy,
                    "retry": "FORBIDDEN",
                },
            )
            result: dict[str, Any] = {
                "plan_id": frozen["plan_id"],
                "arm": arm,
                "replicate": replicate,
                "status": "IN_PROGRESS",
                "choices": [],
                "valid_proposals": 0,
                "critic": None,
                "failure": None,
            }
            # Freeze a nonadaptive sequence without replacement within random campaigns.
            indices = (
                [0, 1]
                if arm == "fixed"
                else random.Random(plan["seed"] + replicate).sample(range(6), 2)
            )
            started = time.monotonic()
            evidence: list[dict[str, Any]] = []
            try:
                for opportunity in range(2):
                    if time.monotonic() - started >= plan["wall_seconds"]:
                        raise GatewayError("COMPARISON_WALL_BUDGET_EXHAUSTED")
                    context = {
                        "phase": "first" if opportunity == 0 else "next",
                        "goal": plan["goal"],
                        "round": replicate + 1,
                        "max_long_window": plan["max_long_window"],
                        "allowed_families": list(FAMILIES),
                        "allowed_actions": [
                            "propose" if opportunity == 0 else "modify"
                        ],
                        "candidate_space": [
                            {"id": v["id"], "strategy": v["strategy"]}
                            for v in plan["candidates"]
                        ],
                        "candidate_space_rule": "Select exactly one supplied family/parameter combination. In phase next select a distinct candidate. No new parameters are admitted.",
                        "evidence": evidence,
                        "research_memory": memory,
                        "dataset_identity": plan["dataset"],
                        "memory_dataset_bindings": memory_bindings(plan, memory),
                        "parameter_unit_reminder": "Core k_pct is percent of population standard deviation; 200.0 is 2 sigma and 2.0 is 0.02 sigma. Preserve these units when discussing prior candidates.",
                        "critics": [result["critic"]] if result["critic"] else [],
                        "budget": {
                            "candidate_views_remaining": 2 - opportunity,
                            "campaign_cost_cap_usd": "0.10",
                        },
                        "final_holdout": "SEALED",
                    }
                    if arm in {"fixed", "random"}:
                        selected = plan["candidates"][indices[opportunity]]
                        proposal = ResearchDecision.model_validate(
                            {
                                **selected["strategy"]["parameters"],
                                "family": selected["strategy"]["family"],
                                "action": context["allowed_actions"][0],
                                "rationale": "Predeclared fixed pair"
                                if arm == "fixed"
                                else "Predeclared seeded uniform sampling without replacement within campaign",
                                "evidence_refs": [v["evidence_id"] for v in evidence],
                            }
                        )
                        call_id = "SCRIPTED_NO_MODEL_REQUEST"
                    else:
                        call_id, proposal = gateway.call(
                            "researcher",
                            self.provider,
                            bounded_context(context),
                            ResearchDecision,
                        )
                        identity = candidate_id(proposal, plan["candidates"])
                        selected = next(
                            v for v in plan["candidates"] if v["id"] == identity
                        )
                    if selected["id"] in {v["candidate_id"] for v in result["choices"]}:
                        raise GatewayError("COMPARISON_REVISION_MUST_BE_DISTINCT")
                    result["valid_proposals"] += 1
                    current = self._evidence(selected)
                    evidence.append(current)
                    choice = {
                        "candidate_id": selected["id"],
                        "run_id": selected["run_id"],
                        "evidence_id": selected["evidence_id"],
                        "strategy": selected["strategy"],
                        "proposal": proposal.model_dump(mode="json"),
                        "call_id": call_id,
                        "screen": rejection(current),
                        "development_return": current["FACT"]["development_metrics"][
                            "total_return"
                        ],
                        "exact_reproduction": True,
                        "new_core_trial": False,
                    }
                    result["choices"].append(choice)
                    write_new(directory / f"choice-{opportunity}.json", choice)
                    if opportunity == 0 and arm in {"critic", "memory"}:
                        if time.monotonic() - started >= plan["wall_seconds"]:
                            raise GatewayError("COMPARISON_WALL_BUDGET_EXHAUSTED")
                        _, critic = gateway.call(
                            "critic",
                            self.provider,
                            bounded_context(
                                {
                                    **context,
                                    "phase": "critic",
                                    "evidence": evidence,
                                    "selected_proposal": proposal.model_dump(
                                        mode="json"
                                    ),
                                    "verdict_scope": "Assess the currently selected candidate, separately from the decision to spend the next candidate view. The host applies the predeclared screen; opinions cannot override Core facts.",
                                    "budget": {
                                        "candidate_views_remaining": 1,
                                        "campaign_cost_cap_usd": "0.10",
                                    },
                                }
                            ),
                            CriticDecision,
                        )
                        result["critic"] = critic.model_dump(mode="json")
                        write_new(directory / "critic.json", result["critic"])
                result["status"] = "COMPLETE"
            except (GatewayError, ValueError, OSError) as exc:
                result["status"] = "FAILED"
                result["failure"] = (
                    str(exc)
                    if isinstance(exc, GatewayError)
                    else "COMPARISON_EXECUTION_FAILED"
                )
            result["budget"] = gateway.budget()
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            write_new(directory / "result.json", result)
            return result
