from .models import NormalizedReview, NormalizedVerdict, ReviewResult


def normalize_review(result: ReviewResult) -> NormalizedReview:
    if result.blocking_findings:
        verdict = NormalizedVerdict.CHANGES_REQUIRED
    elif result.non_blocking_findings:
        verdict = NormalizedVerdict.APPROVE_WITH_MINOR_FINDINGS
    else:
        verdict = NormalizedVerdict.APPROVE
    return NormalizedReview(
        result.reviewer,
        result.review_commit,
        result.raw_verdict,
        verdict,
        result.blocking_findings,
        result.non_blocking_findings,
    )


def review_matches_sha(review: ReviewResult | NormalizedReview, sha: str) -> bool:
    return review.review_commit == sha
