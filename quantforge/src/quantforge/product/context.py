"""Observe executing source/runtime instead of trusting proposal metadata."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import subprocess
from pathlib import Path
from typing import Any


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def source_identity() -> str:
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        if path.is_symlink():
            raise ValueError("redirected source is not supported")
        relative = path.relative_to(package).as_posix().encode("utf-8")
        raw = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def code_context() -> dict[str, Any]:
    root = repository_root()
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    return {
        "git_commit": commit,
        "lockfile_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        "runtime": {
            "python": platform.python_version(),
            **{
                name: importlib.metadata.version(name)
                for name in ("polars", "pydantic", "duckdb")
            },
        },
    }
