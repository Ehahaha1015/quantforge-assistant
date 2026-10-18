"""Read-only, rebuildable cross-room intelligence; facts remain Core references."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantforge.product.storage import local_root

from .gateway import GatewayError, canonical
from .room_gateway import RoomGateway


def library(root: Path, *, exclude: Path | None = None) -> dict[str, Any]:
    root = local_root(root)
    rooms = [
        p
        for p in sorted(root.iterdir())
        if (p / "room-history.sqlite").exists() and p != exclude
    ]
    if len(rooms) > 100:
        raise GatewayError("LIBRARY_SOURCE_LIMIT")
    sources: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    hypotheses: list[dict[str, Any]] = []
    critiques: list[dict[str, Any]] = []
    strategies: list[dict[str, Any]] = []
    for directory in rooms:
        local_root(directory)
        path = directory / "room-history.sqlite"
        if path.is_symlink():
            raise GatewayError("LIBRARY_SOURCE_INVALID")
        try:
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            ) as database:
                if database.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError
                history = [
                    {"seq": seq, **json.loads(raw)}
                    for seq, raw in database.execute(
                        "SELECT seq,payload FROM events ORDER BY seq"
                    )
                ]
            if not history:
                continue
            if history[0].get("kind") != "DECLARATION":
                raise ValueError
            declaration = history[0]["declaration"]
            if not any(
                v["kind"] in {"COMPLETE", "FAILED"}
                or (v["kind"] == "CONTROL" and v["action"] == "stop")
                for v in history
            ):
                continue
            budget = declaration["budget"]
            ledger = RoomGateway(
                directory,
                calls=budget["calls"],
                tokens=budget["tokens"],
                cost=Decimal(budget["cost"]),
                max_context_bytes=budget["max_context_bytes"],
                require_existing=True,
            ).ledger()
            origin = {
                "program_id": declaration["program_id"],
                "declared_at": declaration.get("declared_at", declaration["deadline"]),
                "chronology": "DECLARED_AT"
                if "declared_at" in declaration
                else "LEGACY_DEADLINE_PROXY",
                "dataset_id": declaration["dataset_id"],
                "dataset_content_hash": declaration["dataset"]["content_hash"],
                "mode": declaration["mode"],
                "source_identity": hashlib.sha256(
                    canonical({"history": history, "ledger": ledger}).encode()
                ).hexdigest(),
            }
            sources.append(origin)
            for event in history:
                if event["kind"] in {"FAILED", "ROLE_PROVIDER_FAILED"}:
                    failures.append(
                        {
                            **origin,
                            "classification": "TECHNICAL_FAILURE",
                            "reason": event.get(
                                "classification", "LEGACY_PROVIDER_FAILURE_UNCLASSIFIED"
                            ),
                            "evidence_refs": [],
                        }
                    )
                if event["kind"] == "FREEZE_REFERENCE":
                    prepared = next(
                        (
                            v
                            for v in history
                            if v["kind"] == "EXPERIMENT_PREPARE"
                            and v["run_id"] == event["run_id"]
                        ),
                        {},
                    )
                    strategies.append(
                        {
                            **origin,
                            "run_id": event["run_id"],
                            "evidence_refs": [event["evidence_id"]],
                            "status": event["status"],
                            "alpha_status": "NOT_ESTABLISHED",
                            "strategy": prepared.get("strategy"),
                        }
                    )
            for call in ledger:
                if call["phase"] != "RESPONSE":
                    continue
                decision = call["decision"]
                record = {
                    **origin,
                    "decision_id": call["decision_id"],
                    "call_id": call["call_id"],
                    "rationale": decision["rationale"][:800],
                    "evidence_refs": decision["evidence_refs"],
                }
                if decision.get("action") in {
                    "propose",
                    "modify",
                    "branch",
                    "continue",
                }:
                    hypotheses.append(
                        {
                            **record,
                            "family": decision["family"],
                            "parameters": {
                                k: v
                                for k, v in decision.items()
                                if k
                                not in {
                                    "action",
                                    "family",
                                    "rationale",
                                    "evidence_refs",
                                }
                            },
                            "parents": decision["evidence_refs"],
                        }
                    )
                if decision.get("verdict") in {
                    "freeze",
                    "reject",
                    "continue",
                    "insufficient",
                    "concerns",
                    "descriptive_only",
                }:
                    critiques.append(
                        {
                            **record,
                            "verdict": decision["verdict"],
                            "concerns": decision.get("concerns", []),
                        }
                    )
                if (
                    decision.get("verdict") == "reject"
                    or decision.get("next") == "reject"
                ):
                    failures.append(
                        {
                            **record,
                            "classification": "RESEARCH_REJECTED",
                            "reason": decision["rationale"][:800],
                            "concerns": decision.get("concerns", []),
                        }
                    )
                elif decision.get("verdict") == "insufficient":
                    failures.append(
                        {
                            **record,
                            "classification": "EVIDENCE_INSUFFICIENT",
                            "reason": decision["rationale"][:800],
                        }
                    )
        except (KeyError, TypeError, ValueError, sqlite3.Error, OSError) as exc:
            raise GatewayError("LIBRARY_SOURCE_INVALID") from exc
    # UUID directory names and filesystem mtimes are not research chronology.
    # Retained legacy declarations have only an immutable deadline proxy.
    for records in (sources, hypotheses, failures, strategies, critiques):
        records.sort(
            key=lambda v: (datetime.fromisoformat(v["declared_at"]), v["program_id"])
        )
    result = {
        "schema_version": "research-library-v1",
        "sources": sources,
        "hypotheses": hypotheses,
        "failures": failures,
        "strategies": strategies,
        "critiques": critiques,
        "authority": "INTELLIGENCE_ONLY_CORE_EVIDENCE_REFERENCES",
        "exposure_policy": "Historical exposure identities retained; retrieval never grants new dataset admission or final access",
    }
    return {
        **result,
        "memory_id": "library-"
        + hashlib.sha256(canonical(result).encode()).hexdigest(),
    }


def retrieve(
    root: Path, *, exclude: Path, families: tuple[str, ...], max_bytes: int = 8000
) -> dict[str, Any]:
    index = library(root, exclude=exclude)
    result: dict[str, Any] = {
        "memory_id": index["memory_id"],
        "authority": index["authority"],
        "exposure_policy": index["exposure_policy"],
        "lessons": [],
        "prior_hypotheses": [],
        "source_count": len(index["sources"]),
        "transferability": "Different dataset/regime evidence is a prior lesson, never proof about the new dataset",
        "truncated": False,
    }
    for item in reversed(index["failures"]):
        if item["classification"] == "TECHNICAL_FAILURE":
            continue
        candidate = {
            **item,
            "rationale": item.get("rationale", "")[:500],
            "reason": item["reason"][:500],
        }
        result["lessons"].append(candidate)
        if (
            len(canonical(result).encode()) > max_bytes - 150
            or len(result["lessons"]) > 4
        ):
            result["lessons"].pop()
            result["truncated"] = True
            break
    for item in reversed(index["hypotheses"]):
        if item["family"] not in families:
            continue
        result["prior_hypotheses"].append(
            {**item, "rationale": item["rationale"][:300]}
        )
        if (
            len(canonical(result).encode()) > max_bytes - 150
            or len(result["prior_hypotheses"]) > 4
        ):
            result["prior_hypotheses"].pop()
            result["truncated"] = True
            break
    result["context_id"] = (
        "context-" + hashlib.sha256(canonical(result).encode()).hexdigest()
    )
    return result
