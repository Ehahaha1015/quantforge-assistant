from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import polars as pl
import pytest

from quantforge.product.evidence import evidence_id
from quantforge.product.runner import ExperimentRunError, ExperimentRunner
from quantforge.research import ExperimentSpec


def run(runner: ExperimentRunner, spec: ExperimentSpec) -> dict[str, Any]:
    return runner.run_experiment(spec, research_program_id="p1", trial_family_id="f1")


def test_run_persists_computed_evidence_and_exact_reproduction(
    research_host: Any,
) -> None:
    runner, spec, _ = research_host
    evidence = run(runner, spec)
    assert set(evidence) == {
        "schema_version",
        "FACT",
        "DECLARED_ASSUMPTION",
        "LIMITATION",
    }
    assert evidence["FACT"]["final_lockbox_scored"] is False
    assert len(evidence["FACT"]["validation_periods"]) == 28
    assert len(evidence["FACT"]["validation_windows"]) == 4
    assert evidence["FACT"]["validation_metrics"]["total_return"] > 0
    assert runner.inspect(spec.experiment_id) == evidence
    reproduced = runner.reproduce(spec.experiment_id)
    assert reproduced["result"] == "EXACT_MATCH"
    assert reproduced["reproduced_evidence_id"] == evidence_id(evidence)
    assert len(runner.store.list()) == 1


def test_restart_reopens_same_authoritative_evidence(research_host: Any) -> None:
    runner, spec, _ = research_host
    evidence = run(runner, spec)
    runner.close()
    restarted = ExperimentRunner(runner.root)
    try:
        assert restarted.inspect(spec.experiment_id) == evidence
        assert restarted.reproduce(spec.experiment_id)["result"] == "EXACT_MATCH"
    finally:
        restarted.close()


def test_final_prices_cannot_affect_development_calculation(research_host: Any) -> None:
    runner, spec, frame = research_host
    original = runner._compute(spec, frame)
    # Every final-test OHLC value changes dramatically, with valid ordering.
    changed = frame.with_columns(
        [
            pl.when(pl.int_range(pl.len()) >= 98)
            .then(pl.col(c) * 17)
            .otherwise(pl.col(c))
            .alias(c)
            for c in ("open", "high", "low", "close")
        ]
    )
    assert runner._compute(spec, changed) == original


def test_final_rows_never_enter_signal_generation(
    research_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quantforge.product.runner as module

    runner, spec, _ = research_host
    original = module.generate_moving_average_signals
    observed: list[int] = []

    def guarded(bars: pl.DataFrame, config: Any) -> pl.DataFrame:
        observed.append(bars.height)
        assert bars.height <= 98
        return original(bars, config)

    monkeypatch.setattr(module, "generate_moving_average_signals", guarded)
    run(runner, spec)
    assert observed == [70]


def test_duplicate_run_never_redispatches(
    research_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, spec, _ = research_host
    run(runner, spec)

    def forbidden(*args: Any) -> Any:
        pytest.fail("duplicate redispatched calculation")

    monkeypatch.setattr(runner, "_compute", forbidden)
    with pytest.raises(ExperimentRunError, match="reserved"):
        run(runner, spec)


def test_caller_cannot_supply_authoritative_metrics(research_host: Any) -> None:
    runner, spec, _ = research_host
    with pytest.raises(TypeError):
        runner.run_experiment(
            spec,
            research_program_id="p1",
            trial_family_id="f1",
            metrics={"sharpe": 999},
        )
    assert runner.store.list() == ()


def test_corrupt_evidence_rejects_inspection_and_reproduction(
    research_host: Any,
) -> None:
    runner, spec, _ = research_host
    run(runner, spec)
    path = next(runner.artifact_root.glob("run-*/EvidencePack.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["FACT"]["validation_metrics"]["sharpe"] = 999.0
    from quantforge.experiments import canonical_json

    path.write_bytes(canonical_json(payload).encode("utf-8"))
    with pytest.raises(ExperimentRunError, match="integrity"):
        runner.reproduce(spec.experiment_id)


def test_markdown_is_not_authority(research_host: Any) -> None:
    runner, spec, _ = research_host
    run(runner, spec)
    path = next(runner.artifact_root.glob("run-*/EvidencePack.md"))
    path.write_text("arbitrary derived view", encoding="utf-8")
    assert runner.reproduce(spec.experiment_id)["result"] == "EXACT_MATCH"


def test_changed_source_binding_rejects_reproduction(
    research_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quantforge.product.runner as module

    runner, spec, _ = research_host
    run(runner, spec)
    monkeypatch.setattr(module, "source_identity", lambda: "0" * 64)
    with pytest.raises(ExperimentRunError, match="code/runtime"):
        runner.reproduce(spec.experiment_id)


def test_changed_dataset_rejects_reproduction(research_host: Any) -> None:
    runner, spec, frame = research_host
    run(runner, spec)
    changed = frame.with_columns(pl.col("volume") + 1)
    runner.catalog.write(
        "synthetic-spy",
        changed,
        source="synthetic-test",
        schema_version="bars-v1",
        as_of=datetime(2020, 5, 6, tzinfo=UTC),
        overwrite=True,
    )
    with pytest.raises(ExperimentRunError, match="snapshot"):
        runner.reproduce(spec.experiment_id)


def test_inconsistent_declared_endpoints_reject_before_reservation(
    research_host: Any,
) -> None:
    runner, spec, _ = research_host
    payload = json.loads(spec.to_json())
    payload["recipe"]["split_timestamps"]["development_end"] = (
        "2020-03-08T00:00:00.000000Z"
    )
    from quantforge.experiments import canonical_config_hash

    payload["config_hash"] = canonical_config_hash(
        {k: v for k, v in payload.items() if k != "config_hash"}
    )
    changed = ExperimentSpec.from_json(
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )
    with pytest.raises(ExperimentRunError, match="endpoint"):
        run(runner, changed)
    assert not list(runner.artifact_root.glob("run-*"))


def test_interrupted_publication_leaves_spent_reservation(
    research_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, spec, _ = research_host

    def fail(*args: Any) -> Any:
        raise ValueError("calculation interrupted")

    monkeypatch.setattr(runner, "_compute", fail)
    with pytest.raises(ExperimentRunError):
        run(runner, spec)
    assert runner.store.list() == ()
    assert len(list(runner.artifact_root.glob("run-*/reservation.json"))) == 1
    with pytest.raises(ExperimentRunError, match="reserved"):
        run(runner, spec)
