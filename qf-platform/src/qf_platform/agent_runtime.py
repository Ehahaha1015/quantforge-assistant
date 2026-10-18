"""Protected planner composition and explicit reconciliation of prior call effects."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from qf_lab.bounded_planner import ResearchPlan, plan_task
from qf_lab.room_gateway import RoomGateway, StructuredProvider

from .agent_tools import DATASET, AgentTools
from .bindings import BindingRegistry
from .dataset_use import WORKSPACE
from .storage import PlatformError, digest

PROFILE_ID = "binding-db8f755ee0d04c76a195c46e30220198"
PROTECTED_BINDINGS = WORKSPACE / "local-smoke/real-bindings-20261011/console-runtime"
GOAL_BUDGET = WORKSPACE / "local-smoke/research-governance/agent-goal-20261013"
CORE_ROOT = WORKSPACE / "local-smoke/research-governance/agent-core"


class AgentRuntime:
    def __init__(
        self,
        *,
        tools: AgentTools | None = None,
        gateway: RoomGateway | None = None,
        provider: StructuredProvider | None = None,
    ) -> None:
        self.tools = AgentTools(CORE_ROOT) if tools is None else tools
        self.gateway = (
            RoomGateway(
                GOAL_BUDGET,
                calls=18,
                tokens=140000,
                cost=Decimal("1.00"),
                require_existing=True,
            )
            if gateway is None
            else gateway
        )
        self._test_provider = provider

    def provider(self) -> StructuredProvider:
        if self._test_provider is not None:
            return self._test_provider
        registry = BindingRegistry(PROTECTED_BINDINGS)
        profile = registry.get(PROFILE_ID)
        if (
            profile["health"] != "HEALTHY"
            or profile["cost_currency"] != "USD"
            or Decimal(profile["input_price_per_million"]) < Decimal("0.30")
            or Decimal(profile["output_price_per_million"]) < Decimal("1.20")
        ):
            raise PlatformError("AGENT_MODEL_HEALTH_OR_PRICING_DENIED")
        return cast(StructuredProvider, registry.model_provider(PROFILE_ID))

    def preflight(self, max_experiments: int) -> dict[str, Any]:
        metadata = self.tools.inspect_dataset(DATASET)
        if (
            type(max_experiments) is not int
            or max_experiments not in {2, 4, 6}
            or max_experiments > metadata["core_trials_remaining"]
        ):
            raise PlatformError("AGENT_CORE_TRIAL_BUDGET_EXHAUSTED")
        if self.gateway.budget()["calls_remaining"] < 1:
            raise PlatformError("AGENT_GOAL_MODEL_BUDGET_EXHAUSTED")
        return metadata

    def _record(self, call_id: str) -> dict[str, Any]:
        rows = [
            value for value in self.gateway.ledger() if value.get("call_id") == call_id
        ]
        request = next(value for value in rows if value["phase"] == "REQUEST")
        result = next(
            (value for value in rows if value["phase"] in {"RESPONSE", "ERROR"}), None
        )
        return {
            "call_id": call_id,
            "status": "UNKNOWN" if result is None else result["phase"],
            "reserved_cost_usd": request["reserved_cost"],
            "reserved_tokens": request["reserved_tokens"],
            "charged_cost_usd": result.get("charged_cost") if result else None,
            "usage_known": result.get("usage_known", False) if result else False,
            "paid_model_calls": int(
                bool(result and result.get("real_model_call") is True)
            )
            if result and result["phase"] == "RESPONSE"
            else int(not result or result.get("send_status") != "NOT_SENT_VERIFIED"),
            "model": request["model"],
            "response_hash": result.get("response_hash") if result else None,
        }

    def reconcile(
        self, task_id: str, goal: str, max_experiments: int
    ) -> dict[str, Any] | None:
        requests = [
            value
            for value in self.gateway.ledger()
            if value["phase"] == "REQUEST"
            and value.get("schema") in {"ResearchPlan", "CompactResearchPlan"}
            and value.get("context", {}).get("task_id") == task_id
        ]
        if not requests:
            return None
        if len(requests) != 1:
            raise PlatformError("AGENT_MODEL_ATTEMPT_CONFLICT")
        request = requests[0]
        from .planner_context import original_context

        try:
            context = original_context(request)
        except (KeyError, TypeError, ValueError):
            raise PlatformError("AGENT_MODEL_PROVENANCE_REPLAY_CONFLICT") from None
        if (
            context["goal"] != goal.strip()
            or context["max_experiments"] != max_experiments
        ):
            raise PlatformError("AGENT_MODEL_REPLAY_INPUT_CONFLICT")
        response = next(
            (
                value
                for value in self.gateway.ledger()
                if value.get("call_id") == request["call_id"]
                and value["phase"] == "RESPONSE"
            ),
            None,
        )
        if response is None:
            raise PlatformError("AGENT_MODEL_EFFECT_REQUIRES_REVIEW_NO_RETRY")
        plan = ResearchPlan.model_validate(response["decision"])
        current = self.tools.inspect_dataset(DATASET)
        original = context["dataset"]
        if any(
            current[key] != original[key]
            for key in ("inventory_hash", "snapshot_hash", "dataset_snapshot")
        ):
            raise PlatformError("AGENT_MODEL_SOURCE_REPLAY_CONFLICT")
        if len(plan.experiments) > max_experiments:
            raise PlatformError("AGENT_MODEL_PLAN_BUDGET_DENIED")
        call = self._record(request["call_id"])
        steps = (
            []
            if plan.action == "refuse"
            else [
                {"tool": "inspect_dataset", "input": {"dataset": DATASET}},
                *[
                    {"tool": "run_experiment", "input": intent.model_dump(mode="json")}
                    for intent in plan.experiments
                ],
                {"tool": "compare_results", "input": {}},
            ]
        )
        return {
            "planner": (
                "BOUNDED_REAL_LLM_BM25_V2"
                if "knowledge" in context
                else "BOUNDED_REAL_LLM_V1"
            )
            if call["paid_model_calls"]
            else "DETERMINISTIC_TEST_ONLY",
            "model_plan": plan.model_dump(mode="json"),
            "call": call,
            "steps": steps,
            "dataset_metadata": context["dataset"],
            "knowledge": context.get("knowledge"),
            "live_authority": "NONE",
        }

    def plan(self, task_id: str, goal: str, max_experiments: int) -> dict[str, Any]:
        previous = self.reconcile(task_id, goal, max_experiments)
        if previous is not None:
            return previous
        metadata = self.preflight(max_experiments)
        reference = "dataset-meta-" + digest(metadata)
        knowledge = None
        references = (reference,)
        if self._test_provider is None:
            from datetime import UTC, datetime

            from .knowledge import query_corpus

            search = query_corpus(goal, as_of=datetime.now(UTC), limit=3)
            matches = search["matches"]
            knowledge = {
                "ranking": search["ranking"],
                "as_of": search["as_of"],
                "sources": [
                    {
                        key: item[key]
                        for key in (
                            "id",
                            "text",
                            "scope",
                            "claims",
                            "content_hash",
                            "source_identity",
                            "permission",
                            "available_at",
                        )
                    }
                    for item in matches
                ],
                "grants_authority": False,
            }
            references += tuple("knowledge-" + item["content_hash"] for item in matches)
        from .planner_context import compact_dataset, compact_knowledge

        compact = knowledge is not None
        plan_task(
            self.gateway,
            self.provider(),
            goal=goal,
            dataset=compact_dataset(metadata) if compact else metadata,
            reference_ids=references,
            max_experiments=max_experiments,
            task_id=task_id,
            knowledge=compact_knowledge(knowledge) if knowledge is not None else None,
            compact=compact,
            audit_context=(
                {"dataset": metadata, "knowledge": knowledge} if compact else None
            ),
        )
        result = self.reconcile(task_id, goal, max_experiments)
        if result is None:
            raise PlatformError("AGENT_MODEL_RESPONSE_NOT_COMMITTED")
        return result

    def budget(self) -> dict[str, Any]:
        return self.gateway.budget()

    def effective_budget(self) -> dict[str, Any]:
        goal = self.budget()
        if self._test_provider is not None:
            return {
                key: goal[key]
                for key in ("calls_remaining", "tokens_remaining", "cost_remaining")
            }
        profile = next(
            value
            for value in BindingRegistry(PROTECTED_BINDINGS).model_usage()
            if value["profile_id"] == PROFILE_ID
        )
        return {
            "calls_remaining": min(goal["calls_remaining"], profile["calls_remaining"]),
            "tokens_remaining": min(
                goal["tokens_remaining"], profile["tokens_remaining"]
            ),
            "cost_remaining": str(
                min(Decimal(goal["cost_remaining"]), Decimal(profile["cost_remaining"]))
            ),
        }

    def call_state(self, task_id: str) -> dict[str, Any] | None:
        requests = [
            value
            for value in self.gateway.ledger()
            if value["phase"] == "REQUEST"
            and value.get("schema") in {"ResearchPlan", "CompactResearchPlan"}
            and value.get("context", {}).get("task_id") == task_id
        ]
        if len(requests) > 1:
            raise PlatformError("AGENT_MODEL_ATTEMPT_CONFLICT")
        return self._record(requests[0]["call_id"]) if requests else None
