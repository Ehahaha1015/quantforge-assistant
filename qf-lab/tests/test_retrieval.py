from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qf_lab.retrieval import KnowledgeCorpus, KnowledgeDocument, Visibility

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def document(id: str = "sma", **changes: object) -> KnowledgeDocument:
    values = {
        "id": id,
        "title": "Causal moving average SMA",
        "text": "SMA means a past-only moving average. Threshold units and fees are declared.",
        "keywords": ("SMA", "均线", "moving average"),
        "available_at": NOW,
        "scope": "DEVELOPMENT",
        "permission": "CC0_ORIGINAL_PROJECT_NOTE",
        "source_identity": "a" * 64,
        "source_label": "original test note",
        "claims": (),
    }
    return KnowledgeDocument(**{**values, **changes})  # type: ignore[arg-type]


def test_native_bm25_chinese_keywords_and_exact_immutable_sources(
    tmp_path: Path,
) -> None:
    corpus = KnowledgeCorpus.publish(
        tmp_path,
        (
            document(),
            document(
                "fees",
                title="Switch fees",
                text="A basis point is 0.01 percent. Commission is not switch fee.",
                keywords=("fees", "费用"),
            ),
        ),
    )
    assert corpus.search("均线", Visibility(NOW))["matches"][0]["id"] == "sma"
    assert (
        corpus.search("SMA moving average", Visibility(NOW))["matches"][0]["id"]
        == "sma"
    )
    assert (
        KnowledgeCorpus.publish(tmp_path, corpus.documents).identity == corpus.identity
    )
    with pytest.raises(ValueError, match="cannot change"):
        KnowledgeCorpus.publish(
            tmp_path,
            (
                document(
                    text="A revised source must get a separate new corpus identity."
                ),
            ),
        )


def test_hidden_future_and_opinion_documents_do_not_influence_ranking(
    tmp_path: Path,
) -> None:
    base, expanded = tmp_path / "base", tmp_path / "expanded"
    public = (
        document(),
        document(
            "fees",
            title="Fee units",
            text="Switch fees matter for a moving average strategy with turnover.",
        ),
    )
    first = KnowledgeCorpus.publish(base, public)
    second = KnowledgeCorpus.publish(
        expanded,
        (
            *public,
            document("final", scope="FINAL_PROTECTED", text="SMA " * 100),
            document("future", available_at=NOW + timedelta(days=1), text="SMA " * 100),
            document(
                "opinion",
                scope="OPINION",
                text="SMA was my preferred historical opinion, not fact or permission.",
            ),
        ),
    )
    assert first.search("SMA", Visibility(NOW)) == second.search("SMA", Visibility(NOW))
    assert "opinion" in {
        item["id"]
        for item in second.search(
            "SMA", Visibility(NOW, include_memory=True), limit=10
        )["matches"]
    }
    assert not second.search("SMA", Visibility(NOW - timedelta(days=1)))["matches"]


def test_citation_scope_identity_and_claim_ownership(tmp_path: Path) -> None:
    claim = {"id": "fee", "value": "10", "unit": "bps", "basis": "DECLARED_SWITCH"}
    corpus = KnowledgeCorpus.publish(tmp_path, (document(claims=(claim,)),))
    found = corpus.search("SMA", Visibility(NOW))["matches"][0]
    with pytest.raises(TypeError):
        corpus.documents[0].claims[0]["value"] = "999"  # type: ignore[index]
    assert (
        corpus.verify_citation(
            "sma", found["content_hash"], Visibility(NOW), claim_id="fee"
        )["claim"]
        == claim
    )
    for policy, hash, id in (
        (Visibility(NOW, document_ids=frozenset()), found["content_hash"], "fee"),
        (Visibility(NOW), "b" * 64, "fee"),
        (Visibility(NOW), found["content_hash"], "invented"),
    ):
        with pytest.raises(ValueError):
            corpus.verify_citation("sma", hash, policy, claim_id=id)


def test_permission_corruption_and_query_syntax_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        document(permission="UNKNOWN")
    corpus = KnowledgeCorpus.publish(tmp_path, (document(),))
    assert isinstance(
        corpus.search('" OR * DROP TABLE documents --', Visibility(NOW))["matches"],
        list,
    )
    with pytest.raises(ValueError):
        corpus.search("SMA", Visibility(NOW), limit=True)
    value = json.loads((tmp_path / "corpus.json").read_text())
    value["documents"][0]["text"] = "coherent-looking changed body"
    (tmp_path / "corpus.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="identity"):
        KnowledgeCorpus(tmp_path)
