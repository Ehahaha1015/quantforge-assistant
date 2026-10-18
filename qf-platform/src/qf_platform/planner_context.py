"""Bounded planner views with complete provenance retained by the host."""

from __future__ import annotations

from typing import Any, cast

from .storage import digest


def compact_dataset(metadata: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: metadata[key]
        for key in (
            "dataset", "core_trials_remaining", "scope", "basis",
            "total_return_qualified", "validated_alpha", "live_authority",
        )
    }
    result["data_window"] = {
        key: metadata["dataset_snapshot"][key]
        for key in ("min_timestamp", "max_timestamp", "row_count", "as_of")
    }
    result["full_metadata_hash"] = digest(metadata)
    result["source_assurance"] = "OPERATOR_SNAPSHOT_NOT_EXCHANGE_OR_PIT"
    result["unqualified"] = [
        "calendar", "corporate_actions", "entitlement", "liquidity_capacity",
        "PIT_universe", "broker_rules", "alpha", "total_return", "live",
    ]
    return result


def compact_knowledge(knowledge: dict[str, Any]) -> dict[str, Any]:
    return {
        "ranking": knowledge["ranking"],
        "as_of": knowledge["as_of"],
        "grants_authority": False,
        "full_provenance_hash": digest(knowledge),
        "source_selection": "FULL_TEXT_ALL_CLAIMS_HOST_FILTERED_SCOPE_TIME_PERMISSION",
        "sources": [
            {key: item[key] for key in ("id", "text", "content_hash", "claims")}
            for item in knowledge["sources"]
        ],
    }


def original_context(request: dict[str, Any]) -> dict[str, Any]:
    """Recover full host provenance only when it exactly binds the submitted view."""
    context = request["context"]
    if request["schema"] != "CompactResearchPlan":
        return cast(dict[str, Any], context)
    audit = request.get("audit_context")
    if (
        context.get("context_profile") != "BM25_COMPACT_V3_SAME_INTENT_ADMISSION"
        or not isinstance(audit, dict)
        or set(audit) != {"dataset", "knowledge"}
        or context.get("dataset") != compact_dataset(audit["dataset"])
        or context.get("knowledge") != compact_knowledge(audit["knowledge"])
        or request.get("audit_context_hash") != digest(audit)
    ):
        raise ValueError("Compact planner provenance changed")
    references = ["dataset-meta-" + digest(audit["dataset"])] + [
        "knowledge-" + item["content_hash"] for item in audit["knowledge"]["sources"]
    ]
    if context["reference_ids"] != references:
        raise ValueError("Compact planner references changed")
    return {**context, **audit}
