from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import polars as pl
import pytest

from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.experiments import FrozenPayload, canonical_config_hash, canonical_json
from quantforge.product.evidence import evidence_id
from quantforge.product.lockbox import final_evaluate, inspect_final
from quantforge.product.paper import PaperRuntime
from quantforge.product.policy import freeze_candidate
from quantforge.product.runner import ExperimentRunError, ExperimentRunner
from quantforge.product.service import ResearchService
from quantforge.research import ExperimentSpec, ResearchRequest, admit_research_request
from quantforge.research.oos import ResearchSealError
from quantforge.strategies import (
    MovingAverageCrossoverConfig,
    generate_moving_average_signals,
)
from quantforge.strategies.models import (
    config_from_strategy,
    generate_strategy_signals,
    required_observations,
    strategy_from_parameters,
)
from quantforge.validation import (
    ChronologicalHoldoutConfig,
    WalkForwardConfig,
    run_holdout_validation,
    run_walk_forward_validation,
)

CASES = [
    ("donchian_breakout", {"entry_window": 3, "exit_window": 1}),
    ("bollinger_reversion", {"window": 3, "k_pct": "100.0"}),
    ("time_series_momentum", {"lookback": 3, "threshold_pct": "0.0"}),
]


@pytest.fixture
def breadth_host(research_host: Any) -> Any:
    runner, spec, frame = research_host
    # Deterministic declared daily OHLC with real entry and exit opportunities.
    prices = [100.0 + i * 0.1 + (i % 11) * 0.8 for i in range(frame.height)]
    frame = frame.with_columns(
        [
            pl.Series(c, prices, dtype=pl.Float64)
            for c in ("open", "high", "low", "close")
        ]
    )
    runner.catalog.write(
        "synthetic-spy",
        frame,
        source="synthetic-test",
        schema_version="bars-v1",
        as_of=datetime(2020, 5, 6, tzinfo=UTC),
        overwrite=True,
    )
    return runner, spec, frame


def admit_family(
    runner: ExperimentRunner,
    base: ExperimentSpec,
    family: str,
    parameters: dict[str, Any],
    *,
    run_id: str = "breadth-001",
) -> ExperimentSpec:
    recipe = base.recipe.to_payload()
    request = ResearchRequest.model_validate(
        {**base.request.model_dump(mode="json"), "experiment_id": run_id}
    )
    proposal = {
        k: recipe[k]
        for k in (
            "execution",
            "backtest",
            "costs",
            "split",
            "parameter_optimization",
            "random_seed",
        )
    }
    proposal.update(
        schema_version="strategy-proposal-v2",
        hypothesis={
            k: recipe["hypothesis"][k]
            for k in ("statement", "rationale", "falsification")
        },
        strategy=strategy_from_parameters(family, parameters),
    )
    return admit_research_request(
        request,
        proposal=proposal,
        dataset_snapshot=runner.catalog.verify("synthetic-spy"),
        data_conventions=recipe["data_conventions"],
        code_context=recipe["code_context"],
        split_timestamps=recipe["split_timestamps"],
    )


