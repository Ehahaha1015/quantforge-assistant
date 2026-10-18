from quantforge.autolab.models import (
    FindingClassification,
    ReviewerKind,
    ReviewFinding,
    ReviewResult,
    TransportStatus,
)
from quantforge.autolab.state import normalize_review, review_matches_sha
from quantforge.autolab.triage import triage


def finding(
    id: str, classification: FindingClassification, confirmed: bool = False
) -> ReviewFinding:
    return ReviewFinding(id, "high", classification, id, confirmed=confirmed)


def test_finding_arrays_override_raw_verdict() -> None:
    result = ReviewResult(ReviewerKind.DEEPSEEK, "abc", "changes_required")
    assert normalize_review(result).verdict.value == "APPROVE"
    result = ReviewResult(
        ReviewerKind.DEEPSEEK,
        "abc",
        "approve",
        (),
        (finding("h", FindingClassification.HARDENING),),
    )
    assert normalize_review(result).verdict.value == "APPROVE_WITH_MINOR_FINDINGS"


def test_triage_confirmation_and_deferred() -> None:
    result = triage(
        [
            finding("a", FindingClassification.CORRECTNESS_DEFECT),
            finding("b", FindingClassification.CONTRACT_VIOLATION, True),
            finding("c", FindingClassification.FUTURE_DESIGN),
        ]
    )
    assert [f.id for f in result.unconfirmed_blockers] == ["a"]
    assert [f.id for f in result.confirmed_blockers] == ["b"]
    assert [f.id for f in result.deferred_findings] == ["c"]


def test_sha_identity() -> None:
    review = ReviewResult(ReviewerKind.CODEX, "abc", "approve")
    assert review_matches_sha(review, "abc")
    assert not review_matches_sha(review, "def")


def test_transport_is_not_semantic() -> None:
    assert TransportStatus.TIMEOUT.value == "timeout"
