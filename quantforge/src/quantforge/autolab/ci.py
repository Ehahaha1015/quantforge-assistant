from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from .models import CIResult, CommandResult

DEFAULT_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pytest", ("uv", "run", "pytest")),
    ("ruff", ("uv", "run", "ruff", "check", ".")),
    ("mypy", ("uv", "run", "mypy", "src")),
    ("diff-check", ("git", "diff", "--check")),
)


def run_ci(
    root: str | Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    commands: Sequence[tuple[str, tuple[str, ...]]] = DEFAULT_COMMANDS,
) -> CIResult:
    results = []
    for name, command in commands:
        try:
            completed = runner(
                command, cwd=str(root), capture_output=True, text=True, check=False
            )
            results.append(
                CommandResult(
                    name,
                    command,
                    completed.returncode,
                    completed.stdout,
                    completed.stderr,
                )
            )
        except OSError as exc:
            results.append(CommandResult(name, command, 1, "", str(exc)))
    return CIResult(tuple(results))
