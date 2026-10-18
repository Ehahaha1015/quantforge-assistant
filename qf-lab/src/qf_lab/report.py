"""Derived terminal reports with explicit inference and reproduction limits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .gateway import GatewayError
from .room import ResearchRoom


def report(room: ResearchRoom) -> dict[str, Any]:
    state = room.snapshot()
    if state["status"] not in {"COMPLETE", "FAILED", "STOPPED"}:
        raise GatewayError("ROOM_REPORT_REQUIRES_TERMINAL_STATE")
    declaration = state["declaration"]
    scientific = state["rounds_completed"] > 0 or state["reviews_completed"] > 0
    text = (
        f"# Research report: {declaration['program_id']}\n\n"
        f"Status: {state['status']}\nMode: {state['mode']}\nProvider: {declaration['provider_mode']}\n"
        f"Completed research rounds: {state['rounds_completed']}\n"
        f"Completed retained reviews: {state['reviews_completed']}\n"
        f"Outcome classification: {'EVIDENCE_BASED_RESEARCH_DECISION' if scientific else 'TECHNICAL_OR_UNEXECUTED_TERMINATION'}\n"
        "Alpha: NOT ESTABLISHED. Live authority: NONE.\n\n"
        "## Statistical applicability\n\n"
        "CSCV/PBO: NOT_APPLICABLE for the adaptively selected research path. Additional bars do not make it a predeclared fixed grid. "
        "DSR: NOT_APPLICABLE without a justified independent trial count and comparable distribution. "
        "Block-sign diagnostics and their sample/independence prerequisites are retained in ROUND_COMMITTED.diagnostics; "
        "insufficient samples remain insufficient. A reject/stop decision is a valid scientific outcome only when supported by completed evidence.\n\n"
        "## Reproduction\n\n"
        "Use the exact Core Git commit, source identity, uv.lock and runtime bound in each EvidencePack. "
        "Install with `uv sync --locked --group dev`. For every FACT_MEMORY run use "
        f'`uv run quantforge --root "{room.core_root}" reproduce <run_id>`. '
        "Inspect EvidencePack.FACT.specification.recipe.code_context for bindings. Historical EXACT_MATCH does not certify a different current version. "
        "Final holdout remains sealed; these instructions do not open it.\n\n"
        "## Frozen memory and model budget\n\n"
        "Fees use declared prices, not reconciled provider invoices. Failed/unknown calls retain reservations and must not be replayed. "
        "The cross-room retrieval snapshot below is frozen for this declaration; historical opinions do not grant new data admission.\n\n"
        "```json\n"
        + json.dumps(
            {
                "budget": state["budget"],
                "retrieval": declaration.get("research_memory", {}),
                "memory": room.memory(),
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n```\n"
    )
    path: Path = room.root / "RESEARCH-REPORT.md"
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
    except FileExistsError:
        if path.is_symlink() or path.read_text(encoding="utf-8") != text:
            raise GatewayError("ROOM_REPORT_ALREADY_EXISTS") from None
    return {"report": str(path), "status": state["status"]}
