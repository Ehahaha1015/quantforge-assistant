"""Canonical facts ABI. Markdown is a disposable view of these machine bytes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from quantforge.experiments import canonical_json


def evidence_bytes(payload: dict[str, Any]) -> bytes:
    return canonical_json(payload).encode("utf-8")


def evidence_id(payload: dict[str, Any]) -> str:
    return hashlib.sha256(evidence_bytes(payload)).hexdigest()


def read_evidence(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("redirected evidence")
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or raw != evidence_bytes(payload):
        raise ValueError("noncanonical evidence")
    if set(payload) != {"schema_version", "FACT", "DECLARED_ASSUMPTION", "LIMITATION"}:
        raise ValueError("invalid evidence schema")
    if payload["schema_version"] != "evidence-pack-v1":
        raise ValueError("unsupported evidence version")
    return payload


def render_markdown(payload: dict[str, Any]) -> str:
    facts = payload["FACT"]
    lines = [
        "# Experiment evidence",
        "",
        f"Evidence ID: `{evidence_id(payload)}`",
        "",
        f"Run: `{facts['run_id']}`; stage: `{facts['evaluation_stage']}`",
        "",
        "## FACT",
        "",
        "```json",
        json.dumps(facts, indent=2, ensure_ascii=False),
        "```",
    ]
    for group in ("DECLARED_ASSUMPTION", "LIMITATION"):
        lines.extend(["", f"## {group}", ""])
        lines.extend(f"- {item}" for item in payload[group])
    return "\n".join(lines) + "\n"
