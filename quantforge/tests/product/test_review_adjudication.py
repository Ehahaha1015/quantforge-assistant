from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from quantforge.experiments import canonical_config_hash, canonical_json
from quantforge.product.context import repository_root
from quantforge.product.lockbox import final_evaluate, inspect_final
from quantforge.product.paper import PaperRuntime
from quantforge.product.runner import ExperimentRunError, _instant, _timestamps
from quantforge.research import ExperimentSpec
from quantforge.research.oos import ResearchSealError

from .test_policy_paper import frozen
from .test_runner import run


def test_same_family_cannot_accumulate_disjoint_datasets(research_host: Any) -> None:
    runner, spec, frame = research_host
    run(runner, spec)
    shifted = frame.with_columns(pl.col("timestamp") + pl.duration(days=200))
    snapshot = runner.catalog.write(
        "disjoint",
        shifted,
        source="synthetic-test",
        schema_version="bars-v1",
        as_of=spec.request.submitted_at + timedelta(days=330),
    )
    body = json.loads(spec.to_json())
    body["request"]["experiment_id"] = "disjoint-run"
    body["recipe"]["hypothesis"]["experiment_id"] = "disjoint-run"
    native = snapshot.model_dump(mode="json")
    for name in ("min_timestamp", "max_timestamp", "as_of"):
        native[name] = _instant(getattr(snapshot, name))
    body["recipe"]["dataset_snapshot"] = native
    instants = _timestamps(shifted)
    for key, index in zip(
        (
            "development_start",
            "development_end",
            "validation_start",
            "validation_end",
            "test_start",
            "test_end",
        ),
        (0, 69, 70, 97, 98, 125),
        strict=True,
    ):
        body["recipe"]["split_timestamps"][key] = _instant(instants[index])
    body["config_hash"] = canonical_config_hash(
        {k: v for k, v in body.items() if k != "config_hash"}
    )
    changed = ExperimentSpec.from_json(canonical_json(body))
    with pytest.raises(ExperimentRunError, match="dataset/vintage"):
        runner.run_experiment(changed, research_program_id="p1", trial_family_id="f1")


def test_expected_cli_precondition_has_structured_block_reason(
    research_host: Any,
) -> None:
    runner, _, _ = research_host
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "quantforge.product.cli",
            "--root",
            str(runner.root),
            "paper-start",
            "missing-program",
        ],
        cwd=repository_root(),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "BLOCKED" and payload["reason"]
    assert "Traceback" not in result.stderr


def test_paper_signal_alignment_matches_independent_prior_close_reference(
    research_host: Any,
) -> None:
    runner, spec, _ = frozen(research_host)
    final_evaluate(
        runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    frame = runner.catalog.read(spec.recipe["dataset_snapshot"]["dataset_id"])
    closes = frame["close"].to_list()
    short, long = (
        spec.recipe["strategy"]["short_window"],
        spec.recipe["strategy"]["long_window"],
    )
    paper = PaperRuntime(runner, "p1")
    try:
        paper.run(steps=24)
        for event in paper.history():
            if event["kind"] != "BAR":
                continue
            index = event["cursor"] - 1  # cursor always names the next unprocessed bar
            expected = (
                0
                if index < long
                else int(
                    math.fsum(closes[index - short : index]) / short
                    > math.fsum(closes[index - long : index]) / long
                )
            )
            assert event["desired_position"] == expected
            assert (
                event["timestamp"] == _timestamps(frame.slice(index, 1))[0].isoformat()
            )
    finally:
        paper.close()


def test_interrupted_final_proof_never_becomes_authoritative_or_reshows_freshness(
    research_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, _ = frozen(research_host)
    original = runner.store.save

    def crash(record: Any) -> Any:
        raise OSError("simulated crash before terminal publication")

    monkeypatch.setattr(runner.store, "save", crash)
    with pytest.raises(OSError):
        final_evaluate(
            runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
        )
    assert list((runner.root / "oos").glob("run-*/reproduction.json"))
    from quantforge.experiments import ExperimentNotFoundError

    with pytest.raises(ExperimentNotFoundError):
        inspect_final(runner, "p1")
    monkeypatch.setattr(runner.store, "save", original)
    with pytest.raises(ResearchSealError):
        final_evaluate(
            runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
        )


def test_sqlite_paper_reader_holds_one_consistent_snapshot_during_append(
    research_host: Any,
) -> None:
    runner, _, _ = frozen(research_host)
    final_evaluate(
        runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    paper = PaperRuntime(runner, "p1")
    try:
        paper.run(steps=3)
        before = paper.status()["events"]
        with sqlite3.connect(paper.path) as reader:
            cursor = reader.execute(
                "SELECT seq,previous_hash,payload,event_hash FROM events ORDER BY seq"
            )
            first = cursor.fetchone()
            paper.step()  # another connection commits while reader's statement is open
            rows = [first, *cursor.fetchall()]
        assert len(rows) == before
        assert paper.status()["events"] == before + 1
        assert paper.history()[-1]["cursor"] == 4
    finally:
        paper.close()
