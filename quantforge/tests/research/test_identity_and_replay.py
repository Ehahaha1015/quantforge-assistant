from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

import pytest

from quantforge.data import DatasetSnapshot
from quantforge.experiments import FrozenPayload, canonical_config_hash, canonical_json
from quantforge.research import (
    ExperimentSpec,
    ResearchAdmissionError,
    ResearchRequest,
    admit_research_request,
)

from ._research_fixtures import complete_inputs, request_fields, set_field


def test_full_body_hash_and_exact_canonical_replay_have_independent_byte_oracles() -> (
    None
):
    spec = admit_research_request(**complete_inputs())
    body = spec.to_config().to_payload()
    canonical_body = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    expected_hash = hashlib.sha256(canonical_body.encode("utf-8")).hexdigest()
    expected_json = json.dumps(
        {**body, "config_hash": expected_hash},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    assert spec.to_config().canonical == canonical_body
    assert spec.config_hash == expected_hash == canonical_config_hash(body)
    assert spec.to_json() == expected_json == canonical_json(json.loads(expected_json))
    restored = ExperimentSpec.from_json(expected_json)
    assert restored == spec
    assert restored.to_json() == expected_json
    assert restored.to_config() == spec.to_config()
    assert restored.experiment_id == spec.experiment_id


MEANINGFUL_CHANGES = [
    ("request.experiment_id", "another-id"),
    ("request.goal_text", "另一个提案: fixed research only."),
    ("request.submitted_at", datetime.fromisoformat("2026-10-08T01:02:03.000001Z")),
    ("request.symbol", "QQQ"),
    ("request.constraints.max_drawdown_loss_magnitude", 0.20),
    ("request.provenance.source", "scripted"),
    ("request.provenance.reference", "another-proposal"),
    ("proposal.hypothesis.statement", "Another proposed statement."),
    ("proposal.hypothesis.rationale", "Another proposed rationale."),
    ("proposal.hypothesis.falsification", "Another proposed falsification."),
    ("proposal.strategy.short_window", 21),
    ("proposal.strategy.long_window", 49),
    ("proposal.backtest.initial_cash", 100_001),
    ("proposal.backtest.fee_bps", 11),
    ("proposal.split.warmup_bars", 51),
    ("proposal.split.validation_window_bars", 84),
    ("dataset_snapshot.dataset_id", "another-native-id"),
    ("dataset_snapshot.source", "another-host-source"),
    ("dataset_snapshot.schema_version", "another-host-schema"),
    ("dataset_snapshot.content_hash", "d" * 64),
    ("dataset_snapshot.relative_path", "another-absent-dataset/bars.parquet"),
    ("dataset_snapshot.as_of", datetime.fromisoformat("2020-01-11T14:30:00Z")),
    ("data_conventions.timestamp", "Another timestamp declaration"),
    ("data_conventions.session", "Another session declaration"),
    ("data_conventions.adjustment", "Another adjustment declaration"),
    ("data_conventions.vintage", "Another vintage declaration"),
    ("code_context.git_commit", "d" * 40),
    ("code_context.lockfile_sha256", "e" * 64),
    ("code_context.runtime.python", "3.13.1"),
    ("code_context.runtime.polars", "1.45.0"),
    ("code_context.runtime.pydantic", "2.13.0"),
    ("code_context.runtime.duckdb", "1.6.0"),
    ("split_timestamps.development_end", "2020-01-04T14:30:00Z"),
    ("split_timestamps.validation_start", "2020-01-05T14:30:00Z"),
    ("split_timestamps.validation_end", "2020-01-07T15:30:00Z"),
    ("split_timestamps.test_start", "2020-01-08T15:30:00Z"),
]


@pytest.mark.parametrize(("path", "value"), MEANINGFUL_CHANGES)
def test_each_supported_meaningful_change_enters_full_recipe_identity(
    path: str, value: Any
) -> None:
    baseline = admit_research_request(**complete_inputs())
    inputs = complete_inputs()
    root, tail = path.split(".", 1)
    if root in ("request", "dataset_snapshot"):
        fields = inputs[root].model_dump(mode="python")
        set_field(fields, tail, value)
        inputs[root] = type(inputs[root])(**fields)
    else:
        set_field(inputs, path, value)
    changed = admit_research_request(**inputs)
    assert changed.config_hash != baseline.config_hash
    assert changed.to_json() != baseline.to_json()
    assert ExperimentSpec.from_json(changed.to_json()).to_json() == changed.to_json()


@pytest.mark.parametrize(
    ("field", "count"),
    [
        ("development_bars", 757),
        ("validation_bars", 189),
        ("test_bars", 251),
    ],
)
def test_coherent_split_count_and_snapshot_changes_change_identity(
    field: str, count: int
) -> None:
    inputs = complete_inputs()
    baseline = admit_research_request(**inputs)
    split = inputs["proposal"]["split"]
    split[field] = count
    split["observations"] = sum(
        split[key] for key in ("development_bars", "validation_bars", "test_bars")
    )
    snapshot = inputs["dataset_snapshot"].model_dump(mode="python")
    snapshot["row_count"] = split["observations"]
    inputs["dataset_snapshot"] = DatasetSnapshot(**snapshot)
    changed = admit_research_request(**inputs)
    assert changed.config_hash != baseline.config_hash
    assert changed.recipe["split"][field] == count
    assert changed.recipe["dataset_snapshot"]["row_count"] == split["observations"]


@pytest.mark.parametrize(
    ("endpoint", "snapshot_field", "value"),
    [
        ("development_start", "min_timestamp", "2020-01-01T14:30:00Z"),
        ("test_end", "max_timestamp", "2020-01-10T14:30:00Z"),
    ],
)
def test_coherent_snapshot_endpoint_changes_change_identity(
    endpoint: str, snapshot_field: str, value: str
) -> None:
    inputs = complete_inputs()
    baseline = admit_research_request(**inputs)
    fields = inputs["dataset_snapshot"].model_dump(mode="python")
    fields[snapshot_field] = datetime.fromisoformat(value)
    inputs["dataset_snapshot"] = DatasetSnapshot(**fields)
    inputs["split_timestamps"][endpoint] = value
    assert admit_research_request(**inputs).config_hash != baseline.config_hash


def _reverse_objects(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _reverse_objects(item) for key, item in reversed(list(value.items()))
        }
    return value


def test_order_timezone_and_numeric_equivalence_normalize_to_identical_bytes() -> None:
    baseline_inputs = complete_inputs()
    fields = baseline_inputs["request"].model_dump(mode="python")
    fields["constraints"]["max_drawdown_loss_magnitude"] = 1.0
    baseline_inputs["request"] = ResearchRequest(**fields)
    baseline = admit_research_request(**baseline_inputs)
    equivalent = complete_inputs()
    offset = timezone(timedelta(hours=8))
    fields["submitted_at"] = fields["submitted_at"].astimezone(offset)
    fields["constraints"]["max_drawdown_loss_magnitude"] = 1
    equivalent["request"] = ResearchRequest(**_reverse_objects(fields))
    snapshot = equivalent["dataset_snapshot"].model_dump(mode="python")
    for key in ("min_timestamp", "max_timestamp", "as_of"):
        snapshot[key] = snapshot[key].astimezone(offset)
    equivalent["dataset_snapshot"] = DatasetSnapshot(**_reverse_objects(snapshot))
    equivalent["proposal"]["backtest"].update(initial_cash=100_000, fee_bps=10)
    equivalent["split_timestamps"] = {
        key: datetime.fromisoformat(value).astimezone(offset).isoformat()
        for key, value in equivalent["split_timestamps"].items()
    }
    actual = admit_research_request(**_reverse_objects(equivalent))
    assert actual.config_hash == baseline.config_hash
    assert actual.to_json() == baseline.to_json()
    assert actual.request.submitted_at.utcoffset() == timedelta(0)


def test_same_identifier_is_stateless_across_valid_and_invalid_calls() -> None:
    inputs = complete_inputs()
    first = admit_research_request(**inputs)
    inputs["proposal"]["strategy"]["short_window"] = 21
    second = admit_research_request(**inputs)
    inputs["proposal"]["backtest"]["fee_bps"] = 0
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)
    repeated = admit_research_request(**complete_inputs())
    assert first.experiment_id == second.experiment_id == repeated.experiment_id
    assert first.config_hash != second.config_hash
    assert first.to_json() == repeated.to_json()
    assert ExperimentSpec.from_json(second.to_json()).to_json() == second.to_json()


