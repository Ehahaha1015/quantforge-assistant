from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest

from quantforge.experiments import canonical_config_hash, canonical_json
from quantforge.product.policy import freeze_candidate
from quantforge.product.runner import ExperimentRunError
from quantforge.research import ExperimentSpec

from .test_runner import run


def test_partial_reservation_directory_blocks_new_admission(research_host: Any) -> None:
    runner, spec, _ = research_host
    (runner.artifact_root / "run-interrupted").mkdir()
    with pytest.raises(ExperimentRunError, match="incomplete reservation"):
        run(runner, spec)
    assert runner.store.list() == ()


def test_partial_history_cannot_be_ignored_when_freezing(research_host: Any) -> None:
    runner, spec, _ = research_host
    run(runner, spec)
    (runner.artifact_root / "run-interrupted").mkdir()
    with pytest.raises(ExperimentRunError, match="incomplete reservation"):
        freeze_candidate(runner, spec.experiment_id, program_id="p1")


def test_new_data_hash_cannot_hide_same_date_range_exposure(research_host: Any) -> None:
    runner, spec, frame = research_host
    run(runner, spec)
    altered = runner.catalog.write(
        "other-vintage",
        frame.with_columns(pl.col("volume") + 1),
        source="synthetic-test",
        schema_version="bars-v1",
        as_of=spec.request.submitted_at.replace(year=2021),
    )
    payload = json.loads(spec.to_json())
    payload["request"]["experiment_id"] = "dev-other"
    payload["recipe"]["hypothesis"]["experiment_id"] = "dev-other"
    native = altered.model_dump(mode="json")
    # Native recipe timestamps use six-digit UTC instants.
    for name in ("min_timestamp", "max_timestamp", "as_of"):
        value = getattr(altered, name)
        native[name] = value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    payload["recipe"]["dataset_snapshot"] = native
    payload["config_hash"] = canonical_config_hash(
        {k: v for k, v in payload.items() if k != "config_hash"}
    )
    another = ExperimentSpec.from_json(canonical_json(payload))
    with pytest.raises(ExperimentRunError, match="another research"):
        runner.run_experiment(another, research_program_id="p2", trial_family_id="f2")
