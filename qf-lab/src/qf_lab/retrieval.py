"""Immutable local corpus and native BM25 over only admissible source documents."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from quantforge.product.evidence import evidence_bytes
from quantforge.product.storage import local_root, write_exclusive

from .gateway import canonical

_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
PERMISSIONS = {"OWNER_AUTHORIZED_LOCAL_PROJECT_CONTENT", "CC0_ORIGINAL_PROJECT_NOTE"}
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "for",
        "on",
        "in",
        "is",
        "are",
        "was",
        "were",
        "it",
        "this",
        "that",
        "with",
        "and",
        "or",
        "from",
        "using",
        "use",
        "show",
        "please",
        "what",
        "which",
        "how",
        "does",
        "can",
        "may",
        "must",
    ]
)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class KnowledgeDocument:
    id: str
    title: str
    text: str
    keywords: tuple[str, ...]
    available_at: datetime
    scope: Literal["DEVELOPMENT", "OPINION", "FINAL_PROTECTED"]
    permission: Literal[
        "OWNER_AUTHORIZED_LOCAL_PROJECT_CONTENT", "CC0_ORIGINAL_PROJECT_NOTE"
    ]
    source_identity: str
    source_label: str
    claims: tuple[Mapping[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (
            not _ID.fullmatch(self.id)
            or not 1 <= len(self.title) <= 160
            or not 10 <= len(self.text) <= 8000
            or len(self.keywords) > 30
            or any(
                not isinstance(value, str) or not 1 <= len(value) <= 80
                for value in self.keywords
            )
            or self.available_at.tzinfo is None
            or self.available_at.utcoffset() is None
            or self.scope not in {"DEVELOPMENT", "OPINION", "FINAL_PROTECTED"}
            or self.permission not in PERMISSIONS
            or not _HASH.fullmatch(self.source_identity)
            or not 1 <= len(self.source_label) <= 250
        ):
            raise ValueError("bounded licensed source document required")
        if len(self.claims) > 30 or (self.scope != "DEVELOPMENT" and self.claims):
            raise ValueError("only development documents can carry computed claims")
        for claim in self.claims:
            if (
                set(claim) != {"id", "value", "unit", "basis"}
                or not _ID.fullmatch(claim["id"])
                or any(
                    not isinstance(value, str) or len(value) > 100
                    for value in claim.values()
                )
            ):
                raise ValueError("source-bound closed claim fields required")
        if len({value["id"] for value in self.claims}) != len(self.claims):
            raise ValueError("duplicate claim identity")
        object.__setattr__(
            self,
            "claims",
            tuple(MappingProxyType(dict(value)) for value in self.claims),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "text": self.text,
            "keywords": list(self.keywords),
            "available_at": self.available_at.astimezone(UTC).isoformat(),
            "scope": self.scope,
            "permission": self.permission,
            "source_identity": self.source_identity,
            "source_label": self.source_label,
            "claims": [dict(value) for value in self.claims],
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> KnowledgeDocument:
        return cls(
            **{
                **value,
                "available_at": datetime.fromisoformat(value["available_at"]),
                "keywords": tuple(value["keywords"]),
                "claims": tuple(value["claims"]),
            }
        )


@dataclass(frozen=True)
class Visibility:
    as_of: datetime
    include_memory: bool = False
    document_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if (
            self.as_of.tzinfo is None
            or self.as_of.utcoffset() is None
            or type(self.include_memory) is not bool
            or (
                self.document_ids is not None
                and any(not _ID.fullmatch(value) for value in self.document_ids)
            )
        ):
            raise ValueError("aware host visibility declaration required")

    def admits(self, document: KnowledgeDocument) -> bool:
        return (
            document.scope != "FINAL_PROTECTED"
            and document.available_at <= self.as_of
            and (document.scope != "OPINION" or self.include_memory)
            and (self.document_ids is None or document.id in self.document_ids)
        )


class KnowledgeCorpus:
    def __init__(self, root: Path) -> None:
        self.root = local_root(root)
        self.path = self.root / "corpus.json"
        if (
            self.path.is_symlink()
            or not self.path.is_file()
            or self.path.stat().st_size > 2_000_000
        ):
            raise ValueError("immutable bounded corpus required")
        raw = self.path.read_bytes()
        value = json.loads(raw)
        if (
            raw != evidence_bytes(value)
            or value["schema_version"] != "source-corpus-v1"
            or _digest(value["documents"]) != value["corpus_hash"]
        ):
            raise ValueError("corpus identity mismatch")
        self.documents = tuple(
            KnowledgeDocument.from_json(item) for item in value["documents"]
        )
        if len(self.documents) > 200 or len(
            {item.id for item in self.documents}
        ) != len(self.documents):
            raise ValueError("bounded distinct corpus required")
        self.identity = value["corpus_hash"]

    @classmethod
    def publish(
        cls, root: Path, documents: tuple[KnowledgeDocument, ...]
    ) -> KnowledgeCorpus:
        if (
            not documents
            or len(documents) > 200
            or len({item.id for item in documents}) != len(documents)
        ):
            raise ValueError("bounded distinct documents required")
        root.mkdir(parents=True, exist_ok=True)
        root = local_root(root)
        values = [
            item.to_json() for item in sorted(documents, key=lambda item: item.id)
        ]
        value = {
            "schema_version": "source-corpus-v1",
            "documents": values,
            "corpus_hash": _digest(values),
        }
        path = root / "corpus.json"
        if path.exists():
            if path.is_symlink() or path.read_bytes() != evidence_bytes(value):
                raise ValueError("frozen corpus cannot change")
        else:
            write_exclusive(path, value)
        return cls(root)

    def visible(self, policy: Visibility) -> tuple[KnowledgeDocument, ...]:
        return tuple(item for item in self.documents if policy.admits(item))

    def search(
        self, query: str, policy: Visibility, *, limit: int = 3
    ) -> dict[str, Any]:
        if (
            not isinstance(query, str)
            or not 1 <= len(query.strip()) <= 500
            or type(limit) is not int
            or not 1 <= limit <= 10
        ):
            raise ValueError("bounded lexical query required")
        allowed = self.visible(policy)
        terms = {
            word.lower()
            for word in re.findall(r"[A-Za-z0-9_]+", query)
            if word.lower() not in STOPWORDS
        }
        # Explicit Chinese keywords supplement native unicode61, not a hidden NLP model.
        terms.update(
            word.lower() for item in allowed for word in item.keywords if word in query
        )
        terms.update(
            word for word in re.findall(r"[\u3400-\u9fff]+", query) if len(word) <= 6
        )
        terms = set(sorted(terms)[:24])
        matches = []
        if terms and allowed:
            connection = sqlite3.connect(":memory:")
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE documents USING fts5(id UNINDEXED,title,keywords,body,tokenize='unicode61')"
                )
                connection.executemany(
                    "INSERT INTO documents VALUES(?,?,?,?)",
                    [
                        (item.id, item.title, " ".join(item.keywords), item.text)
                        for item in allowed
                    ],
                )
                literal = " OR ".join(
                    '"' + term.replace('"', '""') + '"' for term in sorted(terms)
                )
                found = connection.execute(
                    "SELECT id,bm25(documents,0,3,4,1) FROM documents WHERE documents MATCH ? ORDER BY 2,id LIMIT ?",
                    (literal, limit),
                ).fetchall()
                lookup = {item.id: item for item in allowed}
                matches = [
                    {
                        **lookup[id].to_json(),
                        "content_hash": _digest(lookup[id].to_json()),
                        "bm25_score": score,
                        "authority": "HISTORICAL_OPINION_NOT_FACT_OR_PERMISSION"
                        if lookup[id].scope == "OPINION"
                        else "SOURCE_BOUND_PROJECT_NOTE_OR_COMPUTED_FACT",
                    }
                    for id, score in found
                ]
            finally:
                connection.close()
        return {
            "schema_version": "visible-bm25-search-v1",
            "query": query,
            "ranking": "SQLITE_FTS5_BM25_FILTER_BEFORE_INDEX",
            "as_of": policy.as_of.astimezone(UTC).isoformat(),
            "matches": matches,
            "visible_source_count": len(allowed),
            "validated_alpha": False,
            "live_authority": "NONE",
        }

    def verify_citation(
        self,
        document_id: str,
        content_hash: str,
        policy: Visibility,
        *,
        claim_id: str | None = None,
    ) -> dict[str, Any]:
        document = next(
            (item for item in self.visible(policy) if item.id == document_id), None
        )
        if document is None or _digest(document.to_json()) != content_hash:
            raise ValueError("citation not visible or identity mismatch")
        claim = (
            None
            if claim_id is None
            else next(
                (value for value in document.claims if value["id"] == claim_id), None
            )
        )
        if claim_id is not None and claim is None:
            raise ValueError("claim does not belong to visible cited source")
        return {
            "document_id": document.id,
            "source_identity": document.source_identity,
            "scope": document.scope,
            "claim": dict(claim) if claim is not None else None,
            "valid": True,
            "grants_authority": False,
        }
