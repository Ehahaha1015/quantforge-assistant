"""Frozen 60-case protocol, source-visible calls and deterministic grading."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from qf_lab.evaluation import EvaluationAnswer, EvaluationBatch
from qf_lab.gateway import GatewayError, canonical
from qf_lab.request_preview import RequestPreview
from qf_lab.retrieval import KnowledgeCorpus
from quantforge.product.storage import local_root, write_exclusive, writer_lock

from .agent_runtime import PROFILE_ID, PROTECTED_BINDINGS, AgentRuntime
from .bindings import BindingRegistry
from .dataset_use import WORKSPACE
from .evaluation_cases import suite
from .evaluation_grading import bounded_answer, rule_answer, score, visibility
from .knowledge import build_corpus
from .storage import digest

OUTPUT = WORKSPACE / "local-smoke/long-term-20261013/agent-evaluation/run-v5"
PAID_ARMS = ("SINGLE_MODEL", "BM25_MODEL", "CRITIC", "MEMORY_ABLATION")


def parts(cases: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "development": [case for case in cases if case["split"] == "DEVELOPMENT"],
        "held-present": [
            case
            for case in cases
            if case["split"] == "HELD" and case["visibility"] == "PRESENT"
        ],
        "held-restricted": [
            case
            for case in cases
            if case["split"] == "HELD" and case["visibility"] != "PRESENT"
        ],
    }


def context_for(
    arm: str,
    batch: str,
    cases: list[dict[str, Any]],
    corpus: KnowledgeCorpus,
    *,
    previous: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    memory = arm == "MEMORY_ABLATION"
    retrieval = arm != "SINGLE_MODEL" and batch != "held-restricted"
    documents: dict[str, dict[str, Any]] = {}
    hits = {}
    for case in cases:
        search = (
            corpus.search(case["question"], visibility(case, corpus, memory=memory))
            if retrieval
            else {"matches": []}
        )
        hits[case["id"]] = [item["id"] for item in search["matches"]]
        for item in search["matches"]:
            documents[item["id"]] = {
                "id": item["id"],
                "text": item["text"]
                if not item["claims"]
                else "Source-bound claims; named ETF/scenario, "
                "exposed raw price fractions, "
                "not alpha or full total return.",
                "scope": item["scope"],
                "claims": [
                    {key: value[key] for key in ("id", "value", "unit")}
                    for value in item["claims"]
                ],
            }
    # Different source visibility is never pooled with source-bearing tasks.
    # The restricted mini-batch uses the empty intersection of its two scopes.
    context: dict[str, Any] = {
        "phase": "task_evaluation",
        "arm": arm,
        "batch": batch,
        "questions": [[case["id"], case["question"]] for case in cases],
        "documents": {
            id: [
                value["scope"],
                value["text"],
                [
                    [claim["id"], claim["value"], claim["unit"]]
                    for claim in value["claims"]
                ],
            ]
            for id, value in documents.items()
        },
        "reference_ids": sorted(documents),
        "evidence": [],
        "instructions": [
            "questions=[q,text]; documents[id]=[scope,text,claims[id,value,unit]].",
            "a: sma=short/long; mom=lookback/threshold_pct; "
            "weighted=fraction; fact=claim-ID.",
            "fact requires cited visible source; unavailable=insufficient; "
            "denied=refuse; unknown=pause.",
        ],
    }
    if arm == "CRITIC":
        context["review_candidates"] = [
            [value["q"], value["op"], value["a"], value["c"]]
            for value in (previous or [])
        ]
        context["instructions"].append(
            "Review candidates against visible sources; "
            "return corrected intentions, not self-scoring."
        )
    trace = {
        "hits": hits,
        "source_ids": sorted(documents),
        "scope_intersection": "EMPTY_FOR_RESTRICTED"
        if batch == "held-restricted"
        else "PRESENT",
        "expected_answers_supplied": False,
    }
    return context, trace


def _code_identity() -> str:
    import qf_lab

    lab = Path(qf_lab.__file__).parent
    files = (
        Path(__file__),
        Path(__file__).with_name("evaluation_cases.py"),
        Path(__file__).with_name("evaluation_grading.py"),
        Path(__file__).with_name("knowledge.py"),
        lab / "protocols.py",
        lab / "room_gateway.py",
        lab / "request_preview.py",
        lab / "retrieval.py",
        lab / "evaluation.py",
    )
    return digest(
        {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    )


def freeze(
    root: Path, corpus: KnowledgeCorpus, runtime: AgentRuntime
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    root = local_root(root)
    path = root / "plan.json"
    if path.exists():
        plan = json.loads(path.read_bytes())
        if (
            path.is_symlink()
            or digest(plan)
            != json.loads(path.with_name("plan-digest.json").read_bytes())["sha256"]
        ):
            raise ValueError("frozen evaluation manifest changed")
        if (
            plan["code_hash"] != _code_identity()
            or plan["corpus_hash"] != corpus.identity
        ):
            raise ValueError("frozen evaluation code or corpus changed")
        return cast(dict[str, Any], plan)
    data = suite(datetime.now(UTC))
    data["policy"]["model_batches"] = (
        "Development40, held-present18, held-restricted2 isolated; "
        "restricted batch has no source text. Tools reuse BM25 intents."
    )
    data["suite_hash"] = digest({"policy": data["policy"], "cases": data["cases"]})
    policy = runtime.budget()
    model = BindingRegistry(PROTECTED_BINDINGS).get(PROFILE_ID)
    plan = {
        "schema_version": "frozen-agent-evaluation-plan-v1",
        "suite": data,
        "corpus_hash": corpus.identity,
        "code_hash": _code_identity(),
        "model": {
            key: model[key]
            for key in (
                "id",
                "protocol",
                "default_model",
                "generation_mode",
                "input_price_per_million",
                "output_price_per_million",
                "cost_currency",
            )
        },
        "initial_goal_budget": policy,
        "initial_effective_budget": runtime.effective_budget(),
        "scoring_frozen_before_held": True,
        "no_retuning": True,
        "live_authority": "NONE",
    }
    write_exclusive(path, plan)
    write_exclusive(root / "plan-digest.json", {"sha256": digest(plan)})
    return plan


def preflight(
    plan: dict[str, Any], corpus: KnowledgeCorpus, runtime: AgentRuntime
) -> dict[str, Any]:
    model = plan["model"]
    preview = RequestPreview(
        protocol=model["protocol"],
        model=model["default_model"],
        generation_mode=model["generation_mode"],
    )
    entries: list[dict[str, Any]] = []
    for arm in PAID_ARMS:
        for batch, cases in parts(plan["suite"]["cases"]).items():
            previous = None
            if arm == "CRITIC":
                previous = [
                    {
                        "q": case["id"],
                        "op": "insufficient",
                        "a": "x" * 40,
                        "c": ["x" * 20, "y" * 20],
                    }
                    for case in cases
                ]
            context, _ = context_for(arm, batch, cases, corpus, previous=previous)
            context["evaluation_key"] = "x" * 64
            output = (
                1800
                if batch == "development"
                else 1000
                if batch == "held-present"
                else 256
            )
            transport = preview.estimate(context, EvaluationBatch, max_output=output)
            room_input = (
                len(canonical(context).encode())
                + len(canonical(EvaluationBatch.model_json_schema()).encode())
                + 2000
            )
            entries.append(
                {
                    "arm": arm,
                    "batch": batch,
                    "transport": transport,
                    "room_input": room_input,
                }
            )
    tokens = sum(
        max(item["transport"]["input"], item["room_input"])
        + item["transport"]["output"]
        for item in entries
    )
    cost = sum(
        (
            Decimal(max(item["transport"]["input"], item["room_input"]))
            * Decimal(model["input_price_per_million"])
            + Decimal(item["transport"]["output"])
            * Decimal(model["output_price_per_million"])
        )
        / 1_000_000
        for item in entries
    )
    effective = runtime.effective_budget()
    return {
        "entries": entries,
        "total_calls": len(entries),
        "conservative_tokens": tokens,
        "conservative_cost_usd": str(cost),
        "effective_budget": effective,
        "admitted": len(entries) <= effective["calls_remaining"]
        and tokens <= effective["tokens_remaining"]
        and cost <= Decimal(effective["cost_remaining"]),
        "model_calls_added": 0,
    }


def _expand(answer: EvaluationAnswer, context: dict[str, Any]) -> EvaluationAnswer:
    aliases = {
        f"E{index + 1}": id for index, id in enumerate(sorted(context["reference_ids"]))
    }
    return answer.model_copy(
        update={"c": tuple(aliases.get(id, id) for id in answer.c)}
    )


def _batch(
    root: Path,
    runtime: AgentRuntime,
    corpus: KnowledgeCorpus,
    arm: str,
    name: str,
    cases: list[dict[str, Any]],
    *,
    previous: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    path = root / (arm.lower() + "-" + name + ".json")
    context, retrieval = context_for(arm, name, cases, corpus, previous=previous)
    if path.exists():
        value = json.loads(path.read_bytes())
        if value["context_hash"] != digest(context):
            raise ValueError("retained batch context changed")
        return cast(dict[str, Any], value)
    key = digest(context)
    ledger = runtime.gateway.ledger()
    request = next(
        (
            item
            for item in ledger
            if item["phase"] == "REQUEST"
            and item.get("context", {}).get("evaluation_key") == key
        ),
        None,
    )
    started = time.perf_counter()
    context["evaluation_key"] = key
    answers, error, call_id = [], None, None
    if request:
        response = next(
            (
                item
                for item in ledger
                if item.get("call_id") == request["call_id"]
                and item["phase"] == "RESPONSE"
            ),
            None,
        )
        decision = (
            None
            if response is None
            else EvaluationBatch.model_validate(response["decision"])
        )
        if response is None:
            error = "EXISTING_MODEL_EFFECT_UNRESOLVED_NO_RETRY"
        call_id = request["call_id"]
    else:
        try:
            call_id, decision = runtime.gateway.call(
                "critic" if arm == "CRITIC" else "researcher",
                runtime.provider(),
                context,
                EvaluationBatch,
                max_output=1800
                if name == "development"
                else 1000
                if name == "held-present"
                else 256,
            )
        except (GatewayError, ValueError, OSError):
            error = "REAL_MODEL_BATCH_FAILED_OR_UNKNOWN_NO_FALLBACK"
            decision = None
            sent = next(
                (
                    item
                    for item in runtime.gateway.ledger()
                    if item["phase"] == "REQUEST"
                    and item.get("context", {}).get("evaluation_key") == key
                ),
                None,
            )
            call_id = sent["call_id"] if sent else None
    returned = []
    if decision is not None:
        ids = {case["id"] for case in cases}
        returned = [
            _expand(answer, context).model_dump(mode="json")
            for answer in decision.answers
        ]
        if {answer.q for answer in decision.answers} != ids:
            error = "QUESTION_SET_INCOMPLETE_OR_FOREIGN"
        answers = [answer for answer in returned if answer["q"] in ids]
    value = {
        "arm": arm,
        "batch": name,
        "context_hash": key,
        "call_id": call_id,
        "answers": answers,
        "returned_intentions": returned,
        "error": error,
        "duration_ms": (time.perf_counter() - started) * 1000,
        "retrieval": retrieval,
        "question_ids": [case["id"] for case in cases],
    }
    write_exclusive(path, value)
    return value


def run(root: Path = OUTPUT, *, preview_only: bool = False) -> dict[str, Any]:
    corpus, runtime = build_corpus(), AgentRuntime()
    root.mkdir(parents=True, exist_ok=True)
    with writer_lock(local_root(root)):
        existing_result = root / "result.json"
        if existing_result.exists() and not preview_only:
            retained = json.loads(existing_result.read_bytes())
            if (
                existing_result.is_symlink()
                or digest(retained)
                != json.loads(
                    existing_result.with_name("result-digest.json").read_bytes()
                )["sha256"]
            ):
                raise ValueError("retained evaluation result changed")
            return cast(dict[str, Any], retained)
        plan = freeze(root, corpus, runtime)
        estimate = preflight(plan, corpus, runtime)
        receipt = root / "initial-preflight.json"
        if not receipt.exists():
            write_exclusive(receipt, {"plan_id": digest(plan), **estimate})
        if preview_only:
            return {"plan_id": digest(plan), "preflight": estimate}
        if (
            not estimate["admitted"]
            and not (root / "single_model-development.json").exists()
        ):
            raise ValueError("whole frozen evaluation exceeds current inherited budget")
        cases = plan["suite"]["cases"]
        batches: dict[str, list[dict[str, Any]]] = {}
        results: dict[str, dict[str, EvaluationAnswer]] = {}
        for arm in PAID_ARMS:
            batches[arm] = []
            for name, subset in parts(cases).items():
                prior = (
                    next(
                        (
                            item["answers"]
                            for item in batches.get("BM25_MODEL", [])
                            if item["batch"] == name
                        ),
                        None,
                    )
                    if arm == "CRITIC"
                    else None
                )
                batches[arm].append(
                    _batch(root, runtime, corpus, arm, name, subset, previous=prior)
                )
            results[arm] = {
                answer["q"]: EvaluationAnswer.model_validate(answer)
                for item in batches[arm]
                for answer in item["answers"]
            }
        results["RULES"] = {case["id"]: rule_answer(case) for case in cases}
        results["BOUNDED_TOOLS"] = {
            case["id"]: bounded_answer(case, results["BM25_MODEL"][case["id"]], corpus)
            for case in cases
            if case["id"] in results["BM25_MODEL"]
        }
        results["MEMORY_ABLATION"] = {
            case["id"]: bounded_answer(
                case, results["MEMORY_ABLATION"][case["id"]], corpus, memory=True
            )
            for case in cases
            if case["id"] in results["MEMORY_ABLATION"]
        }
        evaluations = {}
        for arm in plan["suite"]["policy"]["arms"]:
            scores = [
                score(
                    case,
                    results[arm].get(case["id"]),
                    corpus,
                    memory=arm == "MEMORY_ABLATION",
                )
                for case in cases
            ]
            evaluations[arm] = {
                "passed": sum(item["passed"] for item in scores),
                "total": 60,
                "development_passed": sum(
                    item["passed"]
                    for item, case in zip(scores, cases, strict=True)
                    if case["split"] == "DEVELOPMENT"
                ),
                "held_passed": sum(
                    item["passed"]
                    for item, case in zip(scores, cases, strict=True)
                    if case["split"] == "HELD"
                ),
                "citation_errors": sum(item["citation_errors"] for item in scores),
                "citation_count": sum(item["citation_count"] for item in scores),
                "missing_answers": sum(item["missing_answer"] for item in scores),
                "claim_errors": sum(item["claim_error"] for item in scores),
                "categories": dict(
                    Counter(item["category"] for item in scores if not item["passed"])
                ),
                "scores": scores,
            }
        result = {
            "schema_version": "agent-evaluation-v1",
            "plan_id": digest(plan),
            "corpus_hash": corpus.identity,
            "evaluations": evaluations,
            "batches": batches,
            "budget_after": runtime.budget(),
            "effective_budget_after": runtime.effective_budget(),
            "qualification": "AUTHORED_SMALL_TASK_SUITE_NOT_PRODUCTION_GENERALIZATION",
            "validated_alpha": False,
            "live_authority": "NONE",
        }
        path = root / "result.json"
        if not path.exists():
            write_exclusive(path, result)
            write_exclusive(root / "result-digest.json", {"sha256": digest(result)})
        return result