@pytest.mark.parametrize(("family", "parameters"), CASES)
def test_family_end_to_end_evidence_and_exact_reproduction(
    breadth_host: Any, family: str, parameters: dict[str, Any]
) -> None:
    runner, base, frame = breadth_host
    spec = admit_family(runner, base, family, parameters)
    evidence = runner.run_experiment(
        spec, research_program_id="p1", trial_family_id="research-budget"
    )
    assert set(evidence) == {
        "schema_version",
        "FACT",
        "DECLARED_ASSUMPTION",
        "LIMITATION",
    }
    facts = evidence["FACT"]
    assert facts["specification"]["schema_version"] == "experiment-spec-v2"
    assert facts["evaluation_stage"] == "DEVELOPMENT"
    assert facts["source_sha256"] and facts["final_lockbox_scored"] is False
    assert facts["research"]["trial_family_id"] == "research-budget"
    assert facts["research"]["partitions_seen"] == ["development", "validation"]
    assert len(facts["validation_periods"]) == 28
    assert len(facts["validation_windows"]) == 4
    assert facts["data_qualification"]["status"] == "HOST_DECLARED_DATA"
    metadata = facts["families"]
    assert metadata["strategy_family"] == family
    assert metadata["parameters"] == parameters
    assert metadata["required_observations"] == required_observations(
        config_from_strategy(spec.recipe["strategy"])
    )
    assert metadata["position_values"] == [0, 1]
    assert metadata["maximum_parameter_window"] == 252
    assert (
        metadata["information_boundary"]
        == "canonical_bars_at_or_before_signal_timestamp"
    )
    assert metadata["execution_timing"] == "next_bar_open"
    assert metadata["validated_alpha"] is False
    assert facts["development_metrics"]["turnover"] > 0
    assert facts["validation_metrics"]["turnover"] > 0
    assert evidence["DECLARED_ASSUMPTION"] and evidence["LIMITATION"]
    assert any("no alpha" in item for item in evidence["LIMITATION"])
    assert "sharpe" not in metadata
    assert runner.inspect(spec.experiment_id) == evidence
    proof = runner.reproduce(spec.experiment_id)
    assert proof["result"] == "EXACT_MATCH"
    assert (
        proof["original_evidence_id"]
        == proof["reproduced_evidence_id"]
        == evidence_id(evidence)
    )
    assert len(runner.store.list()) == 1
    # Strong boundary: final-test prices never enter any development result.
    changed = frame.with_columns(
        [
            pl.when(pl.int_range(pl.len()) >= 98)
            .then(pl.col(c) * 17)
            .otherwise(pl.col(c))
            .alias(c)
            for c in ("open", "high", "low", "close")
        ]
    )
    assert runner._compute(spec, frame) == runner._compute(spec, changed)
    runner.close()
    restart = ExperimentRunner(runner.root)
    try:
        assert restart.inspect(spec.experiment_id) == evidence
        assert restart.reproduce(spec.experiment_id)["result"] == "EXACT_MATCH"
    finally:
        restart.close()


