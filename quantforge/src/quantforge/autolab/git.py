from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeIntegrity:
    head_matches: bool
    tracked_modified: bool
    staged_modified: bool
    unauthorized_untracked: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return (
            self.head_matches
            and not self.tracked_modified
            and not self.staged_modified
            and not self.unauthorized_untracked
        )


class GitAdapter:
    def __init__(self, root: str | Path, runner=subprocess.run) -> None:
        self.root = Path(root)
        self.runner = runner

    def _run(self, *args: str, cwd: Path | None = None) -> str:
        result = self.runner(
            ("git", *args),
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise GitError(result.stderr.strip() or "git command failed")
        return result.stdout.rstrip("\r\n")

    def head(self) -> str:
        return self._run("rev-parse", "HEAD")

    def branch(self) -> str:
        return self._run("branch", "--show-current")

    def clean(self) -> bool:
        return not bool(self._run("status", "--porcelain"))

    def create_detached_worktree(self, path: str | Path, sha: str) -> Path:
        target = Path(path)
        self._run("worktree", "add", "--detach", str(target), sha)
        return target

    def remove_worktree(self, path: str | Path) -> None:
        self._run("worktree", "remove", "--force", str(path))

    def integrity(
        self, path: str | Path, sha: str, allowed_untracked: set[str] | None = None
    ) -> WorktreeIntegrity:
        wt = Path(path)
        head = self._run("rev-parse", "HEAD", cwd=wt)
        porcelain = self._run(
            "status", "--porcelain", "--untracked-files=all", "--ignored", cwd=wt
        )
        tracked = False
        staged = False
        untracked: list[str] = []
        allowed = allowed_untracked or set()
        for line in porcelain.splitlines():
            if len(line) < 2:
                continue
            code = line[:2]
            name = line[3:] if len(line) > 3 else ""
            if code[0] not in (" ", "?", "!"):
                staged = True
            if code[1] not in (" ", "?", "!"):
                tracked = True
            if code in ("??", "!!") and name not in allowed:
                untracked.append(name)
        return WorktreeIntegrity(head == sha, tracked, staged, tuple(untracked))


def same_sha(expected: str, actual: str) -> bool:
    return expected == actual
