"""Operational research quality; no profitability or alpha success labels."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .gateway import canonical


def quality(states: list[dict[str, Any]]) -> dict[str, Any]:
    rounds = sum(v["rounds_completed"] for v in states)
    errors = sum(v["status"] == "FAILED" for v in states)
    decisions = sum(
        v["status"] == "COMPLETE" and v["rounds_completed"] > 0 for v in states
    )
    duplicate = 0
    seen: set[str] = set()
    evidence = set()
    for state in states:
        for event in state["history"]:
            if event["kind"] == "EVIDENCE_REFERENCE":
                evidence.add(event["evidence_id"])
            if event["kind"] == "EXPERIMENT_PREPARE":
                identity = canonical(
                    {
                        "dataset_content_hash": state["declaration"]["dataset"][
                            "content_hash"
                        ],
                        "strategy": event["strategy"],
                    }
                )
                duplicate += identity in seen
                seen.add(identity)
    cost = sum((Decimal(v["budget"]["spent_cost"]) for v in states), Decimal(0))
    currencies = {v["declaration"]["cost_currency"] for v in states}
    if len(currencies) > 1:
        raise ValueError("quality cost aggregation requires one declared currency")
    return {
        "schema_version": "research-quality-v1",
        "campaigns": len(states),
        "completed_new_research_campaigns": decisions,
        "completed_rounds": rounds,
        "technical_failures": errors,
        "retained_review_only_campaigns": sum(
            v["reviews_completed"] > 0 and v["rounds_completed"] == 0 for v in states
        ),
        "unique_evidence_packs": len(evidence),
        "repeated_dataset_strategy_trials": duplicate,
        "spent_calls": sum(v["budget"]["spent_calls"] for v in states),
        "declared_cost": str(cost),
        "cost_currency": next(iter(currencies), None),
        "cost_per_completed_round": str(cost / rounds) if rounds else None,
        "completion_rate": decisions / len(states) if states else None,
        "alpha_status": "NOT_ESTABLISHED",
        "interpretation": "Evidence-backed rejection counts as completed research; retained review is separate; positive return is not a success metric",
    }
