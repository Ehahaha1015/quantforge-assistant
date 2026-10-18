"""Two evidence-driven autonomous rounds and immutable intelligence history."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantforge.product.service import ResearchService

from .gateway import (
    SMA,
    GatewayError,
    ModelGateway,
    Provider,
    canonical,
    strategy_identity,
)
from .gateway import (
    allowed_families as validate_families,
)


class RoundController:
    def __init__(
        self,
        core_root: Path,
        lab_root: Path,
        provider: Provider,
        *,
        critic_provider: Provider | None = None,
    ) -> None:
        self.core_root, self.lab_root, self.provider = core_root, lab_root, provider
        self.critic_provider = (
            critic_provider if critic_provider is not None else provider
        )
        lab_root.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        goal: str,
        *,
        dataset_id: str,
        program_id: str,
        allowed_families: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        families = validate_families(allowed_families)
        if not goal.strip() or len(goal) > 5000:
            raise GatewayError("invalid bounded research goal")
        if (
            not program_id.isascii()
            or not program_id
            or len(program_id) > 40
            or any(not (c.isalnum() or c in "_-") for c in program_id)
        ):
            raise GatewayError("invalid program identifier")
        directory = self.lab_root / program_id
        directory.mkdir()  # exclusive program; interrupted programs are inspected, never silently restarted
        history = sqlite3.connect(directory / "history.sqlite")
        history.execute("PRAGMA journal_mode=WAL")
        history.execute("PRAGMA synchronous=FULL")
        history.execute(
            "CREATE TABLE events (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        for operation in ("UPDATE", "DELETE"):
            history.execute(
                f"CREATE TRIGGER no_{operation.lower()} BEFORE {operation} ON events BEGIN SELECT RAISE(ABORT,'append-only'); END"
            )
        history.commit()
        gateway = ModelGateway(directory / "calls.sqlite", self.provider, max_calls=3)
        core = ResearchService(self.core_root)

        def event(kind: str, **values: Any) -> None:
            with history:
                history.execute(
                    "INSERT INTO events(payload) VALUES (?)",
                    (canonical({"kind": kind, **values}),),
                )

        try:
            dataset = core.describe_dataset(dataset_id)
            n = dataset["row_count"]
            max_long = min(50, (n - 2 * max(10, n // 5)) // 3)
            event(
                "GOAL",
                goal=goal,
                program_id=program_id,
                dataset_id=dataset_id,
                provider=self.provider.name,
                model=self.provider.model,
                critic_provider=self.critic_provider.name,
                critic_model=self.critic_provider.model,
                allowed_families=list(families),
            )
            evidence: list[dict[str, Any]] = []
            run_ids: list[str] = []
            previous_identity: str | None = None
            previous_family: str | None = None
            for index in range(2):
                context = {
                    "phase": "first" if index == 0 else "next",
                    "goal": goal,
                    "dataset": dataset,
                    "max_long_window": max_long,
                    "allowed_families": list(families),
                    "require_family_change": index == 1 and len(families) > 1,
                    "evidence": evidence,
                    "round_budget": 2,
                    "allowed_actions": ["propose"]
                    if index == 0
                    else ["modify", "branch", "continue"],
                }
                call_id, decision = gateway.call(context)
                event(
                    "DECISION",
                    call_id=call_id,
                    decision=decision.model_dump(mode="json"),
                )
                if decision.action not in context["allowed_actions"]:
                    raise GatewayError("MODEL_ACTION_OUTSIDE_PHASE")
                identity = strategy_identity(decision.strategy)
                if identity == previous_identity:
                    raise GatewayError("SECOND_ROUND_MUST_CHANGE_PARAMETERS")
                if (
                    context["require_family_change"]
                    and decision.family == previous_family
                ):
                    raise GatewayError("SECOND_ROUND_MUST_CHANGE_FAMILY")
                previous_identity, previous_family = identity, decision.family
                run_id = f"{program_id}-r{index + 1}"
                event(
                    "PROPOSAL",
                    run_id=run_id,
                    family=decision.family,
                    parameters=decision.parameters,
                    **(decision.parameters if decision.family == SMA else {}),
                    call_id=call_id,
                )
                recipe = (
                    decision.parameters
                    if allowed_families is None and decision.family == SMA
                    else {"family": decision.family, "parameters": decision.parameters}
                )
                core.propose_and_run(
                    dataset_id=dataset_id,
                    program_id=program_id,
                    family_id=program_id + "-family",
                    run_id=run_id,
                    goal=goal,
                    **recipe,
                    submitted_at=datetime.now(UTC),
                    model_call_id=call_id,
                    rationale=decision.rationale,
                )
                reproduced = core.reproduce(run_id)
                if reproduced["result"] != "EXACT_MATCH":
                    raise GatewayError("CORE_REPRODUCTION_MISMATCH")
                evidence.append(core.model_context(run_id))
                run_ids.append(run_id)
                event(
                    "EVIDENCE_REFERENCE",
                    run_id=run_id,
                    evidence_id=evidence[-1]["evidence_id"],
                    reproduce=reproduced,
                )
            critic_context = {
                "phase": "critic",
                "goal": goal,
                "dataset": dataset,
                "max_long_window": max_long,
                "allowed_families": list(families),
                "evidence": evidence,
                "allowed_actions": ["freeze", "reject"],
                "round_budget": 0,
            }
            call_id, decision = gateway.call(
                critic_context, provider=self.critic_provider
            )
            event(
                "CRITIC_DECISION",
                call_id=call_id,
                decision=decision.model_dump(mode="json"),
            )
            if decision.action not in ("freeze", "reject") or set(
                decision.evidence_refs
            ) != {v["evidence_id"] for v in evidence}:
                raise GatewayError("CRITIC_MUST_COMPARE_BOTH_ROUNDS")
            matches = [
                item["FACT"]["run_id"]
                for item in evidence
                if strategy_identity(item["strategy"])
                == strategy_identity(decision.strategy)
            ]
            if len(matches) != 1:
                raise GatewayError("CRITIC_CANNOT_INVENT_UNRUN_CANDIDATE")
            frozen = None
            if decision.action == "freeze":
                frozen = core.freeze(matches[0], program_id=program_id)
                event(
                    "CORE_FREEZE_REFERENCE",
                    run_id=matches[0],
                    evidence_id=frozen["evidence_id"],
                    status=frozen["status"],
                )
            summary = {
                "program_id": program_id,
                "rounds_completed": len(run_ids),
                "runs": run_ids,
                "latest_decision": decision.model_dump(mode="json"),
                "frozen": frozen,
                "provider": self.provider.name,
                "requested_model": self.provider.model,
                "critic_provider": self.critic_provider.name,
                "critic_model": self.critic_provider.model,
                "critic_independent": (self.provider.name, self.provider.model)
                != (self.critic_provider.name, self.critic_provider.model),
                "allowed_families": list(families),
                "real_calls_proven": all(
                    row.get("real_model_call") is True
                    for row in gateway.ledger()
                    if row["phase"] == "RESPONSE"
                ),
                "history": str(directory / "history.sqlite"),
                "call_ledger": str(directory / "calls.sqlite"),
            }
            event("COMPLETE", summary=summary)
            return summary
        except Exception as exc:
            # Failure history is preserved; never persist exception text/secret material.
            event("FAILED", error_class=type(exc).__name__)
            raise
        finally:
            core.close()
            gateway.close()
            history.close()


def inspect_history(lab_root: Path, program_id: str) -> list[dict[str, Any]]:
    if (
        not program_id
        or len(program_id) > 40
        or not program_id.isascii()
        or any(not (c.isalnum() or c in "_-") for c in program_id)
    ):
        raise ValueError("invalid program ID")
    directory = lab_root / program_id
    database = directory / "history.sqlite"
    if (
        directory.is_symlink()
        or directory.is_junction()
        or database.is_symlink()
        or not database.resolve().is_relative_to(lab_root.resolve())
    ):
        raise GatewayError(
            "PROGRAM_HISTORY_REDIRECTED: use host-owned history inside the Lab root"
        )
    database = database.resolve()
    if not database.is_file():
        raise GatewayError(
            "PROGRAM_HISTORY_MISSING: inspect the program identifier and Lab root"
        )
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        return [
            {"seq": seq, **json.loads(payload)}
            for seq, payload in connection.execute(
                "SELECT seq,payload FROM events ORDER BY seq"
            )
        ]


def list_programs(lab_root: Path) -> list[dict[str, Any]]:
    """Read persisted program progress without creating or altering history."""
    if not lab_root.exists():
        return []
    result: list[dict[str, Any]] = []
    for directory in sorted(lab_root.iterdir()):
        program = directory.name
        if (
            not directory.is_dir()
            or directory.is_symlink()
            or directory.is_junction()
            or not program.isascii()
            or len(program) > 40
            or any(not (c.isalnum() or c in "_-") for c in program)
        ):
            continue
        try:
            events = inspect_history(lab_root, program)
            kinds = [event["kind"] for event in events]
            result.append(
                {
                    "program_id": program,
                    "status": "COMPLETE"
                    if "COMPLETE" in kinds
                    else "FAILED"
                    if "FAILED" in kinds
                    else "IN_PROGRESS_OR_INTERRUPTED",
                    "rounds_completed": kinds.count("EVIDENCE_REFERENCE"),
                    "last_event": kinds[-1] if kinds else None,
                    "history": str(directory / "history.sqlite"),
                }
            )
        except (GatewayError, sqlite3.Error, ValueError, KeyError, OSError):
            result.append(
                {
                    "program_id": program,
                    "status": "BLOCKED",
                    "reason": "Program history is missing, invalid or unavailable",
                }
            )
    return result
