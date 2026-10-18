from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ReviewerKind(StrEnum):
    DEEPSEEK = "deepseek"
    CODEX = "codex"


class TransportStatus(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    UPSTREAM_DISCONNECT = "upstream_disconnect"
    PROCESS_ERROR = "process_error"
    SPAWN_ERROR = "spawn_error"
    PARSE_ERROR = "parse_error"
    SCHEMA_ERROR = "schema_error"
    READINESS_TIMEOUT = "readiness_timeout"
    HARD_TIMEOUT = "hard_timeout"


class FindingClassification(StrEnum):
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"
    CORRECTNESS_DEFECT = "CORRECTNESS_DEFECT"
    ARCHITECTURE_VIOLATION = "ARCHITECTURE_VIOLATION"
    HARDENING = "HARDENING"
    FUTURE_DESIGN = "FUTURE_DESIGN"


class NormalizedVerdict(StrEnum):
    APPROVE = "APPROVE"
    APPROVE_WITH_MINOR_FINDINGS = "APPROVE_WITH_MINOR_FINDINGS"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"


class RunState(StrEnum):
    DISCOVER = "DISCOVER"
    CI = "CI"
    REVIEW = "REVIEW"
    TRIAGE = "TRIAGE"
    COMPLETE = "COMPLETE"
    FIX_REQUIRED = "FIX_REQUIRED"
    STALLED = "STALLED"
    ESCALATED = "ESCALATED"


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    contract_path: str
    required_ci: tuple[str, ...] = ("pytest", "ruff", "mypy", "diff-check")


@dataclass(frozen=True)
class ReviewFinding:
    id: str
    severity: str
    classification: FindingClassification
    title: str
    file: str = ""
    evidence: str = ""
    why_it_matters: str = ""
    remediation: str = ""
    confirmed: bool = False


@dataclass(frozen=True)
class ReviewCommit:
    sha: str
    reviewer: ReviewerKind
    worktree: str = ""


@dataclass(frozen=True)
class TransportTelemetry:
    reviewer: str = ""
    launcher_type: str = ""
    resolved_executable: str = ""
    start_time: str = ""
    first_output_time: str | None = None
    end_time: str = ""
    duration_seconds: float = 0.0
    exit_code: int | None = None
    transport_status: str = ""
    timeout_classification: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0


@dataclass(frozen=True)
class TransportAttempt:
    attempt: int
    status: TransportStatus
    stderr: str = ""
    stdout: str = ""
    telemetry: TransportTelemetry | None = None


@dataclass(frozen=True)
class ReviewTimeouts:
    readiness_timeout_seconds: float = 300.0
    review_hard_timeout_seconds: float = 900.0


@dataclass(frozen=True)
class ReviewResult:
    reviewer: ReviewerKind
    review_commit: str
    raw_verdict: str
    blocking_findings: tuple[ReviewFinding, ...] = ()
    non_blocking_findings: tuple[ReviewFinding, ...] = ()
    merge_recommendation: str = ""


@dataclass(frozen=True)
class NormalizedReview:
    reviewer: ReviewerKind
    review_commit: str
    raw_verdict: str
    verdict: NormalizedVerdict
    blocking_findings: tuple[ReviewFinding, ...]
    non_blocking_findings: tuple[ReviewFinding, ...]


@dataclass(frozen=True)
class TriageResult:
    confirmed_blockers: tuple[ReviewFinding, ...] = ()
    unconfirmed_blockers: tuple[ReviewFinding, ...] = ()
    deferred_findings: tuple[ReviewFinding, ...] = ()


@dataclass(frozen=True)
class CommandResult:
    name: str
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def passed(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class CIResult:
    commands: tuple[CommandResult, ...]

    @property
    def passed(self) -> bool:
        return all(command.passed for command in self.commands)


@dataclass
class RunResult:
    task_id: str
    target_sha: str
    state: RunState
    ci: CIResult | None = None
    reviews: list[NormalizedReview] = field(default_factory=list)
    triage: TriageResult | None = None
    transport_attempts: dict[str, list[TransportAttempt]] = field(default_factory=dict)
    audit_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
