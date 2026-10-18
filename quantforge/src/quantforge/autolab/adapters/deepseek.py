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
from ..transport import LauncherError, LauncherType, _which
from .base import ProcessAdapter


class DeepSeekAdapter(ReviewAdapter):
    reviewer = ReviewerKind.DEEPSEEK

    def __init__(
        self,
        executable: str = "dsh",
        runner=subprocess.run,
        timeouts: ReviewTimeouts | None = None,
    ) -> None:
        self.process = ProcessAdapter(executable, runner)
        self.timeouts = timeouts or ReviewTimeouts()

    def readiness(self, prompt: str, worktree: str):
        try:
            self.preflight()
            completed = self.process.invoke(
                ["--profile", "headless", prompt],
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
        if (
            launcher.launcher_type == LauncherType.POWERSHELL
            and _which(("pwsh.exe",)) is None
        ):
            # ``resolve_launcher`` already validates this in the normal path;
            # keep the explicit check for a complete preflight.
            raise LauncherError("pwsh.exe is required for DeepSeek .ps1 reviewers")
        if (
            launcher.launcher_type in {LauncherType.POWERSHELL, LauncherType.CMD}
            and _which(("node.exe",)) is None
        ):
            raise LauncherError("node.exe is required for DeepSeek .ps1 reviewers")

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
                ["--profile", "headless", prompt], worktree, timeout
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
