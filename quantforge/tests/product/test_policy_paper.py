from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from quantforge.product.lockbox import final_evaluate, inspect_final
from quantforge.product.paper import PaperRuntime
from quantforge.product.policy import freeze_candidate, multiplicity_screen
from quantforge.product.runner import ExperimentRunError
from quantforge.research import ExperimentSpec
from quantforge.research.oos import ResearchSealError

from .test_runner import run


def frozen(research_host: Any) -> Any:
    runner, spec, _ = research_host
    run(runner, spec)
    freeze = freeze_candidate(runner, spec.experiment_id, program_id="p1")
    return runner, spec, freeze


def test_family_statistical_diagnostic_tracks_multiplicity_and_assumptions() -> None:
    values = [0.003, -0.001, 0.001, 0.002] * 10
    first = multiplicity_screen(values, 1)
    many = multiplicity_screen(values, 8)
    assert many["familywise_adjusted_p"] == min(1, first["uncorrected_p"] * 8)
    assert many["assumptions"] and many["limitations"]
    assert multiplicity_screen([0.1] * 10, 1)["passes_diagnostic"] is False


def test_freeze_binds_reproduced_evidence_and_blocks_more_trials(
    research_host: Any,
) -> None:
    runner, spec, result = frozen(research_host)
    assert result["candidate_count"] == 1
    assert result["live_eligible"] is False and result["validated_alpha"] is False
    with pytest.raises(ExperimentRunError, match="frozen"):
        run(runner, spec)
    assert freeze_candidate(runner, spec.experiment_id, program_id="p1") == result


def test_final_requires_freeze_and_spends_one_opportunity(research_host: Any) -> None:
    runner, _, _ = research_host
    with pytest.raises(ValueError, match="root required"):
        final_evaluate(
            runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
        )
    runner, _spec, _ = frozen(research_host)
    result = final_evaluate(
        runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert result["evidence"]["FACT"]["evaluation_stage"] == "FINAL_LOCKBOX"
    assert result["evidence"]["FACT"]["final_observations"] == 28
    assert result["reproduction"]["result"] == "EXACT_MATCH"
    assert inspect_final(runner, "p1") == result
    with pytest.raises(ResearchSealError):
        final_evaluate(
            runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
        )
    assert len(runner.store.list()) == 2


def test_paper_resume_has_identical_state_without_duplicate_fills(
    research_host: Any,
) -> None:
    runner, _, _ = frozen(research_host)
    final_evaluate(
        runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    paper = PaperRuntime(runner, "p1")
    before = paper.run(steps=40)
    assert before["cursor"] == 40 and before["fills"] >= 1
    history = paper.history()
    paper.close()
    restart = PaperRuntime(runner, "p1")
    try:
        assert restart.status() == before
        assert restart.history() == history
        assert restart.start() == before
        after = restart.run(steps=10)
        assert after["cursor"] == 50
        assert after["fills"] >= before["fills"]
        fill_ids = [
            item["fill"]["order_id"] for item in restart.history() if item.get("fill")
        ]
        assert len(fill_ids) == len(set(fill_ids))
        assert restart.stop()["status"] == "STOPPED"
        assert restart.step()["status"] == "STOPPED"
        assert restart.start()["cursor"] == 50
        assert restart.run()["stream_status"] == "WAITING_FOR_DATA"
    finally:
        restart.close()


def test_paper_never_uses_same_bar_close_for_order_intent(
    research_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quantforge.product.paper as module

    runner, _, _ = frozen(research_host)
    final_evaluate(
        runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    paper = PaperRuntime(runner, "p1")
    original = module.generate_moving_average_signals

    def causal(bars: Any, config: Any) -> Any:
        assert bars.height == paper.status()["cursor"]
        return original(bars, config)

    monkeypatch.setattr(module, "generate_moving_average_signals", causal)
    try:
        paper.run(steps=18)
        for item in paper.history():
            if item.get("fill"):
                assert item["risk"]["approved"] is True
                assert item["fill"]["fee"] != "0"
        assert paper.status()["real_transport"] == "LOCKED"
    finally:
        paper.close()


def test_paper_event_transaction_failure_leaves_cursor_unspent(
    research_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, _ = frozen(research_host)
    final_evaluate(
        runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    paper = PaperRuntime(runner, "p1")
    try:
        paper.start()
        original = paper._append

        def interrupted(item: Any) -> None:
            raise OSError("simulated precommit crash")

        monkeypatch.setattr(paper, "_append", interrupted)
        with pytest.raises(OSError):
            paper.step()
        assert paper.status()["cursor"] == 0
        monkeypatch.setattr(paper, "_append", original)
        assert paper.step()["cursor"] == 1
    finally:
        paper.close()


def test_paper_store_is_append_only_and_corruption_is_detected(
    research_host: Any,
) -> None:
    import sqlite3

    runner, _, _ = frozen(research_host)
    final_evaluate(
        runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    paper = PaperRuntime(runner, "p1")
    try:
        paper.run(steps=1)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            paper.connection.execute("UPDATE events SET payload='{}' WHERE seq=1")
        paper.connection.execute("DROP TRIGGER no_update")
        paper.connection.execute("UPDATE events SET payload='{}' WHERE seq=1")
        paper.connection.commit()
        with pytest.raises(ExperimentRunError, match="integrity"):
            paper.status()
    finally:
        paper.close()


def test_new_family_name_cannot_hide_same_dataset_exposure(research_host: Any) -> None:
    from quantforge.experiments import canonical_config_hash, canonical_json

    runner, spec, _ = research_host
    run(runner, spec)
    payload = __import__("json").loads(spec.to_json())
    payload["request"]["experiment_id"] = "dev-002"
    payload["recipe"]["hypothesis"]["experiment_id"] = "dev-002"
    payload["config_hash"] = canonical_config_hash(
        {k: v for k, v in payload.items() if k != "config_hash"}
    )
    another = ExperimentSpec.from_json(canonical_json(payload))
    with pytest.raises(ExperimentRunError, match="another research"):
        runner.run_experiment(another, research_program_id="p2", trial_family_id="f2")
