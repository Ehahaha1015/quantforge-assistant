from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol

from .audit import AuditStore
from .ci import run_ci
from .git import GitAdapter, GitError
from .models import (
    ReviewerKind,
    ReviewTimeouts,
    RunResult,
    RunState,
    TaskSpec,
    TransportAttempt,
    TransportStatus,
    TriageResult,
)
from .review import ReviewParseError
from .state import normalize_review
from .transport import LauncherError
from .triage import triage

MAX_REVIEW_TRANSPORT_ATTEMPTS = 4


class ReviewProvider(Protocol):
    reviewer: ReviewerKind

    def review(self, prompt: str, worktree: str, sha: str, timeout: float): ...


class Orchestrator:
    def __init__(
        self,
        repo: str | Path,
        audit_root: str | Path,
        ci_runner=run_ci,
        git: GitAdapter | None = None,
        timeouts: ReviewTimeouts | None = None,
    ) -> None:
        self.repo = Path(repo).resolve()
        configured_audit = Path(audit_root)
        self.audit = AuditStore(
            configured_audit
            if configured_audit.is_absolute()
            else self.repo.parent / configured_audit
        )
        self.git = git or GitAdapter(self.repo)
        self.ci_runner = ci_runner
        self.timeouts = timeouts or ReviewTimeouts()

    def run(
        self,
        task: TaskSpec,
        deepseek: ReviewProvider,
        codex: ReviewProvider,
        timeout: float | None = None,
        max_attempts: int = MAX_REVIEW_TRANSPORT_ATTEMPTS,
    ) -> RunResult:
        review_timeout = (
            self.timeouts.review_hard_timeout_seconds if timeout is None else timeout
        )
        try:
            target = self.git.head()
            clean = self.git.clean()
        except GitError:
            return RunResult(task.task_id, "", RunState.STALLED)
        result = RunResult(task.task_id, target, RunState.DISCOVER)
        if not clean:
            run_dir = self.audit.create_run(
                task.task_id, uuid.uuid4().hex, {"target_sha": target}
            )
            result.audit_path = str(run_dir)
            result.state = RunState.STALLED
            self.audit.write(run_dir / "result.json", result)
            return result
        result.state = RunState.CI
        result.ci = self.ci_runner(self.repo)
        run_dir = self.audit.create_run(
            task.task_id, uuid.uuid4().hex, {"target_sha": target}
        )
        result.audit_path = str(run_dir)
        self.audit.write(run_dir / "ci.json", result.ci)
        if not result.ci.passed:
            result.state = RunState.STALLED
            self.audit.write(run_dir / "result.json", result)
            return result
        result.state = RunState.REVIEW
        worktrees: list[Path] = []
        try:
            for provider, label in ((deepseek, "deepseek"), (codex, "codex")):
                expected_reviewer = (
                    ReviewerKind.DEEPSEEK if label == "deepseek" else ReviewerKind.CODEX
                )
                if provider.reviewer != expected_reviewer:
                    result.state = RunState.STALLED
                    self.audit.write(run_dir / "result.json", result)
                    return result
                preflight = getattr(provider, "preflight", None)
                if callable(preflight):
                    try:
                        preflight()
                    except (LauncherError, OSError) as exc:
                        result.transport_attempts[label] = [
                            TransportAttempt(1, TransportStatus.SPAWN_ERROR, str(exc), "")
                        ]
                        self.audit.write(
                            run_dir / f"preflight-error-{label}.json", {"error": str(exc)}
                        )
                        result.state = RunState.STALLED
                        self.audit.write(run_dir / "result.json", result)
                        return result
                wt = run_dir / "worktrees" / label
                try:
                    self.git.create_detached_worktree(wt, target)
                    worktrees.append(wt)
                    integrity = self.git.integrity(wt, target)
                except GitError as exc:
                    result.state = RunState.STALLED
                    self.audit.write(
                        run_dir / f"git-error-{label}.json", {"error": str(exc)}
                    )
                    self.audit.write(run_dir / "result.json", result)
                    return result
                self.audit.write(run_dir / f"integrity-{label}-before.json", integrity)
                if not integrity.valid:
                    result.state = RunState.STALLED
                    self.audit.write(run_dir / "result.json", result)
                    return result
                attempts: list[TransportAttempt] = []
                review = None
                for number in range(1, max_attempts + 1):
                    try:
                        review, attempt = provider.review(
                            f"Review task {task.task_id} at {target}",
                            str(wt),
                            target,
                            review_timeout,
                        )
                    except ReviewParseError as exc:
                        review = None
                        attempt = TransportAttempt(
                            number, TransportStatus.PARSE_ERROR, str(exc), ""
                        )
                    except (ValueError, TypeError, KeyError, IndexError) as exc:
                        review = None
                        attempt = TransportAttempt(
                            number, TransportStatus.SCHEMA_ERROR, str(exc), ""
                        )
                    if review is not None and (
                        review.review_commit != target
                        or review.reviewer != expected_reviewer
                    ):
                        review = None
                        attempt = TransportAttempt(
                            number,
                            TransportStatus.SCHEMA_ERROR,
                            "review identity mismatch",
                            "",
                        )
                    if review is not None and attempt.status != TransportStatus.SUCCESS:
                        review = None
                        attempt = TransportAttempt(
                            number,
                            TransportStatus.SCHEMA_ERROR,
                            "semantic result with failed transport",
                            "",
                        )
                    attempts.append(
                        TransportAttempt(
                            number,
                            attempt.status,
                            attempt.stderr,
                            attempt.stdout,
                            attempt.telemetry,
                        )
                    )
                    if attempt.telemetry is not None:
                        self.audit.write(
                            run_dir / "transport" / f"{label}-{number}.json",
                            attempt.telemetry,
                        )
                    if review is not None:
                        break
                result.transport_attempts[label] = attempts
                if review is None:
                    result.state = RunState.STALLED
                    self.audit.write(run_dir / "result.json", result)
                    return result
                try:
                    integrity = self.git.integrity(wt, target)
                except GitError as exc:
                    result.state = RunState.STALLED
                    self.audit.write(
                        run_dir / f"git-error-{label}-after.json", {"error": str(exc)}
                    )
                    self.audit.write(run_dir / "result.json", result)
                    return result
                self.audit.write(run_dir / f"integrity-{label}-after.json", integrity)
                if not integrity.valid:
                    result.state = RunState.STALLED
                    self.audit.write(run_dir / "result.json", result)
                    return result
                result.reviews.append(normalize_review(review))
                self.audit.write(
                    run_dir / "reviews" / f"{label}.json", result.reviews[-1]
                )
            result.state = RunState.TRIAGE
            blocking = [
                f for review in result.reviews for f in review.blocking_findings
            ]
            deferred = [
                f for review in result.reviews for f in review.non_blocking_findings
            ]
            blocking_triage = triage(blocking)
            result.triage = TriageResult(
                blocking_triage.confirmed_blockers,
                blocking_triage.unconfirmed_blockers,
                (*blocking_triage.deferred_findings, *deferred),
            )
            self.audit.write(run_dir / "triage.json", result.triage)
            if result.triage.confirmed_blockers:
                result.state = RunState.FIX_REQUIRED
                self.audit.write_fix_request(
                    run_dir,
                    task.task_id,
                    target,
                    task.contract_path,
                    result.triage.confirmed_blockers,
                    result.triage.deferred_findings,
                    task.required_ci,
                )
            else:
                result.state = RunState.COMPLETE
            self.audit.write(run_dir / "result.json", result)
            return result
        finally:
            for wt in reversed(worktrees):
                try:
                    self.git.remove_worktree(wt)
                except GitError as exc:
                    self.audit.write(
                        run_dir / "cleanup-error.json",
                        {"worktree": str(wt), "error": str(exc)},
                    )
