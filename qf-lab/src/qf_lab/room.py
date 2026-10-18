"""Bounded multi-role research, durable decisions and development-only memory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar
from unicodedata import category

from pydantic import BaseModel
from quantforge.product.service import ResearchExecutionError, ResearchService
from quantforge.product.storage import local_root, writer_lock

from .gateway import (
    FAMILIES,
    GatewayError,
    ResearchDecision,
    allowed_families,
    canonical,
    strategy_identity,
)
from .room_gateway import (
    CriticDecision,
    DirectorDecision,
    RoomGateway,
    StructuredProvider,
    ValidationDecision,
)

T = TypeVar("T", bound=BaseModel)


_PRECONDITION_FAILURES = frozenset(
    {
        "ROOM_PAUSED",
        "ROOM_STOPPED",
        "ROOM_WALL_BUDGET_EXHAUSTED",
        "MODEL_BUDGET_EXHAUSTED",
        "MODEL_PROFILE_BUDGET_EXHAUSTED",
        "MODEL_GLOBAL_CAPACITY_DENIED",
        "MODEL_BINDING_REVISION_REVOKED",
        "MODEL_CONTEXT_TOO_LARGE",
        "MODEL_CONTEXT_OR_CREDENTIAL_DENIED",
        "MODEL_PRIOR_OUTCOME_UNKNOWN",
    }
)


class ResearchRoom:
    def __init__(
        self,
        root: Path,
        core_root: Path,
        routes: dict[str, list[StructuredProvider]],
        *,
        memory_root: Path | None = None,
    ) -> None:
        self.root, self.core_root = local_root(root), local_root(core_root)
        self.path = self.root / "room-history.sqlite"
        self.routes = routes
        self.memory_root = local_root(memory_root or self.root.parent)
        if (
            set(routes) != {"researcher", "critic", "director", "validator"}
            or any(not 1 <= len(pool) <= 4 for pool in routes.values())
            or any(
                len({(p.profile_id, p.model) for p in pool}) != len(pool)
                for pool in routes.values()
            )
        ):
            raise GatewayError("ROOM_ROLE_ROUTING_INVALID")
        if self.path.is_file():
            try:
                with closing(self.connect(read_only=True)) as db:
                    names = {
                        row[0] for row in db.execute("SELECT name FROM sqlite_master")
                    }
                    if not {"events", "events_no_update", "events_no_delete"} <= names:
                        raise GatewayError("ROOM_HISTORY_INVALID")
            except sqlite3.DatabaseError:
                raise GatewayError("ROOM_HISTORY_INVALID") from None
            return
        with closing(self.connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            for op in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS events_no_{op.lower()} BEFORE {op} ON events BEGIN SELECT RAISE(ABORT,'append-only'); END"
                )

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        local_root(self.root)
        if self.path.is_symlink():
            raise GatewayError("ROOM_STORE_REDIRECTED")
        if read_only:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
            db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def history(self) -> list[dict[str, Any]]:
        with closing(self.connect(read_only=True)) as db:
            return [
                {"seq": seq, **json.loads(payload)}
                for seq, payload in db.execute(
                    "SELECT seq,payload FROM events ORDER BY seq"
                )
            ]

    def event(self, kind: str, **values: Any) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO events(payload) VALUES(?)",
                (canonical({"kind": kind, **values}),),
            )

    def declare(
        self,
        *,
        program_id: str,
        goal: str,
        dataset_id: str,
        rounds: int = 3,
        calls: int = 32,
        tokens: int = 250000,
        cost: Decimal = Decimal(1),
        wall_seconds: int = 600,
        families: tuple[str, ...] = FAMILIES,
        review_run_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(goal, str):
            raise GatewayError("ROOM_DECLARATION_INVALID")
        goal = goal.strip()
        if (
            not program_id
            or len(program_id) > 30
            or not program_id.isascii()
            or any(not (c.isalnum() or c in "_-") for c in program_id)
            or not goal.strip()
            or len(goal) > 3000
            or any(category(c) == "Cc" for c in goal)
            or type(rounds) is not int
            or not 1 <= rounds <= 4
            or type(wall_seconds) is not int
            or not 30 <= wall_seconds <= 3600
        ):
            raise GatewayError("ROOM_DECLARATION_INVALID")
        families = allowed_families(families)
        minimum_calls = (
            3
            if review_run_id is not None
            else rounds
            * (len(self.routes["researcher"]) + len(self.routes["critic"]) + 4)
        )
        if type(calls) is not int or calls < minimum_calls:
            raise GatewayError("ROOM_CALL_BUDGET_INSUFFICIENT")
        currencies = {
            getattr(p, "cost_currency", "USD")
            for pool in self.routes.values()
            for p in pool
        }
        if len(currencies) != 1:
            raise GatewayError("ROOM_PRICE_CURRENCIES_MUST_MATCH")
        with writer_lock(self.root):
            if self.history():
                raise GatewayError("ROOM_ALREADY_DECLARED")
            core = ResearchService(self.core_root)
            try:
                dataset = core.describe_dataset(dataset_id)
                availability = core.research_availability(dataset_id)
                review_source = None
                if review_run_id is not None:
                    pack = core.inspect(review_run_id)
                    facts = pack["FACT"]
                    if (
                        facts["specification"]["recipe"]["dataset_snapshot"][
                            "dataset_id"
                        ]
                        != dataset_id
                    ):
                        raise GatewayError("REVIEW_DATASET_BINDING_MISMATCH")
                    review_source = {
                        "run_id": review_run_id,
                        "evidence_id": core.model_context(review_run_id)["evidence_id"],
                        "source_sha256": facts["source_sha256"],
                        "research": facts["research"],
                    }
                elif availability["status"] != "AVAILABLE":
                    raise GatewayError("DATASET_ALREADY_BOUND_OR_UNRESOLVED")
            finally:
                core.close()
            max_long = min(
                50, (dataset["row_count"] - 2 * max(10, dataset["row_count"] // 5)) // 3
            )
            if dataset["row_count"] < ResearchService.MIN_OBSERVATIONS or max_long < 5:
                raise GatewayError("ROOM_DATASET_INSUFFICIENT")
            gateway = RoomGateway(self.root, calls=calls, tokens=tokens, cost=cost)
            stamp = datetime.now(UTC)
            declaration = {
                "program_id": program_id,
                "declared_at": stamp.isoformat(),
                "goal": goal,
                "dataset_id": dataset_id,
                "dataset": dataset,
                "round_limit": rounds,
                "trial_limit": rounds * 2,
                "families": list(families),
                "cost_currency": next(iter(currencies)),
                "max_long_window": max_long,
                "budget": gateway.policy,
                "deadline": (stamp + timedelta(seconds=wall_seconds)).isoformat(),
                "mode": "SYNTHETIC"
                if "synthetic" in dataset["source"].lower()
                else "HISTORICAL",
                "provider_mode": "DETERMINISTIC_FAKE"
                if all(
                    p.name == "deterministic-fake"
                    for pool in self.routes.values()
                    for p in pool
                )
                else "CONFIGURED_PROVIDER",
                "routes": {
                    role: [
                        {
                            "profile_id": p.profile_id,
                            "model": p.model,
                            "endpoint_identity": p.endpoint_identity,
                            "binding_revision": getattr(
                                p, "binding_revision", "ENV_REFERENCE"
                            ),
                            "cost_currency": getattr(p, "cost_currency", "USD"),
                            "generation_mode": getattr(p, "generation_mode", "DEFAULT"),
                            "input_price": str(p.input_price),
                            "output_price": str(p.output_price),
                        }
                        for p in pool
                    ]
                    for role, pool in self.routes.items()
                },
                "workflow": "EVIDENCE_REVIEW" if review_source else "RESEARCH",
                "review_source": review_source,
                "role_independence": "NOT_CLAIMED",
                "diversity": "DEGRADED_SAME_ENDPOINT_AND_MODEL"
                if {(p.endpoint_identity, p.model) for p in self.routes["researcher"]}
                & {(p.endpoint_identity, p.model) for p in self.routes["critic"]}
                else "DISTINCT_ENDPOINT_OR_MODEL_NOT_PROVEN_INDEPENDENT",
                "role_identity_overlap": len(
                    {
                        (p.endpoint_identity, p.model)
                        for pool in self.routes.values()
                        for p in pool
                    }
                )
                < sum(len(pool) for pool in self.routes.values()),
            }
            from .library import retrieve

            declaration["research_memory"] = retrieve(
                self.memory_root, exclude=self.root, families=families
            )
            self.event("DECLARATION", declaration=declaration)
        return self.snapshot()

    def _gateway(self) -> RoomGateway:
        events = self.history()
        if not events or events[0]["kind"] != "DECLARATION":
            raise GatewayError("ROOM_NOT_DECLARED")
        budget = events[0]["declaration"]["budget"]
        gateway = RoomGateway(
            self.root,
            calls=budget["calls"],
            tokens=budget["tokens"],
            cost=Decimal(budget["cost"]),
            max_context_bytes=budget["max_context_bytes"],
            require_existing=True,
        )
        responses = {
            row["call_id"]: row["decision"]
            for row in gateway.ledger()
            if row["phase"] == "RESPONSE"
        }
        if any(
            row["kind"] == "MODEL_DECISION"
            and responses.get(row["call_id"]) != row["decision"]
            for row in events
        ):
            raise GatewayError("ROOM_CALL_STORE_HISTORY_MISMATCH")
        return gateway

    def control(self, action: str) -> dict[str, Any]:
        if action not in {"pause", "resume", "stop"}:
            raise GatewayError("ROOM_CONTROL_INVALID")
        status = self.snapshot()["status"]
        if status == "UNDECLARED":
            raise GatewayError("ROOM_NOT_DECLARED")
        if status in {"COMPLETE", "FAILED", "STOPPED"}:
            raise GatewayError("ROOM_TERMINAL")
        if action == "resume" and datetime.now(UTC) >= datetime.fromisoformat(
            self.snapshot()["declaration"]["deadline"]
        ):
            # Absolute lifetime wall budget never resets during a pause. Deny
            # before CONTROL/worker start; retain the paused history unchanged.
            raise GatewayError("ROOM_WALL_BUDGET_EXHAUSTED")
        self.event("CONTROL", action=action)
        return self.snapshot()

    def _check(self, declaration: dict[str, Any]) -> None:
        controls = [v for v in self.history() if v["kind"] == "CONTROL"]
        if controls and controls[-1]["action"] in {"pause", "stop"}:
            raise GatewayError(
                "ROOM_PAUSED" if controls[-1]["action"] == "pause" else "ROOM_STOPPED"
            )
        if datetime.now(UTC) >= datetime.fromisoformat(declaration["deadline"]):
            raise GatewayError("ROOM_WALL_BUDGET_EXHAUSTED")

    def _call(
        self,
        gateway: RoomGateway,
        declaration: dict[str, Any],
        step: str,
        role: str,
        provider: StructuredProvider,
        context: dict[str, Any],
        schema: type[T],
    ) -> tuple[str, T]:
        self._check(declaration)
        from .context_view import bounded_context

        context = bounded_context(context)
        context = {
            **context,
            "step_id": step,
            "research_family_id": declaration["program_id"] + "-family",
            "budget": gateway.budget(),
        }
        rows = gateway.ledger()
        not_sent = {
            v["call_id"]
            for v in rows
            if v["phase"] == "ERROR" and v.get("send_status") == "NOT_SENT_VERIFIED"
        }
        requests = [
            v
            for v in rows
            if v["phase"] == "REQUEST"
            and v["context"].get("step_id") == step
            and v["call_id"] not in not_sent
        ]
        if requests:
            request = requests[0]
            response = next(
                (
                    v
                    for v in rows
                    if v["phase"] == "RESPONSE" and v["call_id"] == request["call_id"]
                ),
                None,
            )
            if response:
                # Resume from an immutable completed call, without charging another call.
                return request["call_id"], schema.model_validate(response["decision"])
            failure = next(
                (
                    v
                    for v in rows
                    if v["phase"] == "ERROR" and v["call_id"] == request["call_id"]
                ),
                None,
            )
            if failure:
                # A durable ERROR is known. Re-enter the frozen fallback path
                # without resending this provider; only unresolved requests park.
                raise GatewayError(failure["classification"])
            raise GatewayError("MODEL_PRIOR_OUTCOME_UNKNOWN")
        if any(
            v["kind"] == "ROLE_PROVIDER_FAILED"
            and v["role"] == role
            and v["profile_id"] == provider.profile_id
            for v in self.history()
        ):
            raise GatewayError("MODEL_PROVIDER_ISOLATED")
        # Retry only unsent capacity waits; park after three denials. Sent or
        # ambiguous calls never enter this loop again.
        capacity_denials = 0
        while True:
            self._check(declaration)
            try:
                call_id, result = gateway.call(
                    role,
                    provider,
                    context,
                    schema,
                    parent_id=next(
                        (
                            v.get("call_id")
                            for v in reversed(rows)
                            if v["phase"] == "RESPONSE"
                        ),
                        None,
                    ),
                )
                break
            except GatewayError as exc:
                if str(exc) != "MODEL_PROFILE_BUSY":
                    raise
                capacity_denials += 1
                if capacity_denials >= 3:
                    self.event("CONTROL", action="pause", reason="MODEL_CAPACITY_WAIT")
                    raise GatewayError("ROOM_PAUSED") from None
                time.sleep(0.1)
        self.event(
            "MODEL_DECISION",
            step_id=step,
            role=role,
            call_id=call_id,
            profile_id=provider.profile_id,
            decision=result.model_dump(mode="json"),
        )
        return call_id, result

    def _call_route(
        self,
        gateway: RoomGateway,
        declaration: dict[str, Any],
        step: str,
        role: str,
        providers: list[StructuredProvider],
        context: dict[str, Any],
        schema: type[T],
    ) -> tuple[str, T]:
        """Frozen ordered fallback, each provider sent at most once per step.

        A fallback has its own durable reservation; it never resends the failed
        provider request. Failed providers remain isolated for this role for the
        entire program. Completed responses resume without another paid call.
        """
        for index, provider in enumerate(providers):
            try:
                return self._call(
                    gateway,
                    declaration,
                    step if index == 0 else f"{step}-route-{index}",
                    role,
                    provider,
                    context,
                    schema,
                )
            except GatewayError as exc:
                if str(exc) in _PRECONDITION_FAILURES:
                    raise
                if not any(
                    v["kind"] == "ROLE_PROVIDER_FAILED"
                    and v["role"] == role
                    and v["profile_id"] == provider.profile_id
                    for v in self.history()
                ):
                    self.event(
                        "ROLE_PROVIDER_FAILED",
                        role=role,
                        profile_id=provider.profile_id,
                        step_id=step,
                        retry="FORBIDDEN",
                        classification=str(exc),
                    )
        raise GatewayError("ALL_ROLE_PROVIDERS_FAILED")

    def _experiment(
        self,
        core: ResearchService,
        declaration: dict[str, Any],
        run_id: str,
        call_id: str,
        proposal: ResearchDecision,
    ) -> dict[str, Any]:
        self._check(declaration)
        if not any(v["run_id"] == run_id for v in core.list_runs()):
            self.event(
                "EXPERIMENT_PREPARE",
                run_id=run_id,
                call_id=call_id,
                strategy=proposal.strategy,
            )
            core.propose_and_run(
                dataset_id=declaration["dataset_id"],
                program_id=declaration["program_id"],
                family_id=declaration["program_id"] + "-family",
                run_id=run_id,
                goal=declaration["goal"],
                family=proposal.family,
                parameters=proposal.parameters,
                submitted_at=datetime.now(UTC),
                model_call_id=call_id,
                rationale=proposal.rationale,
            )
        reproduction = core.reproduce(run_id)
        if reproduction["result"] != "EXACT_MATCH":
            raise GatewayError("CORE_REPRODUCTION_MISMATCH")
        context = core.model_context(run_id)
        if not any(
            v["kind"] == "EVIDENCE_REFERENCE" and v["run_id"] == run_id
            for v in self.history()
        ):
            self.event(
                "EVIDENCE_REFERENCE",
                run_id=run_id,
                evidence_id=context["evidence_id"],
                reproduce=reproduction,
            )
        return context

    def _review_existing(
        self,
        declaration: dict[str, Any],
        gateway: RoomGateway,
        core: ResearchService,
    ) -> dict[str, Any]:
        """A bounded new interpretation of retained facts, with zero new trials.

        It preserves the original failed campaign and cannot resend an old call,
        change Core facts, reopen a final partition or requalify old source code.
        """
        source = declaration["review_source"]
        evidence = core.model_context(source["run_id"])
        if evidence["evidence_id"] != source["evidence_id"]:
            raise GatewayError("REVIEW_EVIDENCE_CHANGED")
        base = {
            "goal": declaration["goal"],
            "phase": "critic",
            "round": 1,
            "round_limit": 1,
            "evidence": [evidence],
            "reference_ids": [],
            "review_only": True,
            "allowed_director_outcomes": ["reject", "stop"],
            "limitation": "No new experiment, alpha or current-code reproduction claim; retained source version only.",
        }
        _, critic = self._call_route(
            gateway,
            declaration,
            "review-critic",
            "critic",
            self.routes["critic"],
            base,
            CriticDecision,
        )
        # Conservative retained exposure bound; diagnostics cannot certify an
        # adaptive family as fixed-grid or establish alpha.
        diagnostics = core.research_diagnostics(
            [source["run_id"]], frozen_trial_budget=8
        )
        _, validation = self._call_route(
            gateway,
            declaration,
            "review-validator",
            "validator",
            self.routes["validator"],
            {**base, "diagnostics": diagnostics},
            ValidationDecision,
        )
        _, director = self._call_route(
            gateway,
            declaration,
            "review-director",
            "director",
            self.routes["director"],
            {
                **base,
                "phase": "direct",
                "critics": [critic.model_dump(mode="json")],
                "validation": validation.model_dump(mode="json"),
                "diagnostics": diagnostics,
            },
            DirectorDecision,
        )
        if director.next not in {"reject", "stop"}:
            raise GatewayError("REVIEW_ONLY_NO_NEW_TRIAL_OR_FREEZE")
        if not any(
            v["kind"] == "RETAINED_EVIDENCE_REVIEW"
            and v["run_id"] == source["run_id"]
            and v["evidence_id"] == source["evidence_id"]
            for v in self.history()
        ):
            self.event(
                "RETAINED_EVIDENCE_REVIEW",
                run_id=source["run_id"],
                evidence_id=source["evidence_id"],
                critic=critic.model_dump(mode="json"),
                validation=validation.model_dump(mode="json"),
                director=director.model_dump(mode="json"),
                source_sha256=source["source_sha256"],
            )
        self.event(
            "COMPLETE",
            conclusion="NO VALID ALPHA",
            reason="REVIEW_ONLY_" + director.next.upper(),
        )
        return self.snapshot()

    def run(self) -> dict[str, Any]:
        with writer_lock(self.root):
            state = self.snapshot()
            if state["status"] == "UNDECLARED":
                raise GatewayError("ROOM_NOT_DECLARED")
            if state["status"] in {"COMPLETE", "FAILED", "STOPPED"}:
                return state
            declaration = state["declaration"]
            route_identity = {
                role: [
                    {
                        "profile_id": p.profile_id,
                        "model": p.model,
                        "endpoint_identity": p.endpoint_identity,
                        "binding_revision": getattr(
                            p, "binding_revision", "ENV_REFERENCE"
                        ),
                        "cost_currency": getattr(p, "cost_currency", "USD"),
                        "generation_mode": getattr(p, "generation_mode", "DEFAULT"),
                        "input_price": str(p.input_price),
                        "output_price": str(p.output_price),
                    }
                    for p in pool
                ]
                for role, pool in self.routes.items()
            }
            if route_identity != declaration["routes"]:
                self.event("FAILED", classification="ROOM_ROUTES_IMMUTABLE")
                raise GatewayError("ROOM_ROUTES_IMMUTABLE")
            gateway, core = self._gateway(), ResearchService(self.core_root)
            try:
                if declaration.get("workflow") == "EVIDENCE_REVIEW":
                    return self._review_existing(declaration, gateway, core)
                for round_number in range(1, declaration["round_limit"] + 1):
                    if any(
                        v["kind"] == "ROUND_COMMITTED" and v["round"] == round_number
                        for v in self.history()
                    ):
                        continue
                    base = {
                        "phase": "first",
                        "round": round_number,
                        "round_limit": declaration["round_limit"],
                        "goal": declaration["goal"],
                        "max_long_window": declaration["max_long_window"],
                        "allowed_families": declaration["families"],
                        "allowed_actions": ["propose"],
                        "evidence": [],
                        "research_memory": declaration.get("research_memory", {}),
                    }
                    previous = [
                        v for v in self.history() if v["kind"] == "ROUND_COMMITTED"
                    ]
                    if previous:
                        next_hypothesis = previous[-1]["director"].get(
                            "next_hypothesis"
                        )
                        if next_hypothesis:
                            base["director_next_hypothesis"] = next_hypothesis
                        base["intelligence_memory"] = {
                            "previous_director": previous[-1]["director"],
                            "tested_families": sorted(
                                {
                                    v["strategy"]["family"]
                                    for v in self.history()
                                    if v["kind"] == "EXPERIMENT_PREPARE"
                                    and "strategy" in v
                                    and "family" in v["strategy"]
                                }
                            ),
                            "negative_result_count": len(
                                self.memory()["INTELLIGENCE_MEMORY"]["failures"]
                            ),
                        }
                    with ThreadPoolExecutor(
                        max_workers=len(self.routes["researcher"])
                    ) as pool:
                        proposal_futures = [
                            pool.submit(
                                self._call,
                                gateway,
                                declaration,
                                f"r{round_number}-proposal-{i}",
                                "researcher",
                                p,
                                base,
                                ResearchDecision,
                            )
                            for i, p in enumerate(self.routes["researcher"])
                        ]
                        proposals = []
                        proposal_sources = {}
                        for index, future in enumerate(proposal_futures):
                            provider = self.routes["researcher"][index]
                            try:
                                result = future.result()
                                proposals.append(result)
                                proposal_sources[result[0]] = provider
                            except GatewayError as exc:
                                if str(exc) in _PRECONDITION_FAILURES:
                                    raise
                                self._record_provider_failure(
                                    "researcher", provider, round_number, str(exc)
                                )
                        if not proposals:
                            raise GatewayError("ALL_RESEARCH_PROVIDERS_FAILED")
                    proposed = [
                        {"decision_id": cid, "proposal": p.model_dump(mode="json")}
                        for cid, p in proposals
                    ]
                    _, selection = self._call_route(
                        gateway,
                        declaration,
                        f"r{round_number}-select",
                        "director",
                        self.routes["director"],
                        {
                            **base,
                            "phase": "select",
                            "proposals": proposed,
                            "reference_ids": [v[0] for v in proposals],
                        },
                        DirectorDecision,
                    )
                    if selection.next != "test":
                        if selection.next not in {"stop", "reject"}:
                            raise GatewayError("DIRECTOR_SELECTION_INVALID")
                        self.event(
                            "COMPLETE",
                            conclusion="NO VALID ALPHA",
                            reason="DIRECTOR_REJECTED_ALL_PROPOSALS",
                        )
                        return self.snapshot()
                    selected = next(
                        (v for v in proposals if v[0] == selection.selected_id), None
                    )
                    if selected is None:
                        raise GatewayError("DIRECTOR_UNTESTED_SELECTION")
                    first = self._experiment(
                        core,
                        declaration,
                        f"{declaration['program_id']}-r{round_number}-a",
                        *selected,
                    )
                    change_context = {
                        **base,
                        "phase": "next",
                        "evidence": [first],
                        "allowed_actions": ["modify", "branch", "continue"],
                        "previous_strategy": selected[1].strategy,
                    }
                    primary = proposal_sources[selected[0]]
                    next_id, revised = self._call_route(
                        gateway,
                        declaration,
                        f"r{round_number}-revision",
                        "researcher",
                        [primary]
                        + [p for p in self.routes["researcher"] if p is not primary],
                        change_context,
                        ResearchDecision,
                    )
                    if strategy_identity(selected[1].strategy) == strategy_identity(
                        revised.strategy
                    ):
                        raise GatewayError("SECOND_ROUND_MUST_CHANGE_PARAMETERS")
                    second = self._experiment(
                        core,
                        declaration,
                        f"{declaration['program_id']}-r{round_number}-b",
                        next_id,
                        revised,
                    )
                    evidence = [first, second]
                    evaluation = {**base, "phase": "critic", "evidence": evidence}
                    with ThreadPoolExecutor(
                        max_workers=len(self.routes["critic"])
                    ) as pool:
                        critic_futures = [
                            pool.submit(
                                self._call,
                                gateway,
                                declaration,
                                f"r{round_number}-critic-{i}",
                                "critic",
                                p,
                                evaluation,
                                CriticDecision,
                            )
                            for i, p in enumerate(self.routes["critic"])
                        ]
                        critics: list[tuple[str, CriticDecision]] = []
                        for index, critic_future in enumerate(critic_futures):
                            provider = self.routes["critic"][index]
                            try:
                                critics.append(critic_future.result())
                            except GatewayError as exc:
                                if str(exc) in _PRECONDITION_FAILURES:
                                    raise
                                self._record_provider_failure(
                                    "critic", provider, round_number, str(exc)
                                )
                        if not critics:
                            raise GatewayError("ALL_CRITIC_PROVIDERS_FAILED")
                    runs = [
                        v["run_id"]
                        for v in self.history()
                        if v["kind"] == "EVIDENCE_REFERENCE"
                    ]
                    diagnostics = core.research_diagnostics(
                        runs, frozen_trial_budget=declaration["trial_limit"]
                    )
                    _, validation = self._call_route(
                        gateway,
                        declaration,
                        f"r{round_number}-validator",
                        "validator",
                        self.routes["validator"],
                        {**evaluation, "diagnostics": diagnostics},
                        ValidationDecision,
                    )
                    _, director = self._call_route(
                        gateway,
                        declaration,
                        f"r{round_number}-director",
                        "director",
                        self.routes["director"],
                        {
                            **evaluation,
                            "phase": "direct",
                            "critics": [v[1].model_dump(mode="json") for v in critics],
                            "validation": validation.model_dump(mode="json"),
                            "diagnostics": diagnostics,
                        },
                        DirectorDecision,
                    )
                    if director.next == "test":
                        raise GatewayError("DIRECTOR_PHASE_INVALID")
                    if director.next == "freeze":
                        target = next(
                            (
                                v
                                for v in evidence
                                if v["evidence_id"] == director.selected_id
                            ),
                            None,
                        )
                        if target is None:
                            raise GatewayError("DIRECTOR_UNTESTED_SELECTION")
                        frozen = core.freeze(
                            target["FACT"]["run_id"],
                            program_id=declaration["program_id"],
                        )
                        self.event(
                            "FREEZE_REFERENCE",
                            evidence_id=frozen["evidence_id"],
                            run_id=target["FACT"]["run_id"],
                            status=frozen["status"],
                        )
                    self.event(
                        "ROUND_COMMITTED",
                        round=round_number,
                        director=director.model_dump(mode="json"),
                        critic_disagreement=len({v[1].verdict for v in critics}) > 1,
                        diagnostics=diagnostics,
                    )
                    if (
                        director.next != "continue"
                        or round_number == declaration["round_limit"]
                    ):
                        self.event(
                            "COMPLETE",
                            conclusion="NO VALID ALPHA",
                            reason="DIRECTOR_" + director.next.upper()
                            if director.next != "continue"
                            else "ROUND_LIMIT",
                        )
                        return self.snapshot()
            except GatewayError as exc:
                code = str(exc)
                if code not in {"ROOM_PAUSED", "ROOM_STOPPED"}:
                    self.event(
                        "FAILED",
                        classification=code
                        if code.isascii()
                        and len(code) <= 80
                        and all(c.isupper() or c == "_" for c in code)
                        else "RESEARCH_FAILED",
                    )
            except ResearchExecutionError as exc:
                self.event("FAILED", classification=str(exc))
            except Exception:  # noqa: BLE001 -- closed code only; no secret errors
                self.event("FAILED", classification="RESEARCH_FAILED")
            finally:
                core.close()
        return self.snapshot()

    def _record_provider_failure(
        self, role: str, provider: StructuredProvider, round_number: int, code: str
    ) -> None:
        if not any(
            v["kind"] == "ROLE_PROVIDER_FAILED"
            and v["role"] == role
            and v["profile_id"] == provider.profile_id
            for v in self.history()
        ):
            self.event(
                "ROLE_PROVIDER_FAILED",
                role=role,
                profile_id=provider.profile_id,
                round=round_number,
                retry="FORBIDDEN",
                classification=code,
            )

    def memory(self) -> dict[str, Any]:
        history = self.history()
        gateway = self._gateway()
        ledger = gateway.ledger()
        values = {
            "FACT_MEMORY": [
                {
                    "run_id": v["run_id"],
                    "evidence_id": v["evidence_id"],
                    "reproduce": v["reproduce"],
                }
                for v in history
                if v["kind"] == "EVIDENCE_REFERENCE"
            ],
            "INTELLIGENCE_MEMORY": {
                "hypotheses": [
                    v
                    for v in ledger
                    if v["phase"] == "RESPONSE"
                    and v["decision"].get("action")
                    in {"propose", "modify", "branch", "continue"}
                ],
                "failures": [
                    v
                    for v in history
                    if v["kind"] in {"FAILED", "ROLE_PROVIDER_FAILED"}
                ]
                + [
                    v
                    for v in ledger
                    if v["phase"] == "RESPONSE"
                    and (
                        v["decision"].get("verdict") == "reject"
                        or v["decision"].get("next") == "reject"
                    )
                ],
                "decisions": [v for v in ledger if v["phase"] == "RESPONSE"],
                "strategies": [
                    v
                    for v in history
                    if v["kind"] in {"EXPERIMENT_PREPARE", "FREEZE_REFERENCE"}
                ],
                "regimes": [],
            },
            "derivation": "append-only Lab history and call ledger; Core evidence IDs only",
            "RETAINED_FACT_MEMORY": [
                {
                    "run_id": row["run_id"],
                    "evidence_id": row["evidence_id"],
                    "source_sha256": row["source_sha256"],
                    "current_code_reproduction": "NOT_CLAIMED",
                }
                for row in history
                if row["kind"] == "RETAINED_EVIDENCE_REVIEW"
            ],
        }
        return {
            **values,
            "memory_id": "memory-"
            + hashlib.sha256(canonical(values).encode()).hexdigest(),
        }

    def snapshot(self) -> dict[str, Any]:
        history = self.history()
        if not history:
            return {"status": "UNDECLARED", "history": []}
        if history[0].get("kind") != "DECLARATION" or not isinstance(
            history[0].get("declaration"), dict
        ):
            raise GatewayError("ROOM_HISTORY_INVALID")
        declaration = history[0]["declaration"]
        controls = [v for v in history if v["kind"] == "CONTROL"]
        status = (
            "COMPLETE"
            if any(v["kind"] == "COMPLETE" for v in history)
            else "FAILED"
            if any(v["kind"] == "FAILED" for v in history)
            else "STOPPED"
            if controls and controls[-1]["action"] == "stop"
            else "PAUSED"
            if controls and controls[-1]["action"] == "pause"
            else "READY_OR_RUNNING"
        )
        return {
            "schema_version": "qf-research-room-v1",
            "status": status,
            "mode": declaration["mode"],
            "declaration": declaration,
            "rounds_completed": sum(v["kind"] == "ROUND_COMMITTED" for v in history),
            "reviews_completed": sum(
                v["kind"] == "RETAINED_EVIDENCE_REVIEW" for v in history
            ),
            "conclusion": "NO VALID ALPHA",
            "live_authority": "NONE",
            "budget": self._gateway().budget(),
            "history": history,
            "calls": self._gateway().ledger(),
        }