@pytest.mark.parametrize(("family", "parameters"), CASES)
def test_family_lockbox_and_paper_dispatch_remain_sealed_causal_and_restart_safe(
    breadth_host: Any, family: str, parameters: dict[str, Any]
) -> None:
    runner, base, frame = breadth_host
    spec = admit_family(runner, base, family, parameters)
    runner.run_experiment(
        spec, research_program_id="p1", trial_family_id="research-budget"
    )
    freeze_candidate(runner, spec.experiment_id, program_id="p1")
    final = final_evaluate(
        runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert final["reproduction"]["result"] == "EXACT_MATCH"
    assert final["evidence"]["FACT"]["families"]["strategy_family"] == family
    assert final["evidence"]["FACT"]["final_lockbox_scored"] is True
    assert final["evidence"]["FACT"]["final_observations"] == 28
    assert final["validated_alpha"] is False and final["live_eligible"] is False
    assert final["paper_eligible"] is True
    assert inspect_final(runner, "p1") == final
    with pytest.raises(ResearchSealError):
        final_evaluate(
            runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
        )
    paper = PaperRuntime(runner, "p1")
    try:
        before = paper.run(steps=30)
        history = paper.history()
        assert before["config"]["families"]["strategy_family"] == family
        assert before["real_transport"] == "LOCKED" and before["fills"] > 0
        expected = generate_strategy_signals(
            frame, config_from_strategy(spec.recipe["strategy"])
        )["value"].to_list()
        for item in history:
            if item["kind"] == "BAR":
                index = item["cursor"] - 1
                assert item["desired_position"] == (expected[index - 1] if index else 0)
                if item["fill"]:
                    assert item["risk"]["approved"] is True
                    assert item["fill"]["fee"] != "0"
        paper.close()
        paper = PaperRuntime(runner, "p1")
        assert paper.status() == before and paper.history() == history
        assert paper.start() == before
        assert paper.run(steps=5)["cursor"] == 35
        fill_ids = [
            item["fill"]["order_id"] for item in paper.history() if item.get("fill")
        ]
        assert len(set(fill_ids)) == len(fill_ids)
    finally:
        paper.close()


@pytest.mark.parametrize(("family", "parameters"), CASES)
def test_family_final_scoring_matches_direct_signal_backtest(
    breadth_host: Any, family: str, parameters: dict[str, Any]
) -> None:
    runner, base, frame = breadth_host
    spec = admit_family(runner, base, family, parameters)
    config = config_from_strategy(spec.recipe["strategy"])
    backtest = BacktestConfig(**spec.recipe["backtest"])
    direct = run_vectorized_backtest(
        frame, generate_strategy_signals(frame, config), backtest
    )
    result = run_holdout_validation(
        frame, config, backtest, ChronologicalHoldoutConfig(history_bars=98)
    )
    assert (
        result.aggregate.periods["strategy_return"].to_list()
        == direct.periods["strategy_return"].tail(28).to_list()
    )
    assert (
        result.aggregate.metrics.turnover == direct.periods["turnover"].tail(28).sum()
    )


def service_call(service: ResearchService, **kwargs: Any) -> dict[str, Any]:
    return service.propose_and_run(
        dataset_id="synthetic-spy",
        program_id="service-p",
        family_id="shared-budget",
        run_id="service-001",
        goal="Evaluate a fixed causal family",
        submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
        model_call_id="synthetic-call",
        rationale="Declared hypothesis only",
        **kwargs,
    )


@pytest.mark.parametrize(
    ("family", "parameters"),
    CASES + [("moving_average_crossover", {"short_window": 3, "long_window": 10})],
)
def test_facade_admits_v2_family_and_projects_safe_model_context(
    breadth_host: Any, family: str, parameters: dict[str, Any]
) -> None:
    runner, _, _ = breadth_host
    service = ResearchService(runner.root)
    try:
        evidence = service_call(service, family=family, parameters=parameters)
        assert service.reproduce("service-001")["result"] == "EXACT_MATCH"
        context = service.model_context("service-001")
        assert context["strategy"] == {"family": family, "parameters": parameters}
        assert context["FACT"]["families"] == evidence["FACT"]["families"]
        assert "validation_periods" not in context["FACT"]
        assert "final_metrics" not in canonical_json(context)
    finally:
        service.close()


def test_facade_legacy_call_retains_v1_spec_pack_and_context_shape(
    breadth_host: Any,
) -> None:
    runner, _, _ = breadth_host
    service = ResearchService(runner.root)
    try:
        evidence = service_call(service, short_window=3, long_window=10)
        assert (
            evidence["FACT"]["specification"]["schema_version"] == "experiment-spec-v1"
        )
        assert "families" not in evidence["FACT"]
        assert (
            evidence["LIMITATION"][0]
            == "Fixed long/flat SMA only; no alpha or future profitability claim."
        )
        context = service.model_context("service-001")
        assert (
            context["strategy"]
            == evidence["FACT"]["specification"]["recipe"]["strategy"]
        )
        assert "families" not in context["FACT"]
    finally:
        service.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"family": "donchian_breakout"},
        {"parameters": {"entry_window": 3, "exit_window": 1}},
        {
            "family": "donchian_breakout",
            "parameters": {"entry_window": 3, "exit_window": 1},
            "short_window": 2,
            "long_window": 3,
        },
        {"family": "bollinger_reversion", "parameters": {"window": 3, "k_pct": 2.0}},
        {
            "family": "donchian_breakout",
            "parameters": {"entry_window": 3, "exit_window": 1, "claimed_return": 99},
        },
        {"family": "unsupported", "parameters": {}},
        {"short_window": 3},
        {"long_window": 10},
        {
            "family": "bollinger_reversion",
            "parameters": {"window": 26, "k_pct": "100.0"},
        },
        {
            "family": "donchian_breakout",
            "parameters": {"entry_window": 25, "exit_window": 1},
        },
        {
            "family": "time_series_momentum",
            "parameters": {"lookback": 25, "threshold_pct": "0"},
        },
    ],
)
def test_facade_rejects_invalid_and_out_of_warmup_proposals_before_reservation(
    breadth_host: Any, arguments: dict[str, Any]
) -> None:
    runner, _, _ = breadth_host
    service = ResearchService(runner.root)
    try:
        with pytest.raises(ValueError):
            service_call(service, **arguments)
        assert service.list_runs() == []
        assert list(runner.artifact_root.iterdir()) == []
    finally:
        service.close()


