from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from quantforge.experiments import canonical_config_hash, canonical_json
from quantforge.research import (
    ExperimentSpec,
    ResearchAdmissionError,
    admit_research_request,
)
from quantforge.strategies.models import (
    STRATEGY_ADAPTER,
    parse_strategy,
    required_observations,
    strategy_from_parameters,
)

from ._research_fixtures import complete_inputs

FAMILIES = [
    ("moving_average_crossover", {"short_window": 3, "long_window": 10}),
    ("donchian_breakout", {"entry_window": 10, "exit_window": 3}),
    ("bollinger_reversion", {"window": 10, "k_pct": "200.0"}),
    ("time_series_momentum", {"lookback": 10, "threshold_pct": "0.0"}),
]


def inputs_for(family: str, parameters: dict[str, Any]) -> dict[str, Any]:
    inputs = complete_inputs()
    inputs["proposal"]["schema_version"] = "strategy-proposal-v2"
    inputs["proposal"]["strategy"] = strategy_from_parameters(family, parameters)
    return inputs


@pytest.mark.parametrize(("family", "parameters"), FAMILIES)
def test_v2_admission_strict_discrimination_canonical_replay(
    family: str, parameters: dict[str, Any]
) -> None:
    spec = admit_research_request(**inputs_for(family, parameters))
    assert spec.schema_version == "experiment-spec-v2"
    assert spec.recipe["schema_version"] == "research-recipe-v2"
    assert spec.recipe["strategy"]["name"] == family
    assert spec.recipe["oos_boundary"] == "UNSEALED_NO_TEST_PERMISSION"
    assert spec.recipe["costs"]["slippage"] == "not_modelled"
    assert spec.recipe["backtest"]["fee_bps"] > 0
    assert spec.to_json() == ExperimentSpec.from_json(spec.to_json()).to_json()
    assert spec == admit_research_request(**inputs_for(family, parameters))
    assert spec.config_hash == canonical_config_hash(spec.to_config())
    typed = parse_strategy(spec.recipe["strategy"])
    assert typed.model_dump(mode="json") == spec.recipe["strategy"]
    for field in ("k_pct", "threshold_pct"):
        if field in parameters:
            assert isinstance(getattr(typed, field), Decimal)
            assert spec.recipe["strategy"][field] == parameters[field]


def test_union_schema_has_name_discriminator_and_exact_families() -> None:
    schema = STRATEGY_ADAPTER.json_schema()
    if "$ref" in schema:
        schema = schema["$defs"][schema["$ref"].split("/")[-1]]
    assert schema["discriminator"]["propertyName"] == "name"
    assert set(schema["discriminator"]["mapping"]) == {family for family, _ in FAMILIES}
    assert len(schema["oneOf"]) == 4


@pytest.mark.parametrize(("family", "parameters"), FAMILIES)
@pytest.mark.parametrize(
    "mutation", ["extra", "missing", "wrong_rule", "unknown_name", "other_family_field"]
)
def test_every_family_refuses_ambiguous_strategy_fields(
    family: str, parameters: dict[str, Any], mutation: str
) -> None:
    inputs = inputs_for(family, parameters)
    strategy = inputs["proposal"]["strategy"]
    if mutation == "extra":
        strategy["claimed_sharpe"] = 99
    elif mutation == "missing":
        del strategy[next(iter(parameters))]
    elif mutation == "wrong_rule":
        strategy["position_rule"] = "always_long"
    elif mutation == "unknown_name":
        strategy["name"] = "invented_strategy"
    else:
        strategy["threshold_pct" if family != "time_series_momentum" else "k_pct"] = (
            "2.0"
        )
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(("family", "parameters"), FAMILIES)
@pytest.mark.parametrize("invalid", [True, 2.0, "2", None, 0, -1, 253])
def test_each_integer_parameter_is_strict_and_bounded(
    family: str, parameters: dict[str, Any], invalid: Any
) -> None:
    for field, value in parameters.items():
        if type(value) is int:
            inputs = inputs_for(family, parameters)
            inputs["proposal"]["strategy"][field] = invalid
            with pytest.raises(ResearchAdmissionError):
                admit_research_request(**inputs)


