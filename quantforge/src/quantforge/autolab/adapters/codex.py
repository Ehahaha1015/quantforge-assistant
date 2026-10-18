from __future__ import annotations

import subprocess

from ..models import ReviewerKind, ReviewTimeouts, TransportAttempt, TransportStatus
from ..review import (
    ReviewAdapter,
    ReviewParseError,
    ReviewSchemaError,
    classify_process,
    parse_review,
)
from ..transport import LauncherError, LauncherType
from .base import ProcessAdapter


class CodexAdapter(ReviewAdapter):
    reviewer = ReviewerKind.CODEX

    def __init__(
        self,
        executable: str = "codex",
        runner=subprocess.run,
        timeouts: ReviewTimeouts | None = None,
    ) -> None:
        self.process = ProcessAdapter(executable, runner)
        self.timeouts = timeouts or ReviewTimeouts()

    def readiness(self, prompt: str, worktree: str):
        try:
            self.preflight()
            completed = self.process.invoke(
                ["exec", "--ephemeral", "--sandbox", "read-only", prompt],
                worktree,
                self.timeouts.readiness_timeout_seconds,
            )
            status = classify_process(completed.returncode)
            return None, self._attempt(status, completed.stderr, completed.stdout)
        except subprocess.TimeoutExpired as exc:
            return None, self._attempt(
                TransportStatus.READINESS_TIMEOUT,
                getattr(exc, "stderr", None) or str(exc),
                getattr(exc, "output", None) or "",
            )
        except (LauncherError, OSError) as exc:
            return None, self._attempt(TransportStatus.SPAWN_ERROR, str(exc), "")

    def preflight(self) -> None:
        self.process.last_telemetry = None
        launcher = self.process.launcher()
        if launcher.launcher_type != LauncherType.NATIVE:
            raise LauncherError("Codex requires a native executable")

    def _attempt(
        self, status: TransportStatus, stderr: str = "", stdout: str = ""
    ) -> TransportAttempt:
        return TransportAttempt(
            1,
            status,
            stderr,
            stdout,
            self.process.finalize_telemetry(self.reviewer.value, status),
        )

    def review(self, prompt: str, worktree: str, sha: str, timeout: float):
        try:
            self.preflight()
            completed = self.process.invoke(
                ["exec", "--ephemeral", "--sandbox", "read-only", prompt],
                worktree,
                timeout,
            )
            status = classify_process(completed.returncode)
            if status != TransportStatus.SUCCESS:
                return None, self._attempt(status, completed.stderr, completed.stdout)
            try:
                result = parse_review(completed.stdout, self.reviewer, sha)
            except ReviewParseError:
                return None, self._attempt(
                    TransportStatus.PARSE_ERROR, completed.stderr, completed.stdout
                )
            except (ReviewSchemaError, ValueError, TypeError, KeyError, IndexError):
                return None, self._attempt(
                    TransportStatus.SCHEMA_ERROR, completed.stderr, completed.stdout
                )
            return result, self._attempt(status, completed.stderr, completed.stdout)
        except subprocess.TimeoutExpired as exc:
            status = TransportStatus.HARD_TIMEOUT
            return None, self._attempt(
                status,
                getattr(exc, "stderr", None) or str(exc),
                getattr(exc, "output", None) or "",
            )
        except LauncherError as exc:
            return None, self._attempt(TransportStatus.SPAWN_ERROR, str(exc), "")
        except OSError as exc:
            return None, self._attempt(TransportStatus.SPAWN_ERROR, str(exc), "")
