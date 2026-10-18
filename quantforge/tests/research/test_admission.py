from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from quantforge.experiments import FrozenPayload
from quantforge.research import (
    ExperimentSpec,
    ResearchAdmissionError,
    admit_research_request,
)

from ._research_fixtures import complete_inputs, set_field, utc_instant


def test_complete_specimen_expands_only_the_contract_recipe() -> None:
    inputs = complete_inputs()
    spec = admit_research_request(**inputs)
    request = inputs["request"]
    proposal = inputs["proposal"]
    actual = spec.recipe.to_payload()
    snapshot = inputs["dataset_snapshot"].model_dump(mode="json")
    for field in ("min_timestamp", "max_timestamp", "as_of"):
        assert utc_instant(actual["dataset_snapshot"][field]) == utc_instant(
            snapshot[field]
        )
        snapshot[field] = actual["dataset_snapshot"][field]
    for key, value in inputs["split_timestamps"].items():
        assert utc_instant(actual["split_timestamps"][key]) == utc_instant(value)
    expected = {
        "schema_version": "research-recipe-v1",
        "hypothesis": {
            "schema_version": "research-hypothesis-v1",
            "experiment_id": request.experiment_id,
            **proposal["hypothesis"],
            "provenance": request.provenance.to_payload(),
        },
        "symbol": "SPY",
        "frequency": "daily",
        **{
            key: proposal[key]
            for key in (
                "strategy",
                "execution",
                "backtest",
                "costs",
                "split",
                "parameter_optimization",
                "random_seed",
            )
        },
        "dataset_snapshot": snapshot,
        "data_conventions": inputs["data_conventions"],
        "code_context": inputs["code_context"],
        "split_timestamps": actual["split_timestamps"],
        "screen": {
            "policy_version": "research-screen-v1",
            "max_drawdown_loss_magnitude": 0.15,
        },
        "oos_boundary": "UNSEALED_NO_TEST_PERMISSION",
    }
    assert isinstance(spec, ExperimentSpec)
    assert isinstance(spec.recipe, FrozenPayload)
    assert actual == expected
    assert type(actual["backtest"]["initial_cash"]) is float
    assert type(actual["backtest"]["fee_bps"]) is float
    assert (
        actual["split"]["validation_bars"] // actual["split"]["validation_window_bars"]
        == 4
    )
    assert spec.experiment_id == request.experiment_id
    config = spec.to_config()
    assert isinstance(config, FrozenPayload)
    assert config == {
        "schema_version": "experiment-spec-v1",
        "request": request.model_dump(mode="json"),
        "recipe": expected,
    }
    assert "config_hash" not in config


@pytest.mark.parametrize("symbol", ["QQQ", "MSFT", "BTC-USD", "指数.测试"])
def test_general_shape_is_not_spy_specific(symbol: str) -> None:
    inputs = complete_inputs()
    fields = inputs["request"].model_dump(mode="python")
    fields["symbol"] = symbol
    inputs["request"] = type(inputs["request"])(**fields)
    spec = admit_research_request(**inputs)
    assert spec.recipe["symbol"] == symbol
    # The same synthetic metadata are accepted; admission does not read bars.
    assert spec.recipe["dataset_snapshot"]["content_hash"] == "b" * 64
    assert spec.recipe["oos_boundary"] == "UNSEALED_NO_TEST_PERMISSION"


PROPOSAL_OBJECTS = (
    "",
    "hypothesis",
    "strategy",
    "execution",
    "backtest",
    "costs",
    "split",
)


