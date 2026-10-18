from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from quantforge.data import DatasetSnapshot
from quantforge.experiments import FrozenPayload, canonical_config_hash, canonical_json
from quantforge.research import (
    ExperimentSpec,
    ResearchAdmissionError,
    ResearchRequest,
    admit_research_request,
)

from ._research_fixtures import complete_inputs, request_fields, set_field


@pytest.mark.parametrize("field", list(request_fields()))
def test_every_request_field_is_required(field: str) -> None:
    fields = request_fields()
    del fields[field]
    with pytest.raises(ValidationError):
        ResearchRequest(**fields)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("schema_version", "research-request-v2"),
        ("schema_version", True),
        ("goal_text", ""),
        ("goal_text", " "),
        ("goal_text", 123),
        ("goal_text", " untrimmed"),
        ("goal_text", "text\x00instruction"),
        ("symbol", ""),
        ("symbol", "SPY "),
        ("symbol", 123),
        ("symbol", "SPY\n"),
        ("symbol", "S\x7fPY"),
        ("frequency", "hourly"),
        ("frequency", "DAILY"),
        ("submitted_at", datetime(2026, 10, 8)),  # noqa: DTZ001 -- deliberately naive.
        ("submitted_at", "2026-10-08T01:02:03"),
        ("submitted_at", 1_791_000_000),
        ("submitted_at", True),
        ("constraints", {}),
        ("constraints", []),
        ("constraints.max_drawdown_loss_magnitude", 0),
        ("constraints.max_drawdown_loss_magnitude", -0.15),
        ("constraints.max_drawdown_loss_magnitude", 1.01),
        ("constraints.max_drawdown_loss_magnitude", True),
        ("constraints.max_drawdown_loss_magnitude", "0.15"),
        ("constraints.max_drawdown_loss_magnitude", float("nan")),
        ("constraints.max_drawdown_loss_magnitude", float("inf")),
        ("constraints.max_drawdown_loss_magnitude", -float("inf")),
        ("provenance", {"source": "llm"}),
        ("provenance", {"reference": "proposal"}),
        ("provenance.source", "certified"),
        ("provenance.reference", ""),
        ("provenance.reference", 12),
    ],
)
def test_request_invalid_values_raise_pydantic_validation_error(
    path: str, value: Any
) -> None:
    fields = request_fields()
    set_field(fields, path, value)
    with pytest.raises(ValidationError):
        ResearchRequest(**fields)


def test_symbol_rejects_unicode_c1_control_at_model_boundary() -> None:
    fields = request_fields()
    fields["symbol"] = "S\x80PY"  # U+0080 is a Unicode control character (Cc).
    with pytest.raises(ValidationError):
        ResearchRequest(**fields)


def test_symbol_rejects_unicode_c1_control_in_unsafe_request_at_admission() -> None:
    inputs = complete_inputs()
    inputs["request"] = inputs["request"].model_copy(update={"symbol": "S\x80PY"})
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    "identifier", ["", "-abc", ".abc", "_abc", "a/b", "a b", "é", "a" * 129, 12]
)
def test_experiment_identifier_reuses_existing_ascii_grammar(identifier: Any) -> None:
    fields = request_fields()
    fields["experiment_id"] = identifier
    with pytest.raises(ValidationError):
        ResearchRequest(**fields)


@pytest.mark.parametrize("identifier", ["0", "a.b_c-d", "Z" * 128])
def test_experiment_identifier_boundaries(identifier: str) -> None:
    fields = request_fields()
    fields["experiment_id"] = identifier
    assert ResearchRequest(**fields).experiment_id == identifier


@pytest.mark.parametrize("source", ["user", "llm", "scripted"])
def test_provenance_is_informational_for_each_allowed_source(source: str) -> None:
    fields = request_fields()
    fields["provenance"]["source"] = source
    fields["constraints"]["max_drawdown_loss_magnitude"] = 1
    request = ResearchRequest(**fields)
    assert isinstance(request.provenance, FrozenPayload)
    assert isinstance(request.constraints, FrozenPayload)
    assert request.provenance["source"] == source
    assert request.constraints["max_drawdown_loss_magnitude"] == 1.0
    assert type(request.constraints["max_drawdown_loss_magnitude"]) is float


