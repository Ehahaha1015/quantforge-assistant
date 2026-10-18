from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from qf_lab.bounded_planner import CompactResearchPlan, ResearchPlan, plan_task
from qf_lab.gateway import GatewayError
from qf_lab.room_gateway import FakeRoomProvider, RoomGateway


def payload() -> dict[str, Any]:
    return {
        "schema_version": "bounded-task-plan-v1",
        "action": "run",
        "rationale": "Compare simple causal baselines, with no alpha qualification.",
        "evidence_refs": [],
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


class Planner(FakeRoomProvider):
    def __init__(self, value: dict[str, Any], *, failure: str | None = None) -> None:
        super().__init__("researcher")
        self.value, self.failure, self.count = value, failure, 0

    def complete_structured(
        self,
        context: dict[str, Any],
        schema: type[BaseModel],
        *,
        max_output: int = 1800,
    ) -> tuple[Any, dict[str, Any]]:
        self.count += 1
        if self.failure:
            raise GatewayError(self.failure)
        return schema.model_validate(self.value), {
            "real_model_call": False,
            "input_tokens": 100,
            "output_tokens": 100,
        }


def run(root: Path, provider: Planner, *, budget: int = 2) -> tuple[str, ResearchPlan]:
    return plan_task(
        RoomGateway(root, calls=1, tokens=20000, cost=Decimal(1)),
        provider,
        goal="Compare moving averages and momentum.",
        dataset={"symbol": "TEST_ONLY", "prior_exposure": "TEST_FIXTURE_NOT_REAL_DATA"},
        reference_ids=(),
        max_experiments=budget,
        task_id="test-task",
    )


def test_closed_plan_is_intent_only_and_logged_as_fake(tmp_path: Path) -> None:
    call_id, plan = run(tmp_path, Planner(payload()))
    assert (
        len(plan.experiments) == 2 and plan.experiments[1].parameters["lookback"] == 20
    )
    ledger = RoomGateway(tmp_path, calls=1, tokens=20000, cost=Decimal(1)).ledger()
    assert ledger[-1]["call_id"] == call_id and ledger[-1]["real_model_call"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"tool": "submit_live_order"},
        {"fee_bps": 30},
        {"short_window": True},
        {"long_window": 51},
        {"metrics": {"total_return": 0.9}},
        {"api_key": "fixture"},
    ],
)
def test_unknown_tools_units_metrics_secrets_and_parameters_are_rejected(
    change: dict[str, Any],
) -> None:
    value = payload()
    value["experiments"][0].update(change)
    with pytest.raises(ValueError):
        ResearchPlan.model_validate(value)


def test_refusal_has_no_experiments_and_duplicate_intents_are_invalid() -> None:
    value = payload()
    with pytest.raises(ValueError):
        ResearchPlan.model_validate({**value, "action": "refuse"})
    assert not ResearchPlan.model_validate(
        {**value, "action": "refuse", "experiments": []}
    ).experiments
    with pytest.raises(ValueError):
        ResearchPlan.model_validate(
            {**value, "experiments": [value["experiments"][0]] * 2}
        )


def test_reference_and_task_budget_are_independent_host_checks(tmp_path: Path) -> None:
    value = payload()
    value["evidence_refs"] = ["fabricated-reference"]
    with pytest.raises(GatewayError, match="REFERENCE_INVALID"):
        run(tmp_path, Planner(value))
    value = payload()
    value["experiments"].append({**value["experiments"][1], "lookback": 30})
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(GatewayError, match="BUDGET_EXCEEDED"):
        run(other, Planner(value))


def test_unknown_transport_spends_reserved_call_and_never_auto_retries(
    tmp_path: Path,
) -> None:
    provider = Planner(payload(), failure="MODEL_TRANSPORT_UNKNOWN")
    with pytest.raises(GatewayError, match="TRANSPORT_UNKNOWN"):
        run(tmp_path, provider)
    with pytest.raises(GatewayError, match="BUDGET_EXHAUSTED"):
        run(tmp_path, provider)
    assert provider.count == 1


@pytest.mark.parametrize("model", [ResearchPlan, CompactResearchPlan])
@pytest.mark.parametrize(
    "change",
    [
        {"tool": "submit_live_order"}, {"fee_bps": 30}, {"short_window": True},
        {"long_window": 51}, {"metrics": {"total_return": 9}},
        {"family": "arbitrary_code"},
    ],
)
def test_compact_schema_keeps_original_intent_admission(model, change) -> None:
    value = payload()
    value["experiments"][0].update(change)
    with pytest.raises(ValueError):
        model.model_validate(value)


def test_host_audit_context_is_not_sent_and_is_checked_before_persistence(
    tmp_path: Path,
) -> None:
    provider = Planner(payload())
    contexts = []

    def check(context):
        contexts.append(context)
        if "secret" in context:
            raise GatewayError("MODEL_CONTEXT_OR_CREDENTIAL_DENIED")

    provider.check_context = check
    gateway = RoomGateway(tmp_path, calls=1, tokens=20000, cost=Decimal(1))
    with pytest.raises(GatewayError, match="CREDENTIAL_DENIED"):
        gateway.call(
            "researcher", provider, {}, CompactResearchPlan,
            audit_context={"secret": "TEST_ONLY"},
        )
    assert provider.count == 0 and gateway.ledger() == []
    assert contexts == [{}, {"secret": "TEST_ONLY"}]
