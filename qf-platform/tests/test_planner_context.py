from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from qf_lab.bounded_planner import plan_task
from test_agent_runtime import Planner, runtime

from qf_platform.agent_tools import DATASET
from qf_platform.planner_context import compact_dataset, compact_knowledge
from qf_platform.storage import PlatformError, digest


def knowledge() -> dict[str, Any]:
    return {
        "ranking": "SQLITE_FTS5_BM25_FILTER_BEFORE_INDEX",
        "as_of": "2026-10-15T00:00:00Z",
        "grants_authority": False,
        "sources": [{
            "id": "fixture", "text": "TEST ONLY exact source text",
            "scope": "DEVELOPMENT", "claims": [{
                "id": "fee", "value": "0.001", "unit": "fraction",
                "basis": "DECLARED_SWITCH",
            }],
            "content_hash": "1" * 64, "source_identity": "2" * 64,
            "permission": "OWNER_AUTHORIZED_LOCAL_PROJECT_CONTENT",
            "available_at": "2020-01-01T00:00:00Z",
        }],
    }


def test_compact_replay_preserves_full_provenance_and_exact_text_and_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Planner()
    host = runtime(tmp_path, provider)
    original = host.tools.inspect_dataset(DATASET)
    notes = knowledge()
    host._test_provider = None
    monkeypatch.setattr(host, "provider", lambda: provider)
    monkeypatch.setattr(
        "qf_platform.knowledge.query_corpus",
        lambda *args, **kwargs: {**notes, "matches": notes["sources"]},
    )
    task, goal = "task-" + "d" * 32, "Compare synthetic fixtures and source notes."
    result = host.plan(task, goal, 2)
    wire = provider.contexts[0]
    assert wire["dataset"] == compact_dataset(original)
    assert wire["knowledge"] == compact_knowledge(notes)
    assert wire["knowledge"]["sources"][0]["text"] == notes["sources"][0]["text"]
    assert wire["knowledge"]["sources"][0]["claims"] == notes["sources"][0]["claims"]
    assert "source_identity" not in wire["knowledge"]["sources"][0]
    assert result["dataset_metadata"] == original
    assert result["knowledge"] == notes
    assert result["planner"] == "DETERMINISTIC_TEST_ONLY"
    assert result["call"]["paid_model_calls"] == 0
    assert host.call_state(task) == result["call"]
    assert host.plan(task, goal, 2) == result and provider.count == 1
    rows = host.gateway.ledger()
    request = rows[0]
    assert request["schema"] == "CompactResearchPlan"
    assert request["audit_context"] == {"dataset": original, "knowledge": notes}
    # Even a recomputed audit hash cannot rebind omitted provenance to another view.
    changed = copy.deepcopy(rows)
    changed[0]["audit_context"]["knowledge"]["sources"][0]["permission"] = "CHANGED"
    changed[0]["audit_context_hash"] = digest(changed[0]["audit_context"])
    monkeypatch.setattr(host.gateway, "ledger", lambda: changed)
    with pytest.raises(PlatformError, match="PROVENANCE_REPLAY_CONFLICT"):
        host.plan(task, goal, 2)
    assert provider.count == 1


def test_preexisting_full_bm25_request_recovers_without_retrieval_or_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Planner()
    host = runtime(tmp_path, provider)
    dataset, notes = host.tools.inspect_dataset(DATASET), knowledge()
    task, goal = "task-" + "e" * 32, "Recover a prior full context planner request."
    plan_task(
        host.gateway, provider, goal=goal, dataset=dataset,
        reference_ids=("dataset-meta-" + digest(dataset),),
        max_experiments=2, task_id=task, knowledge=notes,
    )
    monkeypatch.setattr(host, "preflight", lambda *args: pytest.fail("replayed send"))
    result = host.plan(task, goal, 2)
    assert result["dataset_metadata"] == dataset and result["knowledge"] == notes
    assert provider.count == 1