@pytest.mark.parametrize("wrapper", ["dict", "mapping", "frozen"])
def test_original_inputs_and_exported_payload_copies_cannot_mutate_admitted_recipe(
    wrapper: str,
) -> None:
    originals = complete_inputs()
    inputs = dict(originals)
    frozen_arguments: list[FrozenPayload] = []
    for key in ("proposal", "data_conventions", "code_context", "split_timestamps"):
        if wrapper == "mapping":
            inputs[key] = MappingProxyType(originals[key])
        elif wrapper == "frozen":
            inputs[key] = FrozenPayload.from_payload(originals[key], name=key)
            frozen_arguments.append(inputs[key])
    spec = admit_research_request(**inputs)
    before = spec.to_json()
    originals["proposal"]["strategy"]["short_window"] = 5
    originals["proposal"]["hypothesis"]["statement"] = "Mutated caller text"
    originals["code_context"]["runtime"]["pydantic"] = "caller-mutated"
    originals["data_conventions"]["vintage"] = "caller-mutated"
    originals["split_timestamps"]["test_start"] = "caller-mutated"
    for argument in frozen_arguments:
        exported = argument.to_payload()
        exported.clear()
        copied_items = dict(argument.items())
        copied_items.clear()
        if "strategy" in argument:
            argument["strategy"]["short_window"] = [1, 2, 3]
        if "runtime" in argument:
            argument["runtime"]["python"] = "caller-mutated"
        with pytest.raises(TypeError):
            argument["new_field"] = "mutation"  # type: ignore[index]
    for exported in (
        spec.recipe.to_payload(),
        dict(spec.recipe),
        dict(spec.recipe.items()),
    ):
        exported["strategy"]["short_window"] = [20, 21]
        exported["strategy"]["short_window"].append(22)
        exported["hypothesis"]["provenance"]["reference"] = "export-mutated"
        exported["code_context"]["runtime"].clear()
    spec.recipe["strategy"]["short_window"] = 1
    spec.recipe["hypothesis"]["provenance"]["reference"] = "export-mutated"
    config = spec.to_config()
    assert config is not spec.to_config()
    config["recipe"]["strategy"]["short_window"] = 1
    config.to_payload()["request"]["constraints"].clear()
    dump = spec.model_dump(mode="json")
    dump["recipe"]["hypothesis"].clear()
    dump["request"]["provenance"].clear()
    assert spec.to_json() == before
    assert ExperimentSpec.from_json(before).to_json() == before


