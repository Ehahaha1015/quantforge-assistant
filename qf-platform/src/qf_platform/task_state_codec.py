"""Immutable presentation blobs; old events and original Core facts stay untouched."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from collections import OrderedDict
from typing import Any

from .storage import PlatformError, canonical, digest

CODEC = "qf-task-state-blobs-v1"


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS state_blobs "
        "(hash TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    for operation in ("UPDATE", "DELETE"):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS state_blobs_no_{operation.lower()} "
            f"BEFORE {operation} ON state_blobs BEGIN "
            "SELECT RAISE(ABORT,'append-only'); END"
        )


def _store(db: sqlite3.Connection, value: Any) -> str:
    raw = canonical(value)
    identity = digest(value)
    existing = db.execute(
        "SELECT payload FROM state_blobs WHERE hash=?", (identity,)
    ).fetchone()
    if existing is not None:
        if existing[0] != raw:
            raise PlatformError("TASK_BLOB_IDENTITY_CONFLICT")
    else:
        db.execute("INSERT INTO state_blobs VALUES(?,?)", (identity, raw))
    return identity


def pack(db: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
    if "evidence_blob_refs" in state or "report_blob_ref" in state:
        raise PlatformError("TASK_STATE_CODEC_RESERVED_FIELDS")
    result = {
        key: copy.deepcopy(value)
        for key, value in state.items()
        if key not in {"evidence", "report"}
    }
    evidence = state.get("evidence", [])
    if not isinstance(evidence, list) or not all(
        isinstance(item, dict) for item in evidence
    ):
        raise PlatformError("TASK_STATE_EVIDENCE_INVALID")
    result["evidence_blob_refs"] = [_store(db, value) for value in evidence]
    report = state.get("report")
    if report is not None and not isinstance(report, dict):
        raise PlatformError("TASK_STATE_REPORT_INVALID")
    result["report_blob_ref"] = _store(db, report) if report is not None else None
    return result


def references(state: dict[str, Any]) -> list[str]:
    evidence, report = state.get("evidence_blob_refs"), state.get("report_blob_ref")
    if (
        "evidence" in state
        or "report" in state
        or not isinstance(evidence, list)
        or len(evidence) > 100
    ):
        raise PlatformError("TASK_STATE_CODEC_INVALID")
    values = evidence + ([] if report is None else [report])
    if not all(
        isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
        for value in values
    ):
        raise PlatformError("TASK_STATE_BLOB_REFERENCE_INVALID")
    return values


def _read(db: sqlite3.Connection, identity: str) -> tuple[str, dict[str, Any]]:
    row = db.execute(
        "SELECT payload FROM state_blobs WHERE hash=?", (identity,)
    ).fetchone()
    if row is None:
        raise PlatformError("TASK_STATE_BLOB_MISSING")
    value = json.loads(row[0])
    if (
        not isinstance(value, dict)
        or canonical(value) != row[0]
        or digest(value) != identity
    ):
        raise PlatformError("TASK_STATE_BLOB_INTEGRITY_FAILED")
    return row[0], value


def verify(
    db: sqlite3.Connection, state: dict[str, Any], seen: OrderedDict[str, None]
) -> None:
    for identity in references(state):
        if identity not in seen:
            _read(db, identity)
            seen[identity] = None
            if len(seen) > 4096:
                seen.popitem(last=False)
        else:
            seen.move_to_end(identity)


def unpack(db: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
    references(state)
    result = {
        key: copy.deepcopy(value)
        for key, value in state.items()
        if key not in {"evidence_blob_refs", "report_blob_ref"}
    }
    result["evidence"] = [
        _read(db, identity)[1] for identity in state["evidence_blob_refs"]
    ]
    identity = state["report_blob_ref"]
    result["report"] = _read(db, identity)[1] if identity is not None else None
    return result
