"""Bound prompt views of retained development windows without altering evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def bounded_context(context: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(context)

    def windows(facts: dict[str, Any], evidence_id: str) -> None:
        retained = facts.get("validation_windows", [])
        limit = 12
        if len(retained) <= limit:
            return
        indices = [i * (len(retained) - 1) // (limit - 1) for i in range(limit)]
        facts["validation_windows"] = [retained[i] for i in indices]
        facts["validation_window_view"] = {
            "policy": "CHRONOLOGICAL_EVEN_SAMPLE_FOR_PROMPT_ONLY",
            "retained_count": len(retained),
            "supplied_indices": indices,
            "full_evidence_id": evidence_id,
            "omitted_windows_are_not_assumed_pass": True,
        }

    for evidence in result.get("evidence", []):
        windows(evidence["FACT"], evidence["evidence_id"])
    for trial in result.get("diagnostics", {}).get("trials", []):
        windows(trial, trial["evidence_id"])
    return result
