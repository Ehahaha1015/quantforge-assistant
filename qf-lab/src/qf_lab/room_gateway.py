"""Cross-process bounded structured calls; ambiguous spend is reserved, never retried."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field
from quantforge.product.storage import local_root

from .gateway import (
    FakeProvider,
    GatewayError,
    ModelCapacityBusy,
    ResearchDecision,
    canonical,
)

T = TypeVar("T", bound=BaseModel)


class CriticDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    verdict: Literal["freeze", "reject", "continue"]
    rationale: str = Field(min_length=10, max_length=3000)
    evidence_refs: tuple[str, ...]
    concerns: tuple[str, ...] = Field(max_length=12)


class DirectorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    next: Literal["test", "continue", "freeze", "reject", "stop"]
    selected_id: str | None
    rationale: str = Field(min_length=10, max_length=3000)
    evidence_refs: tuple[str, ...]
    next_hypothesis: str | None = Field(default=None, max_length=2000)


class ValidationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    verdict: Literal["insufficient", "concerns", "descriptive_only"]
    rationale: str = Field(min_length=10, max_length=3000)
    evidence_refs: tuple[str, ...]


class StructuredProvider(Protocol):
    name: str
    model: str
    profile_id: str
    endpoint_identity: str
    input_price: Decimal
    output_price: Decimal

    def complete_structured(
        self, context: dict[str, Any], schema: type[T], *, max_output: int = 1800
    ) -> tuple[T, dict[str, Any]]: ...


class FakeRoomProvider:
    name = "deterministic-fake"
    model = "fake-room-v2"
    endpoint_identity = "SYNTHETIC"
    input_price = Decimal(0)
    output_price = Decimal(0)

    def __init__(self, profile_id: str = "synthetic-provider") -> None:
        self.profile_id = profile_id

    def complete_structured(
        self, context: dict[str, Any], schema: type[T], *, max_output: int = 1800
    ) -> tuple[T, dict[str, Any]]:
        refs = [v["evidence_id"] for v in context.get("evidence", [])]
        if schema is ResearchDecision:
            decision, _ = FakeProvider().complete(context)
            # Each round is a new bounded question, never reopen a frozen final.
            value = decision.model_dump(mode="json")
        elif schema is CriticDecision:
            value = {
                "verdict": "reject",
                "rationale": "Synthetic fixtures cannot establish deployable alpha; retain negative results.",
                "evidence_refs": refs,
                "concerns": [
                    "No live eligibility; historical cost/settlement limitations remain."
                ],
            }
        elif schema is ValidationDecision:
            value = {
                "verdict": "insufficient",
                "rationale": "Core diagnostics are descriptive; adaptive search does not validate alpha.",
                "evidence_refs": refs,
            }
        else:
            proposal = context.get("phase") == "select"
            continuation = not proposal and context["round"] < context["round_limit"]
            value = {
                "next": "test"
                if proposal
                else "continue"
                if continuation
                else "reject",
                "selected_id": context["proposals"][0]["decision_id"]
                if proposal
                else None,
                "rationale": "A deterministic bounded research choice with no execution authority.",
                "evidence_refs": context["reference_ids"] if proposal else refs,
                "next_hypothesis": "Stress the same declared family under chronological validation"
                if continuation
                else None,
            }
        return schema.model_validate(value), {
            "real_model_call": False,
            "input_tokens": 0,
            "output_tokens": 0,
        }


class RoomGateway:
    def __init__(
        self,
        root: Path,
        *,
        calls: int,
        tokens: int,
        cost: Decimal,
        max_context_bytes: int = 45000,
        require_existing: bool = False,
    ) -> None:
        self.root = local_root(root)
        self.path = self.root / "room-calls.sqlite"
        self.require_existing = require_existing
        if require_existing and not self.path.is_file():
            raise GatewayError("ROOM_CALL_STORE_MISSING")
        if (
            type(calls) is not int
            or not 1 <= calls <= 100
            or type(tokens) is not int
            or not 4096 <= tokens <= 1_000_000
            or not cost.is_finite()
            or not 0 <= cost <= 1000
            or not 1000 <= max_context_bytes <= 45000
        ):
            raise GatewayError("ROOM_BUDGET_INVALID")
        self.policy: dict[str, Any] = {
            "calls": calls,
            "tokens": tokens,
            "cost": str(cost),
            "max_context_bytes": max_context_bytes,
        }
        if require_existing:
            try:
                with closing(self.connect()) as db:
                    existing = db.execute(
                        "SELECT payload FROM policy WHERE id=1"
                    ).fetchone()
                    if existing is None or existing[0] != canonical(self.policy):
                        raise GatewayError("ROOM_BUDGET_IMMUTABLE")
                    self._rows(db)
            except (sqlite3.DatabaseError, ValueError):
                raise GatewayError("ROOM_CALL_STORE_INVALID") from None
            return
        with closing(self.connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
            )
            existing = db.execute("SELECT payload FROM policy WHERE id=1").fetchone()
            if existing and existing[0] != canonical(self.policy):
                raise GatewayError("ROOM_BUDGET_IMMUTABLE")
            db.execute(
                "INSERT OR IGNORE INTO policy VALUES(1,?)", (canonical(self.policy),)
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS calls (seq INTEGER PRIMARY KEY, phase TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            for table in ("policy", "calls"):
                for op in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'append-only'); END"
                    )

    def connect(self) -> sqlite3.Connection:
        local_root(self.root)
        if self.path.is_symlink():
            raise GatewayError("ROOM_STORE_REDIRECTED")
        if self.require_existing:
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        else:
            db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
        return [
            {"seq": seq, "phase": phase, **json.loads(payload)}
            for seq, phase, payload in db.execute(
                "SELECT seq,phase,payload FROM calls ORDER BY seq"
            )
        ]

    def ledger(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            return self._rows(db)

    def budget(self) -> dict[str, Any]:
        with closing(self.connect()) as db:
            return self._budget(db)

    def _budget(self, db: sqlite3.Connection) -> dict[str, Any]:
        rows = self._rows(db)
        not_sent = {
            v["call_id"]
            for v in rows
            if v["phase"] == "ERROR" and v.get("send_status") == "NOT_SENT_VERIFIED"
        }
        requests = [
            v for v in rows if v["phase"] == "REQUEST" and v["call_id"] not in not_sent
        ]
        results = {v["call_id"]: v for v in rows if v["phase"] == "RESPONSE"}
        spent_tokens, spent_cost = 0, Decimal(0)
        for row in requests:
            result = results.get(row["call_id"], row)
            spent_tokens += result.get("charged_tokens", row["reserved_tokens"])
            spent_cost += Decimal(result.get("charged_cost", row["reserved_cost"]))
        return {
            "calls_remaining": self.policy["calls"] - len(requests),
            "tokens_remaining": self.policy["tokens"] - spent_tokens,
            "cost_remaining": str(Decimal(self.policy["cost"]) - spent_cost),
            "spent_calls": len(requests),
            "spent_tokens": spent_tokens,
            "spent_cost": str(spent_cost),
            "pricing": "DECLARED_ASSUMPTION",
            "unknown_call_ids": [
                v["call_id"]
                for v in requests
                if not any(
                    r["call_id"] == v["call_id"] and r["phase"] in {"RESPONSE", "ERROR"}
                    for r in rows
                )
            ],
        }

    @staticmethod
    def _append(db: sqlite3.Connection, phase: str, value: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO calls(phase,payload) VALUES(?,?)", (phase, canonical(value))
        )

    def call(
        self,
        role: str,
        provider: StructuredProvider,
        context: dict[str, Any],
        schema: type[T],
        *,
        parent_id: str | None = None,
        max_output: int = 1800,
        audit_context: dict[str, Any] | None = None,
    ) -> tuple[str, T]:
        if role not in {"researcher", "critic", "director", "validator"}:
            raise GatewayError("ROOM_ROLE_INVALID")
        if type(max_output) is not int or not 64 <= max_output <= 1800:
            raise GatewayError("MODEL_OUTPUT_BUDGET_INVALID")
        check_context = getattr(provider, "check_context", None)
        if check_context is not None:
            try:
                check_context(context)
                if audit_context is not None:
                    check_context(audit_context)
            except GatewayError:
                raise
            except (ValueError, OSError):
                raise GatewayError("MODEL_CONTEXT_OR_CREDENTIAL_DENIED") from None
        body = canonical(context)
        if audit_context is not None and len(canonical(audit_context).encode()) > 45000:
            raise GatewayError("MODEL_AUDIT_CONTEXT_TOO_LARGE")
        if len(body.encode()) > self.policy["max_context_bytes"]:
            raise GatewayError("MODEL_CONTEXT_TOO_LARGE")
        # A byte/token conservative bound including schema/system overhead and output cap.
        input_bound = (
            len(body.encode())
            + len(canonical(schema.model_json_schema()).encode())
            + 2000
        )
        output_bound = max_output
        reserve = (
            Decimal(input_bound) * provider.input_price
            + Decimal(output_bound) * provider.output_price
        ) / 1_000_000
        if not reserve.is_finite() or reserve < 0:
            raise GatewayError("MODEL_PRICE_INVALID")
        capacity_check = getattr(provider, "check_capacity", None)
        if capacity_check is not None:
            capacity_check(input_bound, output_bound)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            budget = self._budget(db)
            rows = self._rows(db)
            finished = {
                v["call_id"] for v in rows if v["phase"] in {"RESPONSE", "ERROR"}
            }
            active = sum(
                v["phase"] == "REQUEST"
                and v["profile_id"] == provider.profile_id
                and v["call_id"] not in finished
                for v in rows
            )
            if active >= getattr(provider, "max_concurrency", 4):
                raise GatewayError("MODEL_PROFILE_BUSY")
            limits = getattr(provider, "limits", None)
            if limits is not None:
                rows = self._rows(db)
                requests = [
                    v
                    for v in rows
                    if v["phase"] == "REQUEST"
                    and v["profile_id"] == provider.profile_id
                    and not any(
                        r["call_id"] == v["call_id"]
                        and r.get("send_status") == "NOT_SENT_VERIFIED"
                        for r in rows
                    )
                ]
                results = {v["call_id"]: v for v in rows if v["phase"] == "RESPONSE"}
                used_tokens = sum(
                    results.get(v["call_id"], {}).get(
                        "charged_tokens", v["reserved_tokens"]
                    )
                    for v in requests
                )
                used_cost = sum(
                    (
                        Decimal(
                            results.get(v["call_id"], {}).get(
                                "charged_cost", v["reserved_cost"]
                            )
                        )
                        for v in requests
                    ),
                    Decimal(0),
                )
                if (
                    len(requests) >= limits["calls"]
                    or used_tokens + input_bound + output_bound > limits["tokens"]
                    or used_cost + reserve > Decimal(limits["cost"])
                ):
                    raise GatewayError("MODEL_PROFILE_BUDGET_EXHAUSTED")
            if (
                budget["calls_remaining"] < 1
                or budget["tokens_remaining"] < input_bound + output_bound
                or Decimal(budget["cost_remaining"]) < reserve
            ):
                raise GatewayError("MODEL_BUDGET_EXHAUSTED")
            call_id = (
                "room-call-"
                + hashlib.sha256(
                    canonical(
                        {
                            "index": len([v for v in rows if v["phase"] == "REQUEST"]),
                            "role": role,
                            "context": context,
                            "profile": provider.profile_id,
                        }
                    ).encode()
                ).hexdigest()
            )
            self._append(
                db,
                "REQUEST",
                {
                    "call_id": call_id,
                    "role": role,
                    "profile_id": provider.profile_id,
                    "model": provider.model,
                    "endpoint_identity": provider.endpoint_identity,
                    "prompt_hash": hashlib.sha256(body.encode()).hexdigest(),
                    "context": context,
                    **(
                        {
                            "audit_context": audit_context,
                            "audit_context_hash": hashlib.sha256(
                                canonical(audit_context).encode()
                            ).hexdigest(),
                        }
                        if audit_context is not None else {}
                    ),
                    "schema": schema.__name__,
                    "parent_decision_id": parent_id,
                    "reserved_tokens": input_bound + output_bound,
                    "reserved_cost": str(reserve),
                    "input_price": str(provider.input_price),
                    "output_price": str(provider.output_price),
                },
            )
            db.commit()
        started = time.monotonic()
        metadata: dict[str, Any] = {}
        decision: T | None = None
        try:
            decision, metadata = provider.complete_structured(
                context, schema, max_output=output_bound
            )
            decision = schema.model_validate(decision.model_dump(mode="json"))
            known = set(context.get("reference_ids", [])) | {
                v["evidence_id"] for v in context.get("evidence", [])
            }
            refs = set(getattr(decision, "evidence_refs", ()))
            if refs != known:
                raise GatewayError("MODEL_EVIDENCE_REFERENCE_INVALID")
            if isinstance(decision, ResearchDecision) and (
                decision.family not in context["allowed_families"]
                or decision.warmup > context["max_long_window"]
                or decision.action not in context["allowed_actions"]
            ):
                raise GatewayError("MODEL_PARAMETER_OUTSIDE_ALLOWLIST")
            input_tokens, output_tokens = (
                metadata.get("input_tokens"),
                metadata.get("output_tokens"),
            )
            usage_known = all(
                type(v) is int and 0 <= v <= 2_000_000
                for v in (input_tokens, output_tokens)
            )
            charged_tokens = (
                cast(int, input_tokens) + cast(int, output_tokens)
                if usage_known
                else input_bound + output_bound
            )
            charged_cost = (
                (
                    Decimal(cast(int, input_tokens)) * provider.input_price
                    + Decimal(cast(int, output_tokens)) * provider.output_price
                )
                / 1_000_000
                if usage_known
                else reserve
            )
            # Usage exceeding a reservation parks subsequent work; never expand policy.
            if charged_tokens > input_bound + output_bound or charged_cost > reserve:
                raise GatewayError("MODEL_USAGE_EXCEEDED_RESERVATION")
            safe: dict[str, Any] = {
                k: metadata[k]
                for k in (
                    "real_model_call",
                    "input_tokens",
                    "output_tokens",
                    "response_hash",
                    "actual_model",
                )
                if k in metadata
            }
            with closing(self.connect()) as db, db:
                self._append(
                    db,
                    "RESPONSE",
                    {
                        "call_id": call_id,
                        "decision_id": "decision-"
                        + hashlib.sha256(
                            (
                                call_id + canonical(decision.model_dump(mode="json"))
                            ).encode()
                        ).hexdigest(),
                        "decision": decision.model_dump(mode="json"),
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "charged_tokens": charged_tokens,
                        "charged_cost": str(charged_cost),
                        "usage_known": usage_known,
                        "pricing": "DECLARED_ASSUMPTION",
                        **safe,
                    },
                )
            return call_id, decision
        except ModelCapacityBusy:
            with closing(self.connect()) as db, db:
                self._append(
                    db,
                    "ERROR",
                    {
                        "call_id": call_id,
                        "classification": "MODEL_PROFILE_BUSY",
                        "cause": "MODEL_GLOBAL_CAPACITY_DENIED",
                        "send_status": "NOT_SENT_VERIFIED",
                        "spend": "NO_SPEND",
                        "retry": "BEFORE_SEND_ONLY",
                    },
                )
            raise
        except Exception as exc:  # noqa: BLE001 -- persist bounded failure; never retry
            if not metadata and isinstance(exc, GatewayError):
                candidate_metadata = getattr(exc, "safe_metadata", {})
                if isinstance(candidate_metadata, dict):
                    metadata = {
                        k: v
                        for k, v in candidate_metadata.items()
                        if k
                        in {
                            "response_hash",
                            "actual_model",
                            "input_tokens",
                            "output_tokens",
                        }
                    }
            classification = (
                str(exc)
                if isinstance(exc, GatewayError)
                and str(exc)
                in {
                    "MODEL_EVIDENCE_REFERENCE_INVALID",
                    "MODEL_PARAMETER_OUTSIDE_ALLOWLIST",
                    "MODEL_USAGE_EXCEEDED_RESERVATION",
                    "MODEL_AUTH",
                    "MODEL_RATE_LIMIT",
                    "MODEL_REQUEST_DENIED",
                    "MODEL_TRANSPORT_UNKNOWN",
                    "MODEL_RESPONSE_SCHEMA",
                    "MODEL_TOOLS_OR_INCOMPLETE",
                    "MODEL_RESPONSE_REDIRECTED",
                    "MODEL_RESPONSE_TOO_LARGE",
                    "MODEL_SECRET_ECHO_REJECTED",
                    "MODEL_GLOBAL_CAPACITY_DENIED",
                    "MODEL_PROFILE_BUDGET_EXHAUSTED",
                    "MODEL_BINDING_REVISION_REVOKED",
                }
                else "MODEL_CALL_FAILED"
            )
            with closing(self.connect()) as db, db:
                self._append(
                    db,
                    "ERROR",
                    {
                        "call_id": call_id,
                        "classification": classification,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "retry": "FORBIDDEN",
                        "spend": "RESERVATION_RETAINED",
                        "verified_response_hash": metadata.get("response_hash"),
                        "actual_model": metadata.get("actual_model"),
                        "schema_failure_types": getattr(
                            exc, "schema_failure_types", []
                        ),
                        "schema_failure_locations": getattr(
                            exc, "schema_failure_locations", []
                        ),
                        "known_usage": {
                            k: metadata[k]
                            for k in ("input_tokens", "output_tokens")
                            if type(metadata.get(k)) is int
                        },
                        "rejected_structured_decision": decision.model_dump(mode="json")
                        if decision is not None
                        else None,
                    },
                )
            raise GatewayError(classification) from None