def test_switching_strategy_does_not_reset_research_budget_governance(
    breadth_host: Any,
) -> None:
    runner, base, _ = breadth_host
    first = admit_family(runner, base, *CASES[0])
    runner.run_experiment(
        first, research_program_id="p1", trial_family_id="shared-budget"
    )
    second = admit_family(runner, base, *CASES[1], run_id="breadth-002")
    with pytest.raises(ExperimentRunError, match="another research"):
        runner.run_experiment(
            second, research_program_id="p1", trial_family_id="fresh-budget"
        )
    evidence = runner.run_experiment(
        second, research_program_id="p1", trial_family_id="shared-budget"
    )
    assert evidence["FACT"]["research"]["candidate_count"] == 2
    assert evidence["FACT"]["research"]["prior_exposures"] == [first.experiment_id]
    freeze_candidate(runner, second.experiment_id, program_id="p1")
    third = admit_family(runner, base, *CASES[2], run_id="breadth-003")
    with pytest.raises(ExperimentRunError, match="frozen"):
        runner.run_experiment(
            third, research_program_id="p1", trial_family_id="shared-budget"
        )


def test_v1_and_v2_sma_dispatch_have_identical_metrics_and_legacy_pack_contract(
    research_host: Any,
) -> None:
    runner, legacy, frame = research_host
    newer = admit_family(
        runner,
        legacy,
        "moving_average_crossover",
        {"short_window": 3, "long_window": 10},
        run_id="sma-v2",
    )
    old_computed = runner._compute(legacy, frame)
    assert runner._compute(newer, frame) == old_computed
    sma = MovingAverageCrossoverConfig(3, 10)
    backtest = BacktestConfig(**legacy.recipe["backtest"])
    assert old_computed["development_metrics"] == asdict(
        run_vectorized_backtest(
            frame.head(70),
            generate_moving_average_signals(frame.head(70), sma),
            backtest,
        ).metrics
    )
    assert old_computed["validation_metrics"] == asdict(
        run_walk_forward_validation(
            frame.head(98),
            sma,
            backtest,
            WalkForwardConfig(initial_history_bars=70, oos_window_bars=7),
        ).aggregate.metrics
    )
    metadata = {"research_program_id": "p1", "trial_family_id": "f1"}
    packed = runner._pack(legacy, old_computed, metadata, "a" * 64)
    assert "families" not in packed["FACT"]
    assert packed["FACT"]["specification"] == json.loads(legacy.to_json())
    assert packed["FACT"]["research"] == metadata
    assert (
        packed["LIMITATION"][0]
        == "Fixed long/flat SMA only; no alpha or future profitability claim."
    )
    assert "close_at_or_before_signal_timestamp" not in canonical_json(packed)


def test_runner_revalidates_forged_strategy_before_any_publication(
    breadth_host: Any,
) -> None:
    runner, base, _ = breadth_host
    spec = admit_family(runner, base, *CASES[0])
    body = json.loads(spec.to_json())
    body["recipe"]["strategy"]["entry_window"] = True
    body["config_hash"] = canonical_config_hash(
        {k: v for k, v in body.items() if k != "config_hash"}
    )
    forged = ExperimentSpec.model_construct(
        schema_version="experiment-spec-v2",
        request=spec.request,
        recipe=FrozenPayload.from_payload(body["recipe"], name="forged recipe"),
        config_hash=body["config_hash"],
    )
    with pytest.raises(ValueError):
        runner.run_experiment(forged, research_program_id="p1", trial_family_id="f1")
    assert runner.store.list() == () and list(runner.artifact_root.iterdir()) == []
