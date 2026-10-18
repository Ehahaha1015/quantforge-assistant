"""Bounded scale campaign intelligence through the restricted Core facade."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from .gateway import (
    SMA,
    GatewayError,
    ModelGateway,
    Provider,
    ResearchDecision,
    canonical,
    canonical_strategy,
)


def _open_core(root: Path) -> Any:
    from quantforge.product.scale.service import ScaleService

    return ScaleService(root)


def _safe_context(value: dict[str, Any], family: str) -> dict[str, Any]:
    """Closed JSON projection, never an arbitrary cached prompt/report payload."""
    required = {
        "phase",
        "goal",
        "max_long_window",
        "evidence",
        "family",
        "round_budget",
        "allowed_actions",
    }
    optional = {"context_id", "state_epoch", "benchmark_state"}
    if not isinstance(value, dict) or not required <= set(value) <= required | optional:
        raise GatewayError("SCALE_CONTEXT_SCHEMA")
    if value["phase"] not in ("first", "next") or value["family"] != family:
        raise GatewayError("SCALE_CONTEXT_PHASE")
    if (
        not isinstance(value["goal"], str)
        or not value["goal"].strip()
        or len(value["goal"]) > 5000
        or type(value["max_long_window"]) is not int
        or not 2 <= value["max_long_window"] <= 50
        or type(value["round_budget"]) is not int
        or value["round_budget"] < 1
    ):
        raise GatewayError("SCALE_CONTEXT_BOUNDS")
    actions = value["allowed_actions"]
    if (
        not isinstance(actions, list)
        or not actions
        or any(
            action not in ("propose", "modify", "branch", "continue", "reject")
            for action in actions
        )
    ):
        raise GatewayError("SCALE_CONTEXT_ACTIONS")
    if value.get("benchmark_state", "SEALED") != "SEALED":
        raise GatewayError("SCALE_SEARCH_CLOSED")
    evidence = value["evidence"]
    if not isinstance(evidence, list):
        raise GatewayError("SCALE_CONTEXT_EVIDENCE")
    known: set[str] = set()
    for item in evidence:
        minimum = {"evidence_id", "strategy", "metrics", "limitations"}
        extra = {"family", "run_id", "trial_id"}
        if not isinstance(item, dict) or not minimum <= set(item) <= minimum | extra:
            raise GatewayError("SCALE_CONTEXT_EVIDENCE_SCHEMA")
        reference = item["evidence_id"]
        if (
            not isinstance(reference, str)
            or len(reference) != 64
            or any(c not in "0123456789abcdef" for c in reference)
            or reference in known
        ):
            raise GatewayError("SCALE_CONTEXT_EVIDENCE_ID")
        known.add(reference)
        recipe = item["strategy"]
        try:
            strategy = canonical_strategy(recipe)
        except GatewayError:
            raise GatewayError("SCALE_CONTEXT_RECIPE") from None
        if strategy["family"] != SMA or strategy["parameters"]["long_window"] > 50:
            raise GatewayError("SCALE_CONTEXT_RECIPE")
        metrics = item["metrics"]
        if (
            not isinstance(metrics, dict)
            or not metrics
            or not set(metrics)
            <= {
                "total_return",
                "annualized_volatility",
                "sharpe",
                "max_drawdown",
                "turnover",
                "sharpe_ratio",
            }
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in metrics.values()
            )
        ):
            raise GatewayError("SCALE_CONTEXT_FACTS")
        if not isinstance(item["limitations"], list) or any(
            not isinstance(v, str) for v in item["limitations"]
        ):
            raise GatewayError("SCALE_CONTEXT_LIMITATIONS")
    # Canonical JSON copies prevent an external caller mutating stored/sent context.
    safe: dict[str, Any] = json.loads(canonical(value))
    # Campaign family is a frozen budget bucket; scale-v1 admits only SMA signals.
    safe["allowed_families"] = [SMA]
    return safe


class ScaleController:
    def __init__(self, core_root: Path, lab_root: Path, provider: Provider) -> None:
        if str(lab_root).startswith(("\\\\", "//")):
            raise GatewayError("SCALE_LOCAL_HISTORY_REQUIRED")
        self.core_root, self.lab_root, self.provider = core_root, lab_root, provider

    def run(
        self,
        *,
        steps: int = 1,
        freeze_when_exhausted: bool = False,
        resume: bool = False,
    ) -> dict[str, Any]:
        if type(steps) is not int or not 1 <= steps <= 64:
            raise GatewayError("SCALE_STEPS_BOUND")
        core = _open_core(self.core_root)
        history: sqlite3.Connection | None = None
        gateway: ModelGateway | None = None
        try:
            with core.campaign_lock():
                status = core.status()
                if status["benchmark_state"] != "SEALED":
                    return {
                        "status": "SEARCH_CLOSED",
                        "core": status,
                        "steps_completed": 0,
                    }
                if resume and status["paused"]:
                    core.pause(False)
                    status = core.status()
                if status["paused"]:
                    return {"status": "PAUSED", "core": status, "steps_completed": 0}
                core.recover(locked=True)
                status = core.status()
                program, manifest = status["program_id"], status["manifest_id"]
                if (
                    not isinstance(manifest, str)
                    or len(manifest) != 64
                    or any(c not in "0123456789abcdef" for c in manifest)
                ):
                    raise GatewayError("SCALE_PROGRAM_IDENTITY")
                directory = self.lab_root / ("scale-" + manifest)
                for path in (directory, *directory.parents):
                    if path.is_symlink() or path.is_junction():
                        raise GatewayError("SCALE_HISTORY_REDIRECTED")
                directory.mkdir(parents=True, exist_ok=True)
                database = directory / "history.sqlite"
                if database.is_symlink() or database.is_junction():
                    raise GatewayError("SCALE_HISTORY_REDIRECTED")
                history = sqlite3.connect(database)
                history.execute("PRAGMA journal_mode=WAL")
                history.execute("PRAGMA synchronous=FULL")
                history.execute(
                    "CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,previous_hash TEXT NOT NULL,payload TEXT NOT NULL,event_hash TEXT NOT NULL)"
                )
                for operation in ("UPDATE", "DELETE"):
                    history.execute(
                        f"CREATE TRIGGER IF NOT EXISTS no_{operation.lower()} BEFORE {operation} ON events BEGIN SELECT RAISE(ABORT,'append-only'); END"
                    )
                history.commit()

                def events() -> list[dict[str, Any]]:
                    assert history is not None
                    result: list[dict[str, Any]] = []
                    previous = "0" * 64
                    for seq, before, raw, digest in history.execute(
                        "SELECT seq,previous_hash,payload,event_hash FROM events ORDER BY seq"
                    ):
                        value = json.loads(raw)
                        if (
                            seq != len(result) + 1
                            or before != previous
                            or hashlib.sha256((before + raw).encode()).hexdigest()
                            != digest
                            or canonical(value) != raw
                        ):
                            raise GatewayError("SCALE_HISTORY_INTEGRITY")
                        result.append(value)
                        previous = digest
                    return result

                def append(kind: str, **values: Any) -> None:
                    assert history is not None
                    events()
                    row = history.execute(
                        "SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1"
                    ).fetchone()
                    previous = row[0] if row else "0" * 64
                    raw = canonical({"kind": kind, **values})
                    digest = hashlib.sha256((previous + raw).encode()).hexdigest()
                    with history:
                        history.execute(
                            "INSERT INTO events(previous_hash,payload,event_hash) VALUES(?,?,?)",
                            (previous, raw, digest),
                        )

                retained = events()
                identity = {
                    "program_id": program,
                    "manifest_id": manifest,
                    "provider": self.provider.name,
                    "model": self.provider.model,
                }
                if retained and retained[0] != {"kind": "PROGRAM", **identity}:
                    raise GatewayError("SCALE_HISTORY_PROGRAM_MISMATCH")
                if not retained:
                    append("PROGRAM", **identity)
                calls = directory / "calls.sqlite"
                if calls.is_symlink() or calls.is_junction():
                    raise GatewayError("SCALE_HISTORY_REDIRECTED")
                gateway = ModelGateway(
                    calls, self.provider, max_calls=status["budget"]["model_calls"]
                )
                completed = 0
                for _ in range(steps):
                    status = core.status()
                    if status["paused"] or status["benchmark_state"] != "SEALED":
                        break
                    if (
                        status["trials"] >= status["budget"]["trials"]
                        or status["model_calls"] >= status["budget"]["model_calls"]
                    ):
                        break
                    families = sorted(status["budget"]["families"])
                    # Choose the least-consumed declared family; ties follow fixed lexical order.
                    available = [
                        f
                        for f in families
                        if status["family_counts"][f] < status["budget"]["families"][f]
                    ]
                    if not available:
                        break
                    family = min(
                        available,
                        key=lambda f: (status["family_counts"][f], families.index(f)),
                    )
                    context = _safe_context(core.context(family), family)
                    call = core.reserve_model(
                        family=family
                    )  # authoritative charge before network request
                    append(
                        "MODEL_ALLOCATED",
                        core_call=call,
                        family=family,
                        context_sha256=hashlib.sha256(
                            canonical(context).encode()
                        ).hexdigest(),
                    )
                    ledger_id = None
                    try:
                        ledger_id, decision = gateway.call(context)
                        decision = ResearchDecision.model_validate_json(
                            decision.model_dump_json()
                        )
                        if decision.action not in context["allowed_actions"]:
                            raise GatewayError("SCALE_MODEL_ACTION_OUTSIDE_PHASE")
                        if decision.family != SMA:
                            raise GatewayError("SCALE_MODEL_FAMILY_OUTSIDE_CONTRACT")
                        if context["phase"] == "next" and not decision.evidence_refs:
                            raise GatewayError("SCALE_MODEL_REFERENCE_REQUIRED")
                        append(
                            "MODEL_DECISION",
                            core_call=call,
                            ledger_id=ledger_id,
                            decision=decision.model_dump(mode="json"),
                        )
                        core.finish_model(call, status="RESPONSE", ledger_id=ledger_id)
                    except (
                        ValueError,
                        OSError,
                        TypeError,
                        KeyError,
                        RuntimeError,
                        sqlite3.Error,
                    ) as exc:
                        append(
                            "MODEL_FAILED",
                            core_call=call,
                            ledger_id=ledger_id,
                            error_class=type(exc).__name__,
                        )
                        failed_status = core.status()
                        if (
                            failed_status["benchmark_state"] == "SEALED"
                            and not failed_status["paused"]
                        ):
                            invalid = core.submit(
                                f"trial-{failed_status['trials'] + 1}",
                                family,
                                {"invalid_model_attempt": True},
                                origin="AI",
                                call_id=call,
                            )
                            append(
                                "REJECTED_ATTEMPT_REFERENCE",
                                core_call=call,
                                allocation=invalid.get("allocation"),
                                status=invalid.get("status"),
                                reason=invalid.get("reason"),
                            )
                        ambiguous = ledger_id is None and (
                            not isinstance(exc, GatewayError)
                            or "TRANSPORT_UNKNOWN" in str(exc)
                        )
                        if not ambiguous:
                            core.finish_model(call, status="ERROR", ledger_id=ledger_id)
                        raise GatewayError(
                            "SCALE_MODEL_ATTEMPT_FAILED_NO_RETRY"
                        ) from None
                    latest = core.status()
                    if latest["benchmark_state"] != "SEALED" or latest["paused"]:
                        append("LATE_DECISION_IGNORED", core_call=call)
                        break
                    trial_id = f"trial-{latest['trials'] + 1}"
                    proposal = decision.parameters
                    if decision.action == "reject":
                        proposal = {"rejected_idea": True}
                    # Core allocates every attempt before admission, including duplicates/rejects.
                    result = core.submit(
                        trial_id, family, proposal, origin="AI", call_id=call
                    )
                    append(
                        "TRIAL_REFERENCE",
                        core_call=call,
                        trial_id=trial_id,
                        family=family,
                        result={
                            key: result[key]
                            for key in (
                                "admitted",
                                "allocation",
                                "status",
                                "reason",
                                "run_id",
                                "evidence_id",
                            )
                            if key in result
                        },
                    )
                    completed += 1
                status = core.status()
                exhausted = (
                    status["trials"] >= status["budget"]["trials"]
                    or status["model_calls"] >= status["budget"]["model_calls"]
                )
                frozen = None
                if (
                    freeze_when_exhausted
                    and exhausted
                    and status["benchmark_state"] == "SEALED"
                    and not status["paused"]
                ):
                    frozen = core.freeze()
                    append(
                        "CORE_FREEZE_REFERENCE",
                        freeze={
                            key: frozen[key]
                            for key in (
                                "run_id",
                                "evidence_id",
                                "freeze_id",
                                "status",
                                "program_id",
                            )
                            if key in frozen
                        },
                    )
                    status = core.status()
                summary = {
                    "status": "EXHAUSTED"
                    if exhausted
                    else "PAUSED"
                    if status["paused"]
                    else "BOUNDED_STEP_COMPLETE",
                    "core": status,
                    "steps_completed": completed,
                    "frozen": frozen,
                    "history": str(database),
                    "call_ledger": str(calls),
                    "provider": self.provider.name,
                    "model": self.provider.model,
                    "real_calls_proven": any(
                        row["phase"] == "RESPONSE" for row in gateway.ledger()
                    )
                    and all(
                        row.get("real_model_call") is True
                        for row in gateway.ledger()
                        if row["phase"] == "RESPONSE"
                    ),
                }
                append("BOUNDED_RETURN", steps_completed=completed, exhausted=exhausted)
                return summary
        finally:
            if gateway is not None:
                gateway.close()
            if history is not None:
                history.close()
            core.close()


def inspect_scale_history(lab_root: Path, manifest_id: str) -> list[dict[str, Any]]:
    if (
        not isinstance(manifest_id, str)
        or len(manifest_id) != 64
        or any(c not in "0123456789abcdef" for c in manifest_id)
    ):
        raise GatewayError("SCALE_PROGRAM_IDENTITY")
    directory = lab_root / ("scale-" + manifest_id)
    database = directory / "history.sqlite"
    if (
        any(
            path.is_symlink() or path.is_junction()
            for path in (directory, *directory.parents)
        )
        or database.is_symlink()
        or database.is_junction()
        or not database.is_file()
    ):
        raise GatewayError("SCALE_HISTORY_MISSING_OR_REDIRECTED")
    result: list[dict[str, Any]] = []
    previous = "0" * 64
    with sqlite3.connect(
        database.resolve().as_uri() + "?mode=ro", uri=True
    ) as connection:
        for seq, before, raw, digest in connection.execute(
            "SELECT seq,previous_hash,payload,event_hash FROM events ORDER BY seq"
        ):
            value = json.loads(raw)
            if (
                seq != len(result) + 1
                or before != previous
                or hashlib.sha256((before + raw).encode()).hexdigest() != digest
                or canonical(value) != raw
            ):
                raise GatewayError("SCALE_HISTORY_INTEGRITY")
            result.append(value)
            previous = digest
    return result
