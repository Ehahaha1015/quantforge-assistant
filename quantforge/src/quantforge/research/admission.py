"""Deterministic proposal admission without state, persistence or evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quantforge.data import DatasetSnapshot
from quantforge.experiments import FrozenPayload, canonical_config_hash

from .models import (
    ExperimentSpec,
    ResearchAdmissionError,
    ResearchRequest,
    _normalize_recipe,
)


def admit_research_request(
    request: ResearchRequest,
    *,
    proposal: Mapping[str, Any] | FrozenPayload,
    dataset_snapshot: DatasetSnapshot,
    data_conventions: Mapping[str, Any] | FrozenPayload,
    code_context: Mapping[str, Any] | FrozenPayload,
    split_timestamps: Mapping[str, Any] | FrozenPayload,
) -> ExperimentSpec:
    """Freeze supported fields and declared metadata before any OOS use.

    Snapshot/code metadata bind the intended inputs, not their authenticity.
    Later composition must verify retained bytes, executing code and the OOS
    seal. This function neither accesses data nor grants test-use permission.
    """
    try:
        if not isinstance(request, ResearchRequest) or not isinstance(
            dataset_snapshot, DatasetSnapshot
        ):
            raise TypeError(
                "request and snapshot must use existing validated model boundaries"
            )
        copied_request = ResearchRequest.model_validate(
            request.model_dump(mode="python", warnings="error")
        )
        recipe = _normalize_recipe(
            copied_request,
            proposal,
            dataset_snapshot,
            data_conventions,
            code_context,
            split_timestamps,
        )
        body = {
            "schema_version": (
                "experiment-spec-v1"
                if recipe["schema_version"] == "research-recipe-v1"
                else "experiment-spec-v2"
            ),
            "request": copied_request.model_dump(mode="json"),
            "recipe": recipe.to_payload(),
        }
        return ExperimentSpec.model_validate(
            {**body, "config_hash": canonical_config_hash(body)}
        )
    except (ValueError, TypeError, OverflowError):
        raise ResearchAdmissionError("research proposal is not admissible") from None
