from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from qf_lab.evaluation import EvaluationBatch
from qf_lab.retrieval import KnowledgeCorpus, KnowledgeDocument

from qf_platform.agent_evaluation import _batch, context_for, parts
from qf_platform.evaluation_cases import suite


def data(root: Path) -> KnowledgeCorpus:
    return KnowledgeCorpus.publish(
        root,
        (
            KnowledgeDocument(
                "sample",
                "Sample SMA",
                "SMA uses past observations and next-open timing only.",
                ("SMA",),
                datetime(2020, 1, 1, tzinfo=UTC),
                "DEVELOPMENT",
                "OWNER_AUTHORIZED_LOCAL_PROJECT_CONTENT",
                "a" * 64,
                "original test source",
            ),
        ),
    )


def test_context_never_supplies_reference_answers_or_pools_restricted_sources(
    tmp_path: Path,
) -> None:
    corpus = data(tmp_path)
    groups = parts(suite(datetime.now(UTC))["cases"])
    assert [len(value) for value in groups.values()] == [40, 18, 2]
    context, trace = context_for(
        "BM25_MODEL", "held-restricted", groups["held-restricted"], corpus
    )
    assert context["documents"] == {} and context["reference_ids"] == []
    assert "expected" not in str(context) and not trace["expected_answers_supplied"]


def test_retained_batch_never_calls_again_and_keeps_partial_model_outputs(
    tmp_path: Path,
) -> None:
    corpus = data(tmp_path / "corpus")
    cases = [
        {
            "id": "T01",
            "question": "SMA",
            "as_of": datetime.now(UTC).isoformat(),
            "visibility": "PRESENT",
        },
        {
            "id": "T02",
            "question": "SMA",
            "as_of": datetime.now(UTC).isoformat(),
            "visibility": "PRESENT",
        },
    ]

    class Gateway:
        count = 0

        def ledger(self):
            return []

        def call(self, role, provider, context, schema, **kwargs):
            self.count += 1
            return "fixture-call", EvaluationBatch.model_validate(
                {
                    "evidence_refs": [],
                    "answers": [{"q": "T01", "op": "insufficient", "a": "", "c": []}],
                }
            )

    class Runtime:
        gateway = Gateway()

        def provider(self):
            return object()

    runtime = Runtime()
    root = tmp_path / "run"
    root.mkdir()
    first = _batch(root, runtime, corpus, "SINGLE_MODEL", "development", cases)  # type: ignore[arg-type]
    assert first["error"] == "QUESTION_SET_INCOMPLETE_OR_FOREIGN"
    assert len(first["returned_intentions"]) == len(first["answers"]) == 1
    assert _batch(root, runtime, corpus, "SINGLE_MODEL", "development", cases) == first  # type: ignore[arg-type]
    assert runtime.gateway.count == 1