@pytest.mark.parametrize(("family", "parameters"), FAMILIES[2:])
@pytest.mark.parametrize(
    "invalid",
    [
        2,
        2.0,
        True,
        None,
        "NaN",
        "Infinity",
        "2e0",
        "+2.0",
        "-0",
        "-1",
        "00.2",
        " 2.0",
        "2.0 ",
        ".2",
        "2.",
        "0.00001",
        "1000.0001",
    ],
)
def test_percent_interchange_requires_bounded_unsigned_decimal_text(
    family: str, parameters: dict[str, Any], invalid: Any
) -> None:
    inputs = inputs_for(family, parameters)
    field = "k_pct" if family == "bollinger_reversion" else "threshold_pct"
    inputs["proposal"]["strategy"][field] = invalid
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    ("family", "parameters"),
    [
        ("donchian_breakout", {"entry_window": 2, "exit_window": 1}),
        ("bollinger_reversion", {"window": 2, "k_pct": "0.0001"}),
        ("bollinger_reversion", {"window": 252, "k_pct": "1000.0000"}),
        ("time_series_momentum", {"lookback": 2, "threshold_pct": "0.0000"}),
        ("time_series_momentum", {"lookback": 252, "threshold_pct": "100.0000"}),
        ("donchian_breakout", {"entry_window": 252, "exit_window": 251}),
    ],
)
def test_exact_parameter_boundaries_and_family_warmup(
    family: str, parameters: dict[str, Any]
) -> None:
    inputs = inputs_for(family, parameters)
    warmup = required_observations(
        parse_strategy(inputs["proposal"]["strategy"]).to_config()
    )
    inputs["proposal"]["split"]["warmup_bars"] = warmup
    assert admit_research_request(**inputs).recipe["split"]["warmup_bars"] == warmup
    inputs["proposal"]["split"]["warmup_bars"] = warmup - 1
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    ("family", "parameters"),
    [
        ("moving_average_crossover", {"short_window": 2, "long_window": 2}),
        ("donchian_breakout", {"entry_window": 2, "exit_window": 2}),
        ("donchian_breakout", {"entry_window": 1, "exit_window": 1}),
        ("bollinger_reversion", {"window": 1, "k_pct": "2.0"}),
        ("bollinger_reversion", {"window": 2, "k_pct": "0"}),
        ("time_series_momentum", {"lookback": 1, "threshold_pct": "0"}),
        ("time_series_momentum", {"lookback": 2, "threshold_pct": "100.0001"}),
    ],
)
def test_invalid_pairs_minimums_and_percent_bounds(
    family: str, parameters: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        strategy_from_parameters(family, parameters)


def test_legacy_v1_versions_whitelist_unbounded_windows_and_bytes_are_preserved() -> (
    None
):
    spec = admit_research_request(**complete_inputs())
    raw = (Path(__file__).parent / "fixtures" / "sma-v1-spec.json").read_text(
        encoding="utf-8"
    )
    assert raw == spec.to_json() == ExperimentSpec.from_json(raw).to_json()
    assert spec.schema_version == "experiment-spec-v1"
    assert spec.recipe["schema_version"] == "research-recipe-v1"
    inputs = complete_inputs()
    inputs["proposal"]["strategy"].update(short_window=100, long_window=300)
    inputs["proposal"]["split"]["warmup_bars"] = 300
    assert admit_research_request(**inputs).recipe["strategy"]["long_window"] == 300
    inputs["proposal"]["schema_version"] = "strategy-proposal-v2"
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)
    inputs = inputs_for(*FAMILIES[1])
    inputs["proposal"]["schema_version"] = "sma-proposal-v1"
    with pytest.raises(ResearchAdmissionError):
        admit_research_request(**inputs)


@pytest.mark.parametrize(
    ("spec_version", "recipe_version"),
    [
        ("experiment-spec-v1", "research-recipe-v2"),
        ("experiment-spec-v2", "research-recipe-v1"),
        ("experiment-spec-v3", "research-recipe-v2"),
    ],
)
def test_forged_version_pairs_fail_even_with_recomputed_hash(
    spec_version: str, recipe_version: str
) -> None:
    payload = json.loads(admit_research_request(**inputs_for(*FAMILIES[0])).to_json())
    payload["schema_version"] = spec_version
    payload["recipe"]["schema_version"] = recipe_version
    payload["config_hash"] = canonical_config_hash(
        {k: v for k, v in payload.items() if k != "config_hash"}
    )
    with pytest.raises(ResearchAdmissionError):
        ExperimentSpec.from_json(canonical_json(payload))


def test_parameter_route_cannot_override_identity() -> None:
    for fields in ({"name": "donchian_breakout"}, {"position_rule": "always_long"}):
        with pytest.raises(ValueError):
            strategy_from_parameters(
                "moving_average_crossover",
                {"short_window": 2, "long_window": 3, **fields},
            )
    with pytest.raises(ValidationError):
        parse_strategy(
            {
                "name": "bollinger_reversion",
                "window": 3,
                "k_pct": Decimal("200.0"),
                "position_rule": "long_at_or_below_lower_band_flat_at_or_above_mean",
            }
        )