@pytest.mark.parametrize(
    "name", ["turnover", "annualized_turnover", "max_annualized_turnover"]
)
def test_request_has_no_unsupported_turnover_rule(name: str) -> None:
    fields = request_fields()
    fields["constraints"][name] = 1.0
    with pytest.raises(ValidationError):
        ResearchRequest(**fields)
    inputs = complete_inputs()
    # Adversarial model_copy deliberately bypasses validated construction.
    inputs["request"] = inputs["request"].model_copy(
        update={"constraints": fields["constraints"]}
    )
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize("path", ["", "provenance", "constraints"])
@pytest.mark.parametrize(
    "field", ["metrics", "sharpe", "verified_facts", "oos_result", "test_permission"]
)
def test_structured_results_and_authority_are_forbidden_in_request(
    path: str, field: str
) -> None:
    fields = request_fields()
    target = fields[path] if path else fields
    target[field] = {"claim": "llm says this is profitable"}
    with pytest.raises(ValidationError):
        ResearchRequest(**fields)


@pytest.mark.parametrize("mutation", ["copy", "construct"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "forged"),
        ("experiment_id", "../experiment"),
        ("frequency", "hourly"),
        ("symbol", "SPY\nMSFT"),
        ("submitted_at", datetime(2026, 10, 8)),  # noqa: DTZ001 -- adversarial instance.
        ("constraints", {"max_drawdown_loss_magnitude": 0}),
        ("provenance", {"source": "llm", "reference": "x", "sharpe": 9000}),
    ],
)
def test_unsafe_request_instances_are_revalidated_at_each_boundary(
    mutation: str,
    field: str,
    value: Any,
) -> None:
    inputs = complete_inputs()
    request = inputs["request"]
    if mutation == "copy":
        forged = request.model_copy(update={field: value})
    else:
        forged = ResearchRequest.model_construct(
            **{**request.model_dump(mode="python"), field: value}
        )
    with pytest.raises(ValidationError):
        ResearchRequest.model_validate(forged)
    inputs["request"] = forged
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)
    good = admit_research_request(**complete_inputs())
    with pytest.raises(ValidationError):
        ExperimentSpec(
            schema_version="experiment-spec-v1",
            request=forged,
            recipe=good.recipe,
            config_hash=good.config_hash,
        )


@pytest.mark.parametrize("mutation", ["copy", "construct"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("row_count", True),
        ("row_count", 1260.0),
        ("row_count", "1260"),
        ("row_count", 1259),
        ("row_count", 0),
        ("min_timestamp", None),
        ("max_timestamp", None),
        ("min_timestamp", datetime(2020, 1, 9, 14, 30, tzinfo=UTC)),
        ("max_timestamp", datetime(2020, 1, 1, 14, 30, tzinfo=UTC)),
        ("as_of", datetime(2020, 1, 8, 14, 30, tzinfo=UTC)),
        ("as_of", datetime(2020, 1, 10)),  # noqa: DTZ001 -- adversarial snapshot.
        ("dataset_id", "../bars"),
        ("dataset_id", "CON"),
        ("content_hash", "B" * 64),
        ("content_hash", "b" * 63),
        ("relative_path", "../bars.parquet"),
        ("relative_path", "D:/bars.parquet"),
        ("relative_path", "bars\\file.parquet"),
        ("source", ""),
        ("schema_version", ""),
    ],
)
def test_unsafe_native_snapshot_is_revalidated_without_reading_data(
    mutation: str,
    field: str,
    value: Any,
) -> None:
    inputs = complete_inputs()
    snapshot = inputs["dataset_snapshot"]
    inputs["dataset_snapshot"] = (
        snapshot.model_copy(update={field: value})
        if mutation == "copy"
        else DatasetSnapshot.model_construct(
            **{**snapshot.model_dump(mode="python"), field: value}
        )
    )
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


INVALID_RECIPE_CHANGES = [
    ("schema_version", "research-recipe-v2"),
    ("hypothesis.schema_version", "research-hypothesis-v2"),
    ("hypothesis.experiment_id", "different-id"),
    ("hypothesis.provenance.reference", "different-reference"),
    ("hypothesis.statement", ""),
    ("symbol", "QQQ"),
    ("frequency", "hourly"),
    ("strategy.name", "unknown"),
    ("strategy.short_window", True),
    ("strategy.position_rule", "always_long"),
    ("execution.timing", "same_bar_close"),
    ("execution.terminal_liquidation", True),
    ("backtest.fee_bps", 0),
    ("backtest.periods_per_year", 365),
    ("costs.slippage", "zero"),
    ("split.observations", 1),
    ("split.warmup_bars", 49),
    ("split.validation_window_bars", 64),
    ("parameter_optimization", "grid"),
    ("random_seed", 1),
    ("dataset_snapshot.row_count", 1260.0),
    ("dataset_snapshot.source", ""),
    ("dataset_snapshot.content_hash", "INVALID"),
    ("dataset_snapshot.relative_path", "../bars"),
    ("dataset_snapshot.max_timestamp", "2020-01-08T14:30:00Z"),
    ("data_conventions.session", ""),
    ("code_context.git_commit", "HEAD"),
    ("code_context.runtime.python", "3.11.0"),
    ("split_timestamps.validation_start", "2020-01-03T14:30:00Z"),
    ("screen.policy_version", "approved"),
    ("screen.max_drawdown_loss_magnitude", 0.20),
    ("oos_boundary", "SEALED_TEST_PERMISSION"),
]


