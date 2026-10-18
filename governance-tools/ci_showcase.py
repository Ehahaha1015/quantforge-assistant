"""Offline application checks in an exported workspace, with caches on its disk."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    cache = ROOT / "local-tools/cache"
    environment = os.environ.copy()
    for key, folder in {
        "TEMP": "temp",
        "TMP": "temp",
        "UV_CACHE_DIR": "uv",
        "npm_config_cache": "npm",
    }.items():
        path = cache / folder
        path.mkdir(parents=True, exist_ok=True)
        environment[key] = str(path)
    python = (
        ROOT
        / "qf-platform/.venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    npm = "npm.cmd" if os.name == "nt" else "npm"
    if args.install:
        subprocess.run(
            ["uv", "sync", "--project", "qf-platform", "--frozen"],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        subprocess.run(
            [npm, "ci", "--prefix", "quantforge-ui"],
            cwd=ROOT,
            env=environment,
            check=True,
        )
    commands = [
        ("qf-platform", [str(python), "-m", "ruff", "check", "."]),
        ("qf-platform", [str(python), "-m", "mypy", "src"]),
        ("qf-platform", [str(python), "-m", "pytest", "-q"]),
        ("quantforge-ui", [npm, "run", "typecheck"]),
        ("quantforge-ui", [npm, "test"]),
        ("quantforge-ui", [npm, "run", "build"]),
        (
            ".",
            [
                str(python),
                "-m",
                "qf_platform.chart_evaluation",
                "--output",
                str(ROOT / "local-smoke/ci-contract-evaluation.json"),
            ],
        ),
        (
            ".",
            [
                str(python),
                "-m",
                "qf_platform.showcase_checks",
                "verify",
                "--root",
                str(ROOT / "local-smoke/ci-fault-http"),
            ],
        ),
    ]
    for cwd, command in commands:
        subprocess.run(command, cwd=ROOT / cwd, env=environment, check=True)


if __name__ == "__main__":
    main()
