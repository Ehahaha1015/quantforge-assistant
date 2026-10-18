from pathlib import Path

from quantforge.autolab.models import (
    CIResult,
    CommandResult,
    FindingClassification,
    ReviewerKind,
    ReviewFinding,
    ReviewResult,
    TaskSpec,
    TransportAttempt,
    TransportStatus,
)
from quantforge.autolab.orchestrator import Orchestrator


class FakeGit:
    def __init__(self, valid=True, integrity_error_at=None):
        self.removed = []
        self.valid = valid
        self.integrity_error_at = integrity_error_at
        self.integrity_calls = 0

    def head(self):
        return "sha-1"

    def clean(self):
        return True

    def create_detached_worktree(self, path, sha):
        Path(path).mkdir(parents=True, exist_ok=True)

    def remove_worktree(self, path):
        self.removed.append(path)

    def integrity(self, path, sha):
        from quantforge.autolab.git import GitError, WorktreeIntegrity

        self.integrity_calls += 1
        if self.integrity_calls == self.integrity_error_at:
            raise GitError("probe failed")
        return WorktreeIntegrity(self.valid, False, False, ())


class FakeProvider:
    def __init__(self, review, attempts=1, reviewer=None):
        self.review_value = review
        self.attempts = attempts
        self.calls = 0
        self.reviewer = reviewer or (
            review.reviewer if review else ReviewerKind.DEEPSEEK
        )

    def review(self, prompt, worktree, sha, timeout):
        self.calls += 1
        if self.calls < self.attempts:
            return None, TransportAttempt(self.calls, TransportStatus.TIMEOUT)
        return self.review_value, TransportAttempt(self.calls, TransportStatus.SUCCESS)


def ci():
    return CIResult((CommandResult("all", ("fake",), 0),))


def test_complete_and_successful_retry(tmp_path: Path):
    ds = ReviewResult(ReviewerKind.DEEPSEEK, "sha-1", "approve")
    cx = ReviewResult(ReviewerKind.CODEX, "sha-1", "approve")
    orch = Orchestrator(
        tmp_path, tmp_path / "runs", ci_runner=lambda _: ci(), git=FakeGit()
    )
    result = orch.run(
        TaskSpec("T", "tasks/T.md"), FakeProvider(ds, 2), FakeProvider(cx)
    )
    assert result.state.value == "COMPLETE"
    assert result.transport_attempts["deepseek"][0].status == TransportStatus.TIMEOUT


def test_fix_required_writes_machine_request(tmp_path: Path):
    finding = ReviewFinding(
        "b", "high", FindingClassification.CORRECTNESS_DEFECT, "bug", confirmed=True
    )
    review = ReviewResult(ReviewerKind.DEEPSEEK, "sha-1", "approve", (finding,))
    clean = ReviewResult(ReviewerKind.CODEX, "sha-1", "approve")
    orch = Orchestrator(
        tmp_path, tmp_path / "runs", ci_runner=lambda _: ci(), git=FakeGit()
    )
    result = orch.run(
        TaskSpec("T", "tasks/T.md"), FakeProvider(review), FakeProvider(clean)
    )
    assert result.state.value == "FIX_REQUIRED"
    assert list((tmp_path / "runs" / "T").rglob("fix_request.json"))


def test_stalled_after_transport_exhaustion(tmp_path: Path):
    review = ReviewResult(ReviewerKind.DEEPSEEK, "sha-1", "approve")
    orch = Orchestrator(
        tmp_path, tmp_path / "runs", ci_runner=lambda _: ci(), git=FakeGit()
    )
    result = orch.run(
        TaskSpec("T", "tasks/T.md"),
        FakeProvider(None, 9),
        FakeProvider(review, reviewer=ReviewerKind.CODEX),
        max_attempts=2,
    )
    assert result.state.value == "STALLED"
    assert len(result.transport_attempts["deepseek"]) == 2


def test_integrity_failure_stalls_before_review(tmp_path: Path):
    review = ReviewResult(ReviewerKind.DEEPSEEK, "sha-1", "approve")
    git = FakeGit(valid=False)
    orch = Orchestrator(tmp_path, tmp_path / "runs", ci_runner=lambda _: ci(), git=git)
    result = orch.run(
        TaskSpec("T", "tasks/T.md"),
        FakeProvider(review),
        FakeProvider(review, reviewer=ReviewerKind.CODEX),
    )
    assert result.state.value == "STALLED"


def test_wrong_sha_review_is_retried_and_stalls(tmp_path: Path):
    wrong = ReviewResult(ReviewerKind.DEEPSEEK, "other-sha", "approve")
    codex = ReviewResult(ReviewerKind.CODEX, "sha-1", "approve")
    orch = Orchestrator(
        tmp_path, tmp_path / "runs", ci_runner=lambda _: ci(), git=FakeGit()
    )
    result = orch.run(
        TaskSpec("T", "tasks/T.md"),
        FakeProvider(wrong),
        FakeProvider(codex),
        max_attempts=2,
    )
    assert result.state.value == "STALLED"
    assert all(
        attempt.status.value == "schema_error"
        for attempt in result.transport_attempts["deepseek"]
    )


def test_failed_transport_never_accepts_semantic_review(tmp_path: Path):
    review = ReviewResult(ReviewerKind.DEEPSEEK, "sha-1", "approve")

    class InconsistentProvider(FakeProvider):
        def review(self, *args):
            return review, TransportAttempt(1, TransportStatus.TIMEOUT)

    codex = ReviewResult(ReviewerKind.CODEX, "sha-1", "approve")
    orch = Orchestrator(
        tmp_path, tmp_path / "runs", ci_runner=lambda _: ci(), git=FakeGit()
    )
    result = orch.run(
        TaskSpec("T", "tasks/T.md"),
        InconsistentProvider(review),
        FakeProvider(codex),
        max_attempts=2,
    )
    assert result.state.value == "STALLED"


def test_post_review_integrity_error_stalls(tmp_path: Path):
    review = ReviewResult(ReviewerKind.DEEPSEEK, "sha-1", "approve")
    codex = ReviewResult(ReviewerKind.CODEX, "sha-1", "approve")
    orch = Orchestrator(
        tmp_path,
        tmp_path / "runs",
        ci_runner=lambda _: ci(),
        git=FakeGit(integrity_error_at=2),
    )
    result = orch.run(
        TaskSpec("T", "tasks/T.md"), FakeProvider(review), FakeProvider(codex)
    )
    assert result.state.value == "STALLED"


def test_non_blocking_array_cannot_be_promoted_to_blocker(tmp_path: Path):
    finding = ReviewFinding(
        "minor",
        "low",
        FindingClassification.CORRECTNESS_DEFECT,
        "minor",
        confirmed=True,
    )
    review = ReviewResult(
        ReviewerKind.DEEPSEEK, "sha-1", "approve", non_blocking_findings=(finding,)
    )
    codex = ReviewResult(ReviewerKind.CODEX, "sha-1", "approve")
    orch = Orchestrator(
        tmp_path, tmp_path / "runs", ci_runner=lambda _: ci(), git=FakeGit()
    )
    result = orch.run(
        TaskSpec("T", "tasks/T.md"), FakeProvider(review), FakeProvider(codex)
    )
    assert result.state.value == "COMPLETE"
    assert result.triage and result.triage.deferred_findings == (finding,)
