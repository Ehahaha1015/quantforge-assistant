from __future__ import annotations

import json
from typing import Any

from .models import (
    FindingClassification,
    ReviewerKind,
    ReviewFinding,
    ReviewResult,
    TransportAttempt,
    TransportStatus,
)


class ReviewParseError(ValueError):
    """The reviewer process returned bytes that are not JSON."""


class ReviewSchemaError(ValueError):
    """The reviewer JSON is valid but violates the review contract."""


def parse_review(
    payload: str | dict[str, Any], reviewer: ReviewerKind, expected_sha: str
) -> ReviewResult:
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as exc:
        raise ReviewParseError("invalid review JSON") from exc
    if not isinstance(data, dict) or data.get("review_commit") != expected_sha:
        raise ReviewSchemaError("review schema or SHA mismatch")

    def findings(key: str) -> tuple[ReviewFinding, ...]:
        output = []
        entries = data.get(key, [])
        if not isinstance(entries, list):
            raise ReviewSchemaError(f"{key} must be an array")
        for item in entries:
            if not isinstance(item, dict):
                raise ReviewSchemaError("finding must be object")
            for required in ("id", "classification", "title"):
                if not isinstance(item.get(required), str) or not item[required]:
                    raise ReviewSchemaError(f"finding field {required!r} is required")
            if "confirmed" in item and not isinstance(item["confirmed"], bool):
                raise ReviewSchemaError("finding field 'confirmed' must be boolean")
            for optional in (
                "severity",
                "file",
                "evidence",
                "why_it_matters",
                "remediation",
            ):
                if optional in item and not isinstance(item[optional], str):
                    raise ReviewSchemaError(
                        f"finding field {optional!r} must be string"
                    )
            try:
                classification = FindingClassification(item["classification"])
            except ValueError as exc:
                raise ReviewSchemaError(
                    f"unknown finding classification: {item['classification']!r}"
                ) from exc
            output.append(
                ReviewFinding(
                    id=item["id"],
                    severity=item.get("severity", ""),
                    classification=classification,
                    title=item["title"],
                    file=str(item.get("file", "")),
                    evidence=str(item.get("evidence", "")),
                    why_it_matters=str(item.get("why_it_matters", "")),
                    remediation=str(item.get("remediation", "")),
                    confirmed=bool(item.get("confirmed", False)),
                )
            )
        return tuple(output)

    if not isinstance(data.get("raw_verdict"), str):
        raise ReviewSchemaError("raw_verdict required")
    recommendation = data.get("merge_recommendation", "")
    if not isinstance(recommendation, str):
        raise ReviewSchemaError("merge_recommendation must be a string")
    return ReviewResult(
        reviewer,
        expected_sha,
        data["raw_verdict"],
        findings("blocking_findings"),
        findings("non_blocking_findings"),
        recommendation,
    )


def classify_process(
    returncode: int | None, timed_out: bool = False, disconnected: bool = False
) -> TransportStatus:
    if timed_out:
        return TransportStatus.TIMEOUT
    if disconnected:
        return TransportStatus.UPSTREAM_DISCONNECT
    if returncode not in (0, None):
        return TransportStatus.PROCESS_ERROR
    return TransportStatus.SUCCESS


class ReviewAdapter:
    reviewer: ReviewerKind

    def review(
        self, prompt: str, worktree: str, sha: str, timeout: float
    ) -> tuple[ReviewResult | None, TransportAttempt]:
        raise NotImplementedError