@pytest.mark.parametrize(("path", "value"), INVALID_RECIPE_CHANGES)
def test_direct_spec_and_replay_repeat_all_invariants_even_with_recomputed_hash(
    path: str,
    value: Any,
) -> None:
    body = admit_research_request(**complete_inputs()).to_config().to_payload()
    set_field(body["recipe"], path, value)
    document = {**body, "config_hash": canonical_config_hash(body)}
    with pytest.raises(ValidationError):
        ExperimentSpec(**document)
    with pytest.raises(ValueError):
        ExperimentSpec.from_json(canonical_json(document))


@pytest.mark.parametrize(
    "path",
    [
        "",
        "hypothesis",
        "hypothesis.provenance",
        "strategy",
        "execution",
        "backtest",
        "costs",
        "split",
        "dataset_snapshot",
        "data_conventions",
        "code_context",
        "code_context.runtime",
        "split_timestamps",
        "screen",
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_direct_spec_and_replay_require_exact_expanded_recipe_shape(
    path: str, mutation: str
) -> None:
    body = admit_research_request(**complete_inputs()).to_config().to_payload()
    obj = body["recipe"]
    if path:
        for part in path.split("."):
            obj = obj[part]
    if mutation == "missing":
        del obj[next(iter(obj))]
    else:
        obj["oos_result"] = {"sharpe": 9000}
    document = {**body, "config_hash": canonical_config_hash(body)}
    with pytest.raises(ValidationError):
        ExperimentSpec(**document)
    with pytest.raises(ValueError):
        ExperimentSpec.from_json(canonical_json(document))


@pytest.mark.parametrize(
    "field", ["schema_version", "request", "recipe", "config_hash"]
)
def test_each_spec_field_is_required(field: str) -> None:
    document = json.loads(admit_research_request(**complete_inputs()).to_json())
    del document[field]
    with pytest.raises(ValidationError):
        ExperimentSpec(**document)


@pytest.mark.parametrize("digest", ["0" * 64, "A" * 64, "b" * 63, "g" * 64, None])
def test_direct_hash_mismatch_and_invalid_digest_are_pydantic_errors(
    digest: Any,
) -> None:
    document = json.loads(admit_research_request(**complete_inputs()).to_json())
    document["config_hash"] = digest
    with pytest.raises(ValidationError):
        ExperimentSpec(**document)


@pytest.mark.parametrize("mutation", ["copy", "construct"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "forged"),
        ("config_hash", "0" * 64),
        ("recipe", {"oos_boundary": "SEALED"}),
    ],
)
def test_unsafe_spec_instance_cannot_bypass_model_revalidation(
    mutation: str, field: str, value: Any
) -> None:
    spec = admit_research_request(**complete_inputs())
    forged = (
        spec.model_copy(update={field: value})
        if mutation == "copy"
        else ExperimentSpec.model_construct(
            **{**spec.model_dump(mode="python"), field: value}
        )
    )
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(forged)


def test_models_are_frozen_and_identifier_property_is_read_only() -> None:
    spec = admit_research_request(**complete_inputs())
    for instance, field, value in (
        (spec.request, "symbol", "QQQ"),
        (spec, "config_hash", "0" * 64),
        (spec, "experiment_id", "new-id"),
    ):
        with pytest.raises((ValidationError, AttributeError)):
            setattr(instance, field, value)
    assert spec.experiment_id == spec.request.experiment_id
    document = json.loads(spec.to_json())
    document["experiment_id"] = spec.experiment_id
    with pytest.raises(ValidationError):
        ExperimentSpec(**document)


def test_direct_valid_spec_copies_frozen_payload_and_request_values() -> None:
    spec = admit_research_request(**complete_inputs())
    copied = ExperimentSpec(
        schema_version="experiment-spec-v1",
        request=spec.request,
        recipe=spec.recipe,
        config_hash=spec.config_hash,
    )
    assert copied.to_json() == spec.to_json()
    assert copied.recipe is not spec.recipe
    assert copied.request is not spec.request
