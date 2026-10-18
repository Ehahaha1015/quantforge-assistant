from __future__ import annotations

import dataclasses
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from quantforge.data import ParquetDataCatalog
from quantforge.factors import MomentumFactorConfig, compute_momentum_factor
from quantforge.models import (
    PREDICTION_COLUMNS,
    PREDICTION_SCHEMA,
    FittedLinearModel,
    LinearModelFitConfig,
    ModelError,
    fit_linear_model,
    predict_linear_model,
)

START = datetime(2025, 1, 2, tzinfo=UTC)
HAND_OPENS = [10.0, 10.0, 20.0, 30.0]
HAND_CLOSES = [10.0, 20.0, 30.0, 30.0]
HAND_FACTOR = [0.0, 1.0, 0.5, 0.0]
# Five bars so the feature row at the cutoff still HAS a label (from bar 4)
# that the causal rule must refuse, rather than merely missing a label.
BOUNDARY_OPENS = [10.0, 10.0, 20.0, 30.0, 30.0]
BOUNDARY_CLOSES = [10.0, 20.0, 30.0, 30.0, 45.0]
BOUNDARY_FACTOR = [0.0, 1.0, 0.5, 0.25, 0.75]


def make_bars(opens: list[float], closes: list[float], *, symbol: str = "XYZ") -> pl.DataFrame:
    """Canonical daily bars with high = max(open, close) and low = min(...)."""
    timestamps = [START + timedelta(days=index) for index in range(len(closes))]
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(closes),
            "timestamp": timestamps,
            "open": opens,
            "high": [max(open_, close) for open_, close in zip(opens, closes, strict=True)],
            "low": [min(open_, close) for open_, close in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        schema={
            "symbol": pl.String,
            "timestamp": pl.Datetime("us", "UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
        },
    )


def make_factor(
    bars: pl.DataFrame, values: list[float | None], *, symbol: str | None = None
) -> pl.DataFrame:
    base = bars.select("symbol", "timestamp")
    if symbol is not None:
        base = base.with_columns(pl.lit(symbol).alias("symbol"))
    return base.with_columns(
        pl.Series("value", values, dtype=pl.Float64)
    ).select("symbol", "timestamp", "value")


def hand_bars() -> pl.DataFrame:
    return make_bars(HAND_OPENS, HAND_CLOSES)


def ols(features: list[float], targets: list[float]) -> tuple[float, float]:
    """Independent OLS oracle with intercept, computed in the test."""
    rows = len(features)
    feature_mean = math.fsum(features) / rows
    target_mean = math.fsum(targets) / rows
    variance = math.fsum((value - feature_mean) ** 2 for value in features)
    covariance = math.fsum(
        (feature - feature_mean) * (target - target_mean)
        for feature, target in zip(features, targets, strict=True)
    )
    coefficient = covariance / variance
    return (target_mean - coefficient * feature_mean, coefficient)


def next_bar_intraday_returns(opens: list[float], closes: list[float]) -> list[float | None]:
    """The contract's target: close[t+1] / open[t+1] - 1."""
    targets: list[float | None] = []
    for index in range(len(closes)):
        if index + 1 < len(closes):
            targets.append(closes[index + 1] / opens[index + 1] - 1.0)
        else:
            targets.append(None)
    return targets


def cutoff(index: int) -> LinearModelFitConfig:
    return LinearModelFitConfig(train_end_timestamp=START + timedelta(days=index))


def fit_hand(factor_values: list[float | None] = HAND_FACTOR, index: int = 3) -> FittedLinearModel:
    bars = hand_bars()
    return fit_linear_model(bars, make_factor(bars, factor_values), cutoff(index))


def test_fit_config_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        LinearModelFitConfig(train_end_timestamp=START.replace(tzinfo=None))
    with pytest.raises(TypeError, match="datetime"):
        LinearModelFitConfig(train_end_timestamp="2025-01-02")  # type: ignore[arg-type]


def test_fit_config_normalizes_to_utc_and_is_frozen() -> None:
    local = datetime(2025, 1, 2, 8, 0, tzinfo=timezone_offset(8))
    config = LinearModelFitConfig(train_end_timestamp=local)
    assert config.train_end_timestamp == START
    assert config.train_end_timestamp.tzinfo is UTC
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.train_end_timestamp = START  # type: ignore[misc]


def timezone_offset(hours: int):
    from datetime import timezone

    return timezone(timedelta(hours=hours))


def test_train_end_must_exist_in_canonical_bars() -> None:
    bars = hand_bars()
    factor = make_factor(bars, HAND_FACTOR)
    with pytest.raises(ModelError, match="existing canonical bar timestamp"):
        fit_linear_model(
            bars,
            factor,
            LinearModelFitConfig(train_end_timestamp=START + timedelta(days=9)),
        )


def test_factor_column_contract_is_enforced() -> None:
    bars = hand_bars()
    missing = make_factor(bars, HAND_FACTOR).drop("value")
    with pytest.raises(ModelError, match="columns"):
        fit_linear_model(bars, missing, cutoff(3))
    extra = make_factor(bars, HAND_FACTOR).with_columns(pl.lit(1.0).alias("extra"))
    with pytest.raises(ModelError, match="columns"):
        fit_linear_model(bars, extra, cutoff(3))
    wrong_dtype = make_factor(bars, HAND_FACTOR).with_columns(
        pl.col("value").cast(pl.String)
    )
    with pytest.raises(ModelError, match="schema"):
        fit_linear_model(bars, wrong_dtype, cutoff(3))
    with pytest.raises(TypeError, match="Polars DataFrame"):
        fit_linear_model(bars, [0.0, 1.0], cutoff(3))  # type: ignore[arg-type]


def test_duplicate_factor_keys_are_rejected() -> None:
    bars = hand_bars()
    duplicated = pl.concat([make_factor(bars, HAND_FACTOR), make_factor(bars, HAND_FACTOR).head(1)])
    with pytest.raises(ModelError, match="duplicate"):
        fit_linear_model(bars, duplicated, cutoff(3))


def test_missing_extra_and_misaligned_factor_keys_are_rejected() -> None:
    bars = hand_bars()
    missing = make_factor(bars, HAND_FACTOR).head(3)
    with pytest.raises(ModelError, match="one row per canonical bar"):
        fit_linear_model(bars, missing, cutoff(3))
    extra_row = pl.concat(
        [
            make_factor(bars, HAND_FACTOR),
            make_factor(bars, HAND_FACTOR).tail(1).with_columns(
                pl.lit(START + timedelta(days=9)).cast(pl.Datetime("us", "UTC")).alias("timestamp")
            ),
        ]
    )
    with pytest.raises(ModelError, match="one row per canonical bar"):
        fit_linear_model(bars, extra_row, cutoff(3))
    misaligned = make_factor(bars, HAND_FACTOR).with_columns(
        pl.when(pl.int_range(pl.len()) == 3)
        .then(pl.lit(START + timedelta(days=9)).cast(pl.Datetime("us", "UTC")))
        .otherwise(pl.col("timestamp"))
        .alias("timestamp")
    )
    with pytest.raises(ModelError, match="keys must match"):
        fit_linear_model(bars, misaligned, cutoff(3))


def test_factor_symbol_must_match_and_be_single() -> None:
    bars = hand_bars()
    other = make_factor(bars, HAND_FACTOR, symbol="OTHER")
    with pytest.raises(ModelError, match="keys must match"):
        fit_linear_model(bars, other, cutoff(3))
    mixed = pl.concat([make_factor(bars, HAND_FACTOR), make_factor(bars, HAND_FACTOR, symbol="OTHER")])
    with pytest.raises(ModelError, match="exactly one symbol"):
        fit_linear_model(bars, mixed, cutoff(3))


def test_factor_row_order_does_not_change_the_fit() -> None:
    bars = hand_bars()
    factor = make_factor(bars, HAND_FACTOR)
    shuffled = pl.concat([factor[2:], factor[:2]])
    assert fit_linear_model(bars, factor, cutoff(3)) == fit_linear_model(
        bars, shuffled, cutoff(3)
    )


def test_hand_calculated_ols_intercept_coefficient_and_predictions() -> None:
    bars = hand_bars()
    factor = make_factor(bars, HAND_FACTOR)
    model = fit_linear_model(bars, factor, cutoff(3))
    # Eligible samples: x = [0, 1, 0.5], y = [1.0, 0.5, 0.0].
    assert model.training_rows == 3
    assert model.coefficient == pytest.approx(-0.5)
    assert model.intercept == pytest.approx(0.75)
    assert model.train_end_timestamp == START + timedelta(days=3)
    assert ols([0.0, 1.0, 0.5], [1.0, 0.5, 0.0]) == pytest.approx(
        (model.intercept, model.coefficient)
    )
    predictions = predict_linear_model(factor, model)["prediction"].to_list()
    assert predictions == pytest.approx([0.75, 0.25, 0.5, 0.75])


def test_positive_and_negative_coefficient_cases() -> None:
    negative = fit_hand(HAND_FACTOR)
    positive = fit_hand([0.0, -1.0, -0.5, 0.0])
    assert negative.coefficient == pytest.approx(-0.5)
    assert positive.coefficient == pytest.approx(0.5)
    assert positive.intercept == pytest.approx(0.75)


def test_zero_coefficient_with_non_zero_feature_variance() -> None:
    model = fit_hand([0.0, 1.0, 0.0, 0.0])
    assert model.coefficient == pytest.approx(0.0)
    assert model.intercept == pytest.approx(0.5)
    assert model.training_rows == 3
    # Feature variance is non-zero, so this is a genuine zero slope.
    features = [0.0, 1.0, 0.0]
    mean = math.fsum(features) / len(features)
    assert math.fsum((value - mean) ** 2 for value in features) > 0.0


def test_zero_feature_variance_is_rejected() -> None:
    with pytest.raises(ModelError, match="variance is zero"):
        fit_hand([1.0, 1.0, 1.0, 1.0])


def test_at_least_two_training_rows_are_required() -> None:
    bars = make_bars([10.0, 10.0], [10.0, 20.0])
    factor = make_factor(bars, [0.5, 0.5])
    with pytest.raises(ModelError, match="at least two eligible training rows"):
        fit_linear_model(bars, factor, cutoff(1))


def test_warmup_null_factors_are_excluded_from_training() -> None:
    bars = hand_bars()
    factor = make_factor(bars, [None, 0.0, 1.0, 0.5])
    model = fit_linear_model(bars, factor, cutoff(3))
    # If nulls were zero-filled, three rows would train; correctly only two do.
    assert model.training_rows == 2
    assert (model.intercept, model.coefficient) == pytest.approx(ols([0.0, 1.0], [0.5, 0.0]))
    predictions = predict_linear_model(factor, model)["prediction"].to_list()
    assert predictions[0] is None
    assert predictions[1:] == pytest.approx(
        [model.intercept + model.coefficient * value for value in (0.0, 1.0, 0.5)]
    )


def test_feature_at_train_end_has_no_future_label() -> None:
    bars = make_bars(BOUNDARY_OPENS, BOUNDARY_CLOSES)
    factor = make_factor(bars, BOUNDARY_FACTOR)
    model = fit_linear_model(bars, factor, cutoff(3))
    # Bar 3 is the cutoff and bar 4 exists, so feature[3] HAS a label in the
    # frame; the causal rule alone must exclude it (three eligible rows).
    assert model.training_rows == 3
    assert (model.intercept, model.coefficient) == pytest.approx(
        ols([0.0, 1.0, 0.5], [1.0, 0.5, 0.0])
    )
    mutated_cutoff_feature = make_factor(bars, [0.0, 1.0, 0.5, 99.0, 0.75])
    assert fit_linear_model(bars, mutated_cutoff_feature, cutoff(3)) == model


def test_first_row_after_train_end_is_excluded() -> None:
    bars = make_bars(BOUNDARY_OPENS, BOUNDARY_CLOSES)
    factor = make_factor(bars, BOUNDARY_FACTOR)
    model = fit_linear_model(bars, factor, cutoff(3))
    mutated = make_factor(bars, [0.0, 1.0, 0.5, 0.25, 99.0])
    assert fit_linear_model(bars, mutated, cutoff(3)) == model


def test_feature_immediately_before_train_end_is_included() -> None:
    bars = make_bars(BOUNDARY_OPENS, BOUNDARY_CLOSES)
    factor = make_factor(bars, BOUNDARY_FACTOR)
    model = fit_linear_model(bars, factor, cutoff(3))
    # Feature 2 (one row before the cutoff) trains on target[2], whose label
    # bar is exactly the cutoff timestamp, so changing it must change the fit.
    mutated = make_factor(bars, [0.0, 1.0, 2.0, 0.25, 0.75])
    assert fit_linear_model(bars, mutated, cutoff(3)) != model


def test_label_at_train_end_is_included() -> None:
    bars = hand_bars()
    factor = make_factor(bars, HAND_FACTOR)
    baseline = fit_linear_model(bars, factor, cutoff(3))
    # Bar 3 is the cutoff, so target[2] must contribute to the fit.
    changed = make_bars(HAND_OPENS, [10.0, 20.0, 30.0, 60.0])
    assert fit_linear_model(changed, factor, cutoff(3)) != baseline


def test_exact_target_definition_close_next_over_open_next() -> None:
    opens = [10.0, 5.0, 8.0, 20.0]
    closes = [10.0, 10.0, 16.0, 25.0]
    bars = make_bars(opens, closes)
    factor = make_factor(bars, [0.0, 1.0, 2.0, 0.0])
    model = fit_linear_model(bars, factor, cutoff(3))
    correct_targets = [1.0, 1.0, 0.25]
    close_to_close = [0.0, 0.6, 0.5625]
    open_to_close = [-0.5, -0.2, 0.25]
    assert (model.intercept, model.coefficient) == pytest.approx(
        ols([0.0, 1.0, 2.0], correct_targets)
    )
    assert model.coefficient != pytest.approx(
        ols([0.0, 1.0, 2.0], close_to_close)[1]
    )
    assert model.coefficient != pytest.approx(ols([0.0, 1.0, 2.0], open_to_close)[1])
    assert next_bar_intraday_returns(opens, closes) == correct_targets + [None]


@pytest.mark.parametrize("variant", ["mutate", "append", "remove", "replace"])
def test_future_bars_strictly_after_train_end_cannot_change_the_fit(variant: str) -> None:
    bars = make_bars(BOUNDARY_OPENS, BOUNDARY_CLOSES)
    factor = make_factor(bars, BOUNDARY_FACTOR)
    baseline = fit_linear_model(bars, factor, cutoff(3))
    if variant == "mutate":
        changed_bars = make_bars(BOUNDARY_OPENS, [*BOUNDARY_CLOSES[:4], 90.0])
        changed_factor = factor
    elif variant == "append":
        changed_bars = make_bars([*BOUNDARY_OPENS, 30.0], [*BOUNDARY_CLOSES, 40.0])
        changed_factor = make_factor(changed_bars, [*BOUNDARY_FACTOR, 0.5])
    elif variant == "remove":
        changed_bars = make_bars(BOUNDARY_OPENS[:4], BOUNDARY_CLOSES[:4])
        changed_factor = make_factor(changed_bars, BOUNDARY_FACTOR[:4])
    else:
        changed_bars = make_bars([*BOUNDARY_OPENS[:4], 99.0], [*BOUNDARY_CLOSES[:4], 99.0])
        changed_factor = factor
    assert fit_linear_model(changed_bars, changed_factor, cutoff(3)) == baseline


def test_changing_the_cutoff_bar_changes_the_fit() -> None:
    bars = make_bars(BOUNDARY_OPENS, BOUNDARY_CLOSES)
    factor = make_factor(bars, BOUNDARY_FACTOR)
    baseline = fit_linear_model(bars, factor, cutoff(3))
    changed = make_bars(BOUNDARY_OPENS, [10.0, 20.0, 30.0, 60.0, 45.0])
    assert fit_linear_model(changed, factor, cutoff(3)) != baseline


def test_future_bars_and_factor_rows_beyond_the_cutoff_are_ignored() -> None:
    opens = [*HAND_OPENS, 30.0, 30.0]
    closes = [*HAND_CLOSES, 45.0, 15.0]
    bars = make_bars(opens, closes)
    factor = make_factor(bars, [*HAND_FACTOR, 0.25, -0.5])
    baseline = fit_linear_model(bars, factor, cutoff(3))
    for changed_closes in ([*closes[:4], 45.0, 99.0], [*closes[:4], 99.0, 15.0]):
        changed_bars = make_bars(opens, changed_closes)
        assert fit_linear_model(changed_bars, factor, cutoff(3)) == baseline
    assert fit_linear_model(bars, make_factor(bars, [*HAND_FACTOR, 9.0, -9.0]), cutoff(3)) == baseline


def test_future_factor_rows_cannot_change_earlier_predictions() -> None:
    bars = hand_bars()
    factor = make_factor(bars, HAND_FACTOR)
    model = fit_linear_model(bars, factor, cutoff(3))
    baseline = predict_linear_model(factor, model)
    mutated = predict_linear_model(make_factor(bars, [0.0, 1.0, 0.5, 50.0]), model)
    assert mutated["prediction"].to_list()[:3] == baseline["prediction"].to_list()[:3]
    shortened_factor = make_factor(bars.head(3), HAND_FACTOR[:3])
    shortened = predict_linear_model(shortened_factor, model)
    assert shortened["prediction"].to_list() == baseline["prediction"].to_list()[:3]
    replaced = predict_linear_model(make_factor(bars, [0.0, 1.0, 0.5, -7.0]), model)
    assert replaced["prediction"].to_list()[:3] == baseline["prediction"].to_list()[:3]


def test_future_factor_append_cannot_change_earlier_predictions() -> None:
    bars = hand_bars()
    factor = make_factor(bars, HAND_FACTOR)
    model = fit_linear_model(bars, factor, cutoff(3))
    extended_bars = make_bars([*HAND_OPENS, 30.0], [*HAND_CLOSES, 35.0])
    extended_factor = make_factor(extended_bars, [*HAND_FACTOR, 4.0])
    baseline = predict_linear_model(factor, model)["prediction"].to_list()
    extended = predict_linear_model(extended_factor, model)["prediction"].to_list()
    assert extended[: len(baseline)] == baseline
    assert extended[-1] == pytest.approx(model.intercept + model.coefficient * 4.0)


def test_prediction_output_shape_alignment_and_ordering() -> None:
    bars = hand_bars()
    factor = make_factor(bars, HAND_FACTOR)
    model = fit_linear_model(bars, factor, cutoff(3))
    predictions = predict_linear_model(factor, model)
    assert predictions.columns == list(PREDICTION_COLUMNS)
    assert dict(predictions.schema) == PREDICTION_SCHEMA
    assert predictions.height == factor.height
    assert predictions["symbol"].equals(factor["symbol"])
    assert predictions["timestamp"].cast(pl.Int64).to_list() == factor["timestamp"].cast(
        pl.Int64
    ).to_list()
    shuffled = pl.concat([factor[1:], factor[:1]])
    assert predict_linear_model(shuffled, model).equals(predictions)


def test_prediction_keeps_warmup_nulls_and_continuous_values() -> None:
    bars = hand_bars()
    factor = make_factor(bars, [None, -4.0, 0.0, 3.0])
    model = FittedLinearModel(
        intercept=0.5,
        coefficient=0.25,
        train_end_timestamp=START,
        training_rows=2,
    )
    values = predict_linear_model(factor, model)["prediction"].to_list()
    assert values[0] is None
    assert values[1:] == pytest.approx([-0.5, 0.5, 1.25])
    assert values[1] < 0.0
    assert values[2] > 0.0
    assert values[3] > 0.0


def test_prediction_argument_validation() -> None:
    bars = hand_bars()
    factor = make_factor(bars, HAND_FACTOR)
    model = FittedLinearModel(
        intercept=0.0, coefficient=1.0, train_end_timestamp=START, training_rows=2
    )
    with pytest.raises(TypeError, match="FittedLinearModel"):
        predict_linear_model(factor, {"intercept": 0.0})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Polars DataFrame"):
        predict_linear_model([1.0], model)  # type: ignore[arg-type]
    duplicated = pl.concat([factor, factor.head(1)])
    with pytest.raises(ModelError, match="duplicate"):
        predict_linear_model(duplicated, model)
    mixed = pl.concat([factor, make_factor(bars, HAND_FACTOR, symbol="OTHER")])
    with pytest.raises(ModelError, match="exactly one symbol"):
        predict_linear_model(mixed, model)


def test_non_finite_target_is_rejected() -> None:
    opens = [2.0, 2.0, 1e-300, 1e308]
    closes = [2.0, 1e308, 1e308, 1e308]
    bars = make_bars(opens, closes)
    factor = make_factor(bars, [0.0, 0.5, 1.0, 0.0])
    with pytest.raises(ModelError, match="training data must be finite"):
        fit_linear_model(bars, factor, cutoff(3))


def test_non_finite_derived_coefficient_is_rejected() -> None:
    opens = [2.0, 2.0, 1e308, 1e308]
    closes = [2.0, 1e308, 1e308, 1e308]
    bars = make_bars(opens, closes)
    factor = make_factor(bars, [5e307, 0.0, 0.0, 0.0])
    with pytest.raises(ModelError, match="coefficients must be finite"):
        fit_linear_model(bars, factor, cutoff(3))


def test_non_finite_prediction_is_rejected() -> None:
    bars = hand_bars()
    factor = make_factor(bars, [1e10, 0.0, 0.0, 0.0])
    model = FittedLinearModel(
        intercept=0.0,
        coefficient=1e300,
        train_end_timestamp=START,
        training_rows=2,
    )
    with pytest.raises(ModelError, match="prediction is not finite"):
        predict_linear_model(factor, model)


def test_fitted_model_serialization_round_trip_is_deterministic() -> None:
    model = fit_hand()
    first = model.model_dump_json()
    second = model.model_dump_json()
    assert first == second
    assert FittedLinearModel.from_json(first) == model
    assert FittedLinearModel.model_validate_json(first) == model


@pytest.mark.parametrize(
    "payload",
    [
        {"intercept": float("nan"), "coefficient": 1.0, "training_rows": 2},
        {"intercept": 0.0, "coefficient": float("inf"), "training_rows": 2},
        {"intercept": 0.0, "coefficient": 1.0, "training_rows": 1},
        {"intercept": 0.0, "coefficient": 1.0, "training_rows": True},
    ],
)
def test_fitted_model_rejects_invalid_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FittedLinearModel(train_end_timestamp=START, **payload)  # type: ignore[arg-type]


def test_fitted_model_rejects_naive_and_extra_fields() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        FittedLinearModel(
            intercept=0.0,
            coefficient=1.0,
            train_end_timestamp=START.replace(tzinfo=None),
            training_rows=2,
        )
    with pytest.raises(ValidationError):
        FittedLinearModel(
            intercept=0.0,
            coefficient=1.0,
            train_end_timestamp=START,
            training_rows=2,
            model_registry_id="nope",
        )


def test_fit_and_prediction_are_deterministic_and_do_not_mutate_inputs() -> None:
    bars = hand_bars()
    factor = make_factor(bars, HAND_FACTOR)
    bars_snapshot = bars.clone()
    factor_snapshot = factor.clone()
    first = fit_linear_model(bars, factor, cutoff(3))
    second = fit_linear_model(bars, factor, cutoff(3))
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert predict_linear_model(factor, first).equals(predict_linear_model(factor, first))
    assert bars.equals(bars_snapshot)
    assert factor.equals(factor_snapshot)


def test_existing_factor_to_model_integration_is_deterministic() -> None:
    closes = [100.0, 110.0, 121.0, 108.9, 130.0, 90.0, 132.0, 140.0, 120.0, 150.0]
    opens = [c - 1.0 for c in closes]
    bars = make_bars(opens, closes, symbol="AAPL")
    factor = compute_momentum_factor(bars, MomentumFactorConfig(lookback=2))
    config = cutoff(5)
    model = fit_linear_model(bars, factor, config)
    # Samples: momentum non-null (t >= 2) and label bar t+1 <= cutoff (t <= 4).
    assert model.training_rows == 3
    factor_values = factor["value"].to_list()
    targets = next_bar_intraday_returns(opens, closes)
    expected_x = [factor_values[index] for index in (2, 3, 4)]
    expected_y = [targets[index] for index in (2, 3, 4)]
    assert (model.intercept, model.coefficient) == pytest.approx(
        ols(expected_x, expected_y)  # type: ignore[arg-type]
    )
    detached = fit_linear_model(bars, factor, config)
    assert detached == model
    predictions = predict_linear_model(factor, model)
    assert predictions["prediction"].to_list()[:2] == [None, None]
    assert predictions.height == bars.height
    assert predict_linear_model(factor, model).equals(predictions)
    rerun = compute_momentum_factor(bars.clone(), MomentumFactorConfig(lookback=2))
    assert fit_linear_model(bars, rerun, config) == model


def test_public_surface_and_scope_are_minimal() -> None:
    from quantforge import models

    assert set(models.__all__) == {
        "FACTOR_COLUMNS",
        "FACTOR_SCHEMA",
        "PREDICTION_COLUMNS",
        "PREDICTION_SCHEMA",
        "FittedLinearModel",
        "LinearModelFitConfig",
        "ModelError",
        "fit_linear_model",
        "predict_linear_model",
    }
    for forbidden in (
        "ModelRegistry",
        "Estimator",
        "train",
        "pipeline",
        "optimize",
        "search",
    ):
        assert not hasattr(models, forbidden)
    from quantforge.models import linear

    source = Path(linear.__file__).read_text(encoding="utf-8")
    forbidden_import = re.compile(
        r"^\s*(from|import)\s+.*(strategies|backtest|validation|experiments|"
        r"portfolio|execution|risk|autolab)",
        re.MULTILINE,
    )
    assert forbidden_import.search(source) is None


def test_catalog_read_path_still_feeds_the_model(tmp_path: Path) -> None:
    bars = hand_bars()
    catalog = ParquetDataCatalog(tmp_path / "catalog")
    last_microseconds = int(bars["timestamp"].cast(pl.Int64).max())
    as_of = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=last_microseconds, days=1)
    snapshot = catalog.write(
        "bars.XYZ-1d", bars, source="model-test", schema_version="1", as_of=as_of
    )
    read_back = catalog.read(snapshot.dataset_id)
    factor = make_factor(bars, HAND_FACTOR)
    assert fit_linear_model(read_back, factor, cutoff(3)) == fit_linear_model(
        bars, factor, cutoff(3)
    )
