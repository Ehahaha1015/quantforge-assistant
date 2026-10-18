"""One final holdout after immutable candidate freeze, using the frozen OOS gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from quantforge.backtest import BacktestConfig
from quantforge.experiments import (
    ArtifactRef,
    ExperimentRecord,
    canonical_config_hash,
    canonical_json,
)
from quantforge.research import ExperimentSpec, ResearchRequest
from quantforge.research.oos import seal_oos_test_use, verify_oos_test_use
from quantforge.strategies.models import config_from_strategy, family_metadata
from quantforge.validation import ChronologicalHoldoutConfig, run_holdout_validation

from .context import code_context, source_identity
from .evidence import evidence_id, read_evidence, render_markdown
from .policy import read_freeze
from .runner import ExperimentRunError, ExperimentRunner, _instant
from .storage import local_root, run_directory, write_exclusive, writer_lock


def final_evaluate(
    runner: ExperimentRunner, *, program_id: str, admitted_at: datetime
) -> dict[str, Any]:
    with writer_lock(runner.root):
        frozen = read_freeze(runner, program_id)
        original = runner.inspect(frozen["run_id"])
        original_facts = original["FACT"]
        body = original_facts["specification"]
        if (
            code_context() != body["recipe"]["code_context"]
            or source_identity() != original_facts["source_sha256"]
        ):
            raise ExperimentRunError(
                "final evaluation requires identical frozen code/runtime"
            )
        # Preserve the complete selected recipe and alter only new run identity/time.
        body = json.loads(canonical_json(body))
        final_id = program_id + "-final"
        body["request"]["experiment_id"] = final_id
        body["request"]["submitted_at"] = _instant(admitted_at)
        body["request"] = ResearchRequest.model_validate(body["request"]).model_dump(
            mode="json"
        )
        body["recipe"]["hypothesis"]["experiment_id"] = final_id
        body["config_hash"] = canonical_config_hash(
            {k: v for k, v in body.items() if k != "config_hash"}
        )
        spec = ExperimentSpec.from_json(canonical_json(body))
        receipt = seal_oos_test_use(
            spec,
            catalog=runner.catalog,
            experiment_store=runner.store,
            artifact_root=runner.root / "oos",
            admitted_at=admitted_at,
            prior_exposure="none_known_outside_factory",
            code_context=code_context(),
            mode="first_use",
        )
        if receipt["test_use_allowed"] is not True:
            raise ExperimentRunError("final test-use admission denied")
        frame = runner._bound_data(spec)
        recipe = spec.recipe.to_payload()
        strategy = config_from_strategy(
            recipe["strategy"], legacy=spec.schema_version == "experiment-spec-v1"
        )
        backtest = BacktestConfig(**recipe["backtest"])
        history = (
            recipe["split"]["development_bars"] + recipe["split"]["validation_bars"]
        )

        def compute() -> dict[str, Any]:
            result = run_holdout_validation(
                frame,
                strategy,
                backtest,
                ChronologicalHoldoutConfig(history_bars=history),
            )
            metrics = asdict(result.aggregate.metrics)
            return {
                "schema_version": "evidence-pack-v1",
                "FACT": {
                    "run_id": final_id,
                    "evaluation_stage": "FINAL_LOCKBOX",
                    "specification": json.loads(spec.to_json()),
                    "source_sha256": original_facts["source_sha256"],
                    **(
                        {"families": family_metadata(recipe["strategy"])}
                        if spec.schema_version == "experiment-spec-v2"
                        else {}
                    ),
                    "candidate_freeze_sha256": frozen["freeze_sha256"],
                    "selected_development_evidence_id": frozen["evidence_id"],
                    "research": {
                        "research_program_id": program_id,
                        "trial_family_id": frozen["trial_family_id"],
                        "candidate_count": frozen["candidate_count"],
                        "selection_rule": frozen["selection_rule"],
                        "partitions_seen": [
                            "development",
                            "validation",
                            "final_lockbox",
                        ],
                    },
                    "final_metrics": metrics,
                    "final_observations": result.aggregate.oos_bars,
                    "screen_pass": -metrics["max_drawdown"]
                    <= recipe["screen"]["max_drawdown_loss_magnitude"],
                    "final_lockbox_scored": True,
                },
                "DECLARED_ASSUMPTION": original["DECLARED_ASSUMPTION"],
                "LIMITATION": original["LIMITATION"]
                + [
                    "One post-freeze final holdout; passing a loss screen is not a statistically validated alpha claim."
                ],
            }

        evidence = compute()
        # Machine-only identical recomputation under the original active seal,
        # before a terminal row. This never creates a second candidate/exposure.
        reproduced = compute()
        if evidence_id(evidence) != evidence_id(reproduced):
            raise ExperimentRunError("final deterministic reproduction mismatch")
        if (
            source_identity() != original_facts["source_sha256"]
            or code_context() != recipe["code_context"]
        ):
            raise ExperimentRunError("final code/runtime changed")
        runner._bound_data(spec)
        directory = run_directory(runner.root / "oos", final_id)
        seal_path = directory / "oos-seal.json"
        seal_hash = hashlib.sha256(seal_path.read_bytes()).hexdigest()
        write_exclusive(directory / "EvidencePack.json", evidence)
        with (directory / "EvidencePack.md").open(
            "x", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(render_markdown(evidence))
        proof = {
            "run_id": final_id,
            "result": "EXACT_MATCH",
            "original_evidence_id": evidence_id(evidence),
            "reproduced_evidence_id": evidence_id(reproduced),
            "scope": "machine-only identical recomputation before terminal closure",
        }
        write_exclusive(directory / "reproduction.json", proof)
        outcome = (
            "RESEARCH_SCREEN_PASS"
            if evidence["FACT"]["screen_pass"]
            else "RESEARCH_SCREEN_FAIL"
        )
        record = ExperimentRecord.create(
            experiment_id=final_id,
            git_commit=recipe["code_context"]["git_commit"],
            dataset_id=recipe["dataset_snapshot"]["dataset_id"],
            dataset_hash=recipe["dataset_snapshot"]["content_hash"],
            backtest_config=spec.to_config(),
            random_seed=0,
            started_at=admitted_at,
            metrics={
                "research_factory": {
                    "outcome": outcome,
                    "evidence_id": evidence_id(evidence),
                    "artifact_integrity": {
                        "research_oos_seal": seal_hash,
                        "reproduction": hashlib.sha256(
                            canonical_json(proof).encode()
                        ).hexdigest(),
                    },
                }
            },
            artifact_refs=(
                ArtifactRef(name="research_oos_seal", uri=seal_path.as_uri()),
                ArtifactRef(
                    name="EvidencePack", uri=(directory / "EvidencePack.json").as_uri()
                ),
            ),
        )
        runner.store.save(record)
        verify_oos_test_use(
            spec,
            catalog=runner.catalog,
            experiment_store=runner.store,
            artifact_root=runner.root / "oos",
            code_context=code_context(),
        )
        return {
            "evidence": evidence,
            "reproduction": proof,
            "outcome": outcome,
            "paper_eligible": outcome == "RESEARCH_SCREEN_PASS",
            "validated_alpha": False,
            "live_eligible": False,
        }


def inspect_final(runner: ExperimentRunner, program_id: str) -> dict[str, Any]:
    frozen = read_freeze(runner, program_id)
    run_id = program_id + "-final"
    record = runner.store.get(run_id)
    spec = ExperimentSpec.from_json(
        canonical_json(
            {**record.backtest_config.to_payload(), "config_hash": record.config_hash}
        )
    )
    directory = run_directory(runner.root / "oos", run_id)
    local_root(directory)
    path = directory / "EvidencePack.json"
    evidence = read_evidence(path)
    metrics = record.metrics["research_factory"]
    if (
        metrics["evidence_id"] != evidence_id(evidence)
        or evidence["FACT"]["candidate_freeze_sha256"] != frozen["freeze_sha256"]
    ):
        raise ExperimentRunError("final evidence/freeze mismatch")
    if evidence["FACT"]["specification"] != json.loads(spec.to_json()):
        raise ExperimentRunError("final spec/evidence mismatch")
    refs = {ref.name: ref.uri for ref in record.artifact_refs}
    if refs.get("EvidencePack") != path.as_uri():
        raise ExperimentRunError("final artifact mismatch")
    proof_path = directory / "reproduction.json"
    if proof_path.is_symlink():
        raise ExperimentRunError("redirected reproduction artifact")
    raw = proof_path.read_bytes()
    proof = json.loads(raw)
    if (
        raw != canonical_json(proof).encode()
        or hashlib.sha256(raw).hexdigest()
        != metrics["artifact_integrity"]["reproduction"]
    ):
        raise ExperimentRunError("final reproduction integrity mismatch")
    if (
        proof["original_evidence_id"] != evidence_id(evidence)
        or proof["reproduced_evidence_id"] != evidence_id(evidence)
        or proof["result"] != "EXACT_MATCH"
    ):
        raise ExperimentRunError("final reproduction evidence mismatch")
    verify_oos_test_use(
        spec,
        catalog=runner.catalog,
        experiment_store=runner.store,
        artifact_root=runner.root / "oos",
        code_context=spec.recipe["code_context"],
    )
    return {
        "evidence": evidence,
        "reproduction": proof,
        "outcome": metrics["outcome"],
        "paper_eligible": metrics["outcome"] == "RESEARCH_SCREEN_PASS",
        "validated_alpha": False,
        "live_eligible": False,
    }
