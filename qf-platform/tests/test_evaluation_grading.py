from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from qf_lab.evaluation import EvaluationAnswer, EvaluationBatch
from qf_lab.retrieval import KnowledgeCorpus, KnowledgeDocument

from qf_platform.evaluation_cases import suite
from qf_platform.evaluation_grading import bounded_answer, score

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def corpus(root: Path) -> KnowledgeCorpus:
    claim = {
        "id": "sample_return",
        "value": "-0.1",
        "unit": "fraction",
        "basis": "TEST_ONLY",
    }
    doc = KnowledgeDocument(
        "sample",
        "Synthetic test fact",
        "This is only a test fact, not actual market evidence.",
        ("sample",),
        NOW,
        "DEVELOPMENT",
        "OWNER_AUTHORIZED_LOCAL_PROJECT_CONTENT",
        "a" * 64,
        "test source",
        (claim,),
    )
    return KnowledgeCorpus.publish(root, (doc,))


def case() -> dict:
    return {
        "id": "N01",
        "category": "N",
        "question": "Test source only",
        "visibility": "PRESENT",
        "as_of": NOW.isoformat(),
        "expected": {"op": "fact", "a": "sample_return", "support": "sample"},
    }


def test_exact_claim_citation_and_absence_not_model_judged(tmp_path: Path) -> None:
    data = corpus(tmp_path)
    assert score(
        case(),
        EvaluationAnswer(q="N01", op="fact", a="sample_return", c=("sample",)),
        data,
    )["passed"]
    assert not score(
        case(), EvaluationAnswer(q="N01", op="fact", a="sample_return"), data
    )["passed"]
    assert score(
        case(), EvaluationAnswer(q="N01", op="fact", a="invented", c=("sample",)), data
    )["claim_error"]
    assert score(case(), None, data)["missing_answer"]
    assert not score(
        {**case(), "visibility": "PAST"},
        EvaluationAnswer(q="N01", op="fact", a="sample_return", c=("sample",)),
        data,
    )["passed"]


def test_tool_resolves_real_source_but_cannot_make_an_unknown_claim(
    tmp_path: Path,
) -> None:
    data = corpus(tmp_path)
    result = bounded_answer(
        case(),
        EvaluationAnswer(q="N01", op="fact", a="sample_return", c=("invented",)),
        data,
    )
    assert result.c == ("sample",)
    assert score(case(), result, data)["resolved_fact"]["value"] == "-0.1"
    assert (
        bounded_answer(
            case(), EvaluationAnswer(q="N01", op="fact", a="unknown"), data
        ).op
        == "insufficient"
    )
    assert (
        bounded_answer(case(), EvaluationAnswer(q="N01", op="sma", a="20/5"), data).op
        == "refuse"
    )


def test_batch_has_no_metric_or_authority_fields_and_exact_unique_ids() -> None:
    with pytest.raises(ValueError):
        EvaluationAnswer.model_validate(
            {"q": "N01", "op": "fact", "a": "sample", "metrics": {"return": 9}}
        )
    answer = {"q": "N01", "op": "insufficient"}
    with pytest.raises(ValueError):
        EvaluationBatch.model_validate(
            {"evidence_refs": [], "answers": [answer, answer]}
        )


def test_suite_structure_is_exact_without_scoring_held_cases() -> None:
    data = suite(NOW)
    assert len(data["cases"]) == 60
    assert len({case["id"] for case in data["cases"]}) == 60
    assert sum(case["split"] == "DEVELOPMENT" for case in data["cases"]) == 40
    assert sum(case["split"] == "HELD" for case in data["cases"]) == 20
    assert data["policy"]["retuning_after_first_result"] == "FORBIDDEN_IN_V1"