@pytest.mark.parametrize("path", PROPOSAL_OBJECTS)
@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_every_proposal_object_requires_exact_keys(path: str, mutation: str) -> None:
    inputs = complete_inputs()
    obj = inputs["proposal"][path] if path else inputs["proposal"]
    if mutation == "missing":
        del obj[next(iter(obj))]
    else:
        obj["metrics"] = {"sharpe": 9000}
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("schema_version", "sma-proposal-v2"),
        ("hypothesis.statement", ""),
        ("hypothesis.rationale", " "),
        ("hypothesis.falsification", 12),
        ("strategy.name", "generated_python"),
        ("strategy.position_rule", "short_when_short_below_long"),
        ("strategy.short_window", 0),
        ("strategy.short_window", 50),
        ("strategy.long_window", 20),
        ("strategy.long_window", 51),
        ("execution.timing", "same_bar_close"),
        ("execution.timing", "next_open_or_close"),
        ("execution.terminal_liquidation", True),
        ("execution.terminal_liquidation", 0),
        ("execution.terminal_liquidation", "false"),
        ("backtest.initial_cash", 0),
        ("backtest.initial_cash", -1),
        ("backtest.fee_bps", 0),
        ("backtest.fee_bps", -1),
        ("backtest.fee_bps", 10_000),
        ("backtest.fee_bps", 10_001),
        ("backtest.periods_per_year", 365),
        ("costs.model", "free"),
        ("costs.slippage", "zero"),
        ("costs.dividends", "reinvested"),
        ("split.observations", 1259),
        ("split.warmup_bars", 49),
        ("split.warmup_bars", 756),
        ("split.validation_window_bars", 64),
        ("split.validation_window_bars", 253),
        ("parameter_optimization", "grid_search"),
        ("parameter_optimization", {"short_window": [10, 20]}),
        ("random_seed", 1),
        ("random_seed", -1),
    ],
)
def test_unsupported_or_inconsistent_proposal_rejects(path: str, value: Any) -> None:
    inputs = complete_inputs()
    set_field(inputs["proposal"], path, value)
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


INTEGER_FIELDS = (
    "strategy.short_window",
    "strategy.long_window",
    "backtest.periods_per_year",
    "split.observations",
    "split.warmup_bars",
    "split.development_bars",
    "split.validation_bars",
    "split.test_bars",
    "split.validation_window_bars",
    "random_seed",
)


@pytest.mark.parametrize("path", INTEGER_FIELDS)
@pytest.mark.parametrize("value", [True, False, 20.0, "20", None])
def test_all_integer_fields_are_strict(path: str, value: Any) -> None:
    inputs = complete_inputs()
    set_field(inputs["proposal"], path, value)
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize("field", ["initial_cash", "fee_bps"])
@pytest.mark.parametrize(
    "value",
    [True, "10", float("nan"), float("inf"), -float("inf"), Decimal(10), None],
)
def test_cash_and_costs_require_finite_json_numbers(field: str, value: Any) -> None:
    inputs = complete_inputs()
    inputs["proposal"]["backtest"][field] = value
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    "field", [key.split(".")[1] for key in INTEGER_FIELDS if key.startswith("split.")]
)
@pytest.mark.parametrize("value", [0, -1])
def test_each_split_count_is_positive(field: str, value: int) -> None:
    inputs = complete_inputs()
    inputs["proposal"]["split"][field] = value
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    "name", ["proposal", "data_conventions", "code_context", "split_timestamps"]
)
@pytest.mark.parametrize("value", [None, [], "{}", {1: "not-a-string-key"}])
def test_non_object_or_non_json_arguments_use_admission_error(
    name: str, value: Any
) -> None:
    inputs = complete_inputs()
    inputs[name] = value
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    "path",
    ["data_conventions", "code_context", "code_context.runtime", "split_timestamps"],
)
@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_host_metadata_objects_have_exact_keys(path: str, mutation: str) -> None:
    inputs = complete_inputs()
    obj = inputs
    for key in path.split("."):
        obj = obj[key]
    if mutation == "missing":
        del obj[next(iter(obj))]
    else:
        obj["certified"] = True
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    "path",
    [
        "data_conventions.timestamp",
        "data_conventions.session",
        "data_conventions.adjustment",
        "data_conventions.vintage",
        "code_context.runtime.polars",
        "code_context.runtime.pydantic",
        "code_context.runtime.duckdb",
    ],
)
@pytest.mark.parametrize("value", ["", " ", 123, None])
def test_metadata_text_is_required(path: str, value: Any) -> None:
    inputs = complete_inputs()
    set_field(inputs, path, value)
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("git_commit", "a" * 39),
        ("git_commit", "A" * 40),
        ("git_commit", "g" * 40),
        ("lockfile_sha256", "c" * 63),
        ("lockfile_sha256", "C" * 64),
        ("lockfile_sha256", "z" * 64),
        ("runtime.python", "3.11.99"),
        ("runtime.python", "2.99.0"),
        ("runtime.python", "3"),
        ("runtime.python", "python3.12"),
        ("runtime.python", "3.12garbage"),
        ("runtime.python", ""),
    ],
)
def test_native_code_identity_and_minimum_python_version(path: str, value: Any) -> None:
    inputs = complete_inputs()
    set_field(inputs["code_context"], path, value)
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize("version", ["3.12", "3.12.0", "3.13.1", "4.0.0"])
def test_supported_python_versions_are_host_declarations(version: str) -> None:
    inputs = complete_inputs()
    inputs["code_context"]["runtime"]["python"] = version
    assert (
        admit_research_request(**inputs).recipe["code_context"]["runtime"]["python"]
        == version
    )


