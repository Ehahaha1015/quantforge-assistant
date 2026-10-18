"""Deterministic freeze and bounded family screening, never AI performance truth."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from quantforge.experiments import canonical_json
from quantforge.research import ExperimentSpec

from .evidence import evidence_id
from .runner import ExperimentRunError, ExperimentRunner, _identifier
from .storage import local_root, run_directory, write_exclusive, writer_lock


def multiplicity_screen(returns: list[float], candidate_count: int) -> dict[str, Any]:
    """Bonferroni correction of a declared normal/iid mean-return z screen.

    This is an exploratory diagnostic, not a universal corrected Sharpe or a
    validation certificate. Dependence/regime/short-sample assumptions remain
    explicit and the untouched final lockbox remains mandatory.
    """
    if (
        not 1 <= candidate_count <= 8
        or len(returns) < 2
        or any(not math.isfinite(v) for v in returns)
    ):
        raise ExperimentRunError("invalid statistical screen input")
    mean = math.fsum(returns) / len(returns)
    variance = math.fsum((v - mean) ** 2 for v in returns) / (len(returns) - 1)
    if variance == 0:
        pvalue = 1.0  # degeneracy cannot authorize a significance claim
    else:
        zvalue = mean / math.sqrt(variance / len(returns))
        pvalue = 0.5 * math.erfc(zvalue / math.sqrt(2))
    adjusted = min(1.0, candidate_count * pvalue)
    return {
        "method": "bonferroni_normal_mean_return_diagnostic",
        "candidate_count": candidate_count,
        "uncorrected_p": pvalue,
        "familywise_adjusted_p": adjusted,
        "alpha": 0.05,
        "passes_diagnostic": adjusted <= 0.05,
        "assumptions": [
            "iid approximately normal daily returns",
            "family count includes every retained proposal reservation",
        ],
        "limitations": [
            "serial dependence, fat tails and short samples can invalidate the base normal approximation",
            "diagnostic never grants validated/live status",
        ],
    }


def freeze_candidate(
    runner: ExperimentRunner, run_id: str, *, program_id: str
) -> dict[str, Any]:
    program_id = _identifier(program_id)
    root = runner.root / "programs"
    root.mkdir(exist_ok=True)
    local_root(root)
    with writer_lock(runner.root):
        evidence = runner.inspect(run_id)
        facts = evidence["FACT"]
        if facts["research"]["research_program_id"] != program_id:
            raise ExperimentRunError("candidate belongs to another program")
        result = runner.reproduce(run_id)
        if result["result"] != "EXACT_MATCH" or not facts["screen_pass"]:
            raise ExperimentRunError(
                "candidate lacks reproduced passing development screen"
            )
        family = facts["research"]["trial_family_id"]
        # Count immutable reservations, including experiments interrupted before save.
        configs = []
        for entry in sorted(runner.artifact_root.iterdir()):
            if not entry.name.casefold().startswith("run-"):
                continue
            path = entry / "reservation.json"
            local_root(path.parent)
            if path.is_symlink() or not path.is_file():
                raise ExperimentRunError("incomplete reservation history blocks freeze")
            raw = path.read_bytes()
            config = json.loads(raw)
            if raw != canonical_json(config).encode("utf-8"):
                raise ExperimentRunError("invalid reservation history")
            spec = ExperimentSpec.from_json(canonical_json(config["spec"]))
            if (
                entry.name
                != run_directory(runner.artifact_root, spec.experiment_id).name
            ):
                raise ExperimentRunError("reservation identity mismatch")
            if config.get("trial_family_id") == family:
                configs.append(config)
        returns = [row["strategy_return"] for row in facts["validation_periods"]]
        payload = {
            "schema_version": "candidate-freeze-v1",
            "program_id": program_id,
            "trial_family_id": family,
            "run_id": run_id,
            "evidence_id": evidence_id(evidence),
            "candidate_count": len(configs),
            "selection_rule": "explicit evidence-referenced host/Lab freeze",
            "validation_scheme": "chronological expanding development validation; final rows unscored",
            "statistical_diagnostic": multiplicity_screen(returns, len(configs)),
            "status": "FROZEN_RESEARCH_CANDIDATE",
            "paper_eligibility": "REQUIRES_FINAL_SCREEN",
            "validated_alpha": False,
            "live_eligible": False,
        }
        directory = root / program_id
        directory.mkdir(exist_ok=True)
        local_root(directory)
        path = directory / "freeze.json"
        if path.exists():
            if path.read_bytes() != canonical_json(payload).encode("utf-8"):
                raise ExperimentRunError(
                    "program already frozen to a different candidate/history"
                )
            return payload
        write_exclusive(path, payload)
        return payload


def read_freeze(runner: ExperimentRunner, program_id: str) -> dict[str, Any]:
    program_id = _identifier(program_id)
    path = runner.root / "programs" / program_id / "freeze.json"
    local_root(path.parent)
    if path.is_symlink():
        raise ExperimentRunError("redirected freeze")
    raw = path.read_bytes()
    payload = json.loads(raw)
    if (
        raw != canonical_json(payload).encode("utf-8")
        or payload.get("schema_version") != "candidate-freeze-v1"
    ):
        raise ExperimentRunError("invalid candidate freeze")
    evidence = runner.inspect(payload["run_id"])
    if payload["program_id"] != program_id or payload["evidence_id"] != evidence_id(
        evidence
    ):
        raise ExperimentRunError("frozen candidate evidence mismatch")
    return {**payload, "freeze_sha256": hashlib.sha256(raw).hexdigest()}