def test_request_frozen_payload_inputs_are_copied_and_public_copies_are_safe() -> None:
    fields = request_fields()
    constraints = FrozenPayload.from_payload(fields["constraints"], name="constraints")
    provenance = FrozenPayload.from_payload(fields["provenance"], name="provenance")
    request = ResearchRequest(
        **{**fields, "constraints": constraints, "provenance": provenance}
    )
    inputs = complete_inputs()
    inputs["request"] = request
    spec = admit_research_request(**inputs)
    before = spec.to_json()
    assert request.constraints is not constraints
    assert request.provenance is not provenance
    assert spec.request is not request
    assert spec.request.constraints is not request.constraints
    assert spec.request.provenance is not request.provenance
    fields["constraints"]["max_drawdown_loss_magnitude"] = 0
    fields["provenance"]["reference"] = "mutated-original"
    for payload in (
        constraints,
        provenance,
        request.constraints,
        request.provenance,
        spec.request.constraints,
        spec.request.provenance,
    ):
        payload.to_payload().clear()
        dict(payload.items()).clear()
    request.model_dump(mode="json")["provenance"].clear()
    assert spec.to_json() == before


@pytest.mark.parametrize("text", ["", "not-json", "[]", "null", "{}", "{", "\ufeff{}"])
def test_interchange_corruption_raises_value_error(text: str) -> None:
    with pytest.raises(ValueError):
        ExperimentSpec.from_json(text)


@pytest.mark.parametrize(
    "mutation",
    [
        "whitespace",
        "newline",
        "order",
        "duplicate_outer",
        "duplicate_nested",
        "NaN",
        "Infinity",
        "-Infinity",
    ],
)
def test_only_canonical_finite_nonduplicate_json_is_replayable(mutation: str) -> None:
    spec = admit_research_request(**complete_inputs())
    text = spec.to_json()
    if mutation == "whitespace":
        text = json.dumps(json.loads(text), indent=2)
    elif mutation == "newline":
        text += "\n"
    elif mutation == "order":
        text = json.dumps(_reverse_objects(json.loads(text)), separators=(",", ":"))
    elif mutation == "duplicate_outer":
        text = '{"config_hash":' + json.dumps(spec.config_hash) + "," + text[1:]
    elif mutation == "duplicate_nested":
        text = text.replace('"fee_bps":10.0', '"fee_bps":10.0,"fee_bps":10.0', 1)
    else:
        text = text.replace('"fee_bps":10.0', '"fee_bps":' + mutation, 1)
    assert text != spec.to_json()
    with pytest.raises(ValueError):
        ExperimentSpec.from_json(text)


@pytest.mark.parametrize("field", ["config_hash", "recipe", "request"])
def test_canonical_interchange_hash_tampering_rejects(field: str) -> None:
    document = json.loads(admit_research_request(**complete_inputs()).to_json())
    if field == "config_hash":
        document[field] = "0" * 64
    elif field == "recipe":
        document[field]["backtest"]["initial_cash"] += 1
    else:
        document[field]["goal_text"] = "Altered proposal text"
    with pytest.raises(ValueError):
        ExperimentSpec.from_json(canonical_json(document))


def test_valid_rewritten_recipe_and_checksum_are_not_cryptographic_authentication() -> (
    None
):
    first = admit_research_request(**complete_inputs())
    body = first.to_config().to_payload()
    body["recipe"]["backtest"]["fee_bps"] = 11.0
    digest = canonical_config_hash(body)
    serialized = canonical_json({**body, "config_hash": digest})
    restored = ExperimentSpec.from_json(serialized)
    assert restored.config_hash == digest != first.config_hash
    assert restored.experiment_id == first.experiment_id
    assert restored.to_json() == serialized
    assert restored.recipe["oos_boundary"] == "UNSEALED_NO_TEST_PERMISSION"
