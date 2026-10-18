"""Read-only evaluation projection and reproducible local human report."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agent_evaluation import OUTPUT
from .storage import digest


def read_evaluation(root: Path = OUTPUT) -> dict[str, Any]:
    for name in ("plan.json", "result.json", "plan-digest.json", "result-digest.json"):
        if (root / name).is_symlink():
            raise ValueError("redirected evaluation source")
    plan = json.loads((root / "plan.json").read_bytes())
    result = json.loads((root / "result.json").read_bytes())
    if (
        digest(plan) != json.loads((root / "plan-digest.json").read_bytes())["sha256"]
        or digest(result)
        != json.loads((root / "result-digest.json").read_bytes())["sha256"]
        or result["plan_id"] != digest(plan)
    ):
        raise ValueError("evaluation source identity mismatch")
    return {
        "schema_version": "qf-evaluation-readout-v1",
        "plan_id": digest(plan),
        "result_hash": digest(result),
        "corpus_hash": result["corpus_hash"],
        "policy": plan["suite"]["policy"],
        "evaluations": result["evaluations"],
        "batches": result["batches"],
        "budget_after": result["budget_after"],
        "effective_budget_after": result["effective_budget_after"],
        "interpretation": "Authored task suite. "
        "No independent production, market or alpha qualification.",
        "live_authority": "NONE",
        "validated_alpha": False,
    }


def render_readout(value: dict[str, Any]) -> str:
    rows = value["evaluations"]
    lines = [
        "# AGENT-LT-002: frozen task evaluation",
        "",
        "40 development / 20 frozen held questions; six authored task categories.",
        "Sources: locally owner-authorized notes and source-bound historical facts.",
        "Scope and timestamps are filtered before native SQLite FTS5 BM25 ranking.",
        "Restricted source questions run in a physically source-empty batch.",
        "",
        "| Arm | All n/N | Development n/40 | Held n/20 | Wrong Citations "
        "| Citations | Missing | Claim Errors |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| {arm} | {row['passed']}/60 | {row['development_passed']}/40 "
            f"| {row['held_passed']}/20 | {row['citation_errors']} "
            f"| {row['citation_count']} | {row['missing_answers']} "
            f"| {row['claim_errors']} |"
            for arm, row in rows.items()
        ],
        "",
        "## Interpretation",
        "",
        "Bounded tools resolve exact owned claims and derive citations; "
        "the LLM never emits metric facts.",
        "Missing citations are claim-resolution failures, "
        "not zero citation errors or verified quotations.",
        "A one-question difference is not evidence of stable improvement; "
        "held n/N must remain visible.",
        "Critic and memory use the same configured provider, "
        "not independent human certification.",
        "Rules and reference questions share project-authored methodology; "
        "authoring bias remains.",
        "Short templated questions and one run do not estimate production accuracy.",
        "No vector index is promoted without an independent measured need.",
        "",
        "## Cost and Timing",
        "",
    ]
    for arm, batches in value["batches"].items():
        lines.append(
            f"- {arm}: {sum(row['duration_ms'] for row in batches):.1f} ms "
            f"across {len(batches)} real batches; "
            f"{sum(bool(row['error']) for row in batches)} retained batch failures."
        )
    lines.extend(
        [
            "",
            "Goal budget snapshot: "
            + json.dumps(value["budget_after"], sort_keys=True),
            "Lower inherited budget snapshot: "
            + json.dumps(value["effective_budget_after"], sort_keys=True),
            "Declared peak cost estimates and retained reservations "
            "are not a provider invoice.",
            "",
            "## All Failed Questions",
            "",
        ]
    )
    for arm, row in rows.items():
        failures = [item for item in row["scores"] if not item["passed"]]
        lines.append(f"### {arm}: {len(failures)} failures")
        lines.extend(
            f"- {item['id']}: "
            + json.dumps(
                {
                    key: item[key]
                    for key in (
                        "missing_answer",
                        "citation_errors",
                        "claim_error",
                        "answer",
                    )
                    if key in item
                },
                sort_keys=True,
            )
            for item in failures
        )
    lines.extend(
        [
            "",
            "Plan identity: " + value["plan_id"],
            "Result identity: " + value["result_hash"],
            "NO VALID ALPHA. No final access or live authority.",
        ]
    )
    return "\n".join(lines) + "\n"