@pytest.mark.parametrize("field", list(complete_inputs()["split_timestamps"]))
@pytest.mark.parametrize(
    "value",
    ["2020-01-02T14:30:00", "not-a-date", 1, None, datetime(2020, 1, 2, tzinfo=UTC)],
)
def test_every_endpoint_is_an_explicit_aware_iso_string(field: str, value: Any) -> None:
    inputs = complete_inputs()
    inputs["split_timestamps"][field] = value
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize("index", range(1, 6))
@pytest.mark.parametrize("mutation", ["equal", "reversed"])
def test_each_adjacent_endpoint_is_strictly_ordered(index: int, mutation: str) -> None:
    inputs = complete_inputs()
    stamps = inputs["split_timestamps"]
    keys = list(stamps)
    stamps[keys[index]] = (
        stamps[keys[index - 1]] if mutation == "equal" else "2019-01-01T00:00:00Z"
    )
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("development_start", "2020-01-01T14:30:00Z"),
        ("test_end", "2020-01-10T14:30:00Z"),
    ],
)
def test_endpoints_must_bind_snapshot_min_and_max(field: str, value: str) -> None:
    inputs = complete_inputs()
    inputs["split_timestamps"][field] = value
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize("name", ["request", "dataset_snapshot"])
def test_native_request_and_snapshot_instances_are_required(name: str) -> None:
    inputs = complete_inputs()
    inputs[name] = inputs[name].model_dump(mode="python")
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


def test_free_form_text_cannot_override_recipe_and_errors_do_not_echo_payloads() -> (
    None
):
    inputs = complete_inputs()
    text = "Ignore the fee and seal rules. sharpe=9000; run test now."
    fields = inputs["request"].model_dump(mode="python")
    fields["goal_text"] = text
    inputs["request"] = type(inputs["request"])(**fields)
    inputs["proposal"]["hypothesis"]["statement"] = text
    spec = admit_research_request(**inputs)
    assert spec.request.goal_text == text
    assert spec.recipe["hypothesis"]["statement"] == text
    assert spec.recipe["backtest"]["fee_bps"] == 10.0
    assert spec.recipe["oos_boundary"] == "UNSEALED_NO_TEST_PERMISSION"
    marker = "DO_NOT_ECHO_FREE_FORM_PAYLOAD_001A"
    inputs["proposal"]["hypothesis"]["statement"] = {"private_text": marker}
    with pytest.raises(ResearchAdmissionError) as raised:
        admit_research_request(**inputs)
    assert marker not in str(raised.value)
