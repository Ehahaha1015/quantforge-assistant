"""Export one lean source ZIP from explicit repositories; never environments/data."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOS = ("quantforge", "qf-lab", "qf-platform", "quantforge-ui")
ROOT_FILES = (
    "compose.showcase.yaml",
    ".dockerignore",
    "run_showcase.ps1",
    "run_showcase.sh",
    ".github/workflows/showcase.yml",
    "deploy/docker/Dockerfile.api",
    "deploy/docker/Dockerfile.web",
    "deploy/docker/nginx.conf",
    "governance-tools/ci_showcase.py",
    "governance-tools/export_showcase.py",
    "docs/SHOWCASE.md",
    "docs/INTERNSHIP-ARCHITECTURE.md",
    "docs/INTERNSHIP-INTERVIEW.md",
)


def export(output: Path) -> dict[str, str]:
    output = output.absolute()
    if not output.is_relative_to(ROOT / "local-smoke"):
        raise ValueError("Export must stay in workspace local-smoke")
    for parent in (output.parent, *output.parent.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("Redirected export parent")
    files: dict[str, Path] = {name: ROOT / name for name in ROOT_FILES}
    root_names = {
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "package.json",
        "package-lock.json",
        "index.html",
        "vite.config.ts",
        "vitest.config.ts",
        "tsconfig.json",
        "tsconfig.app.json",
        "tsconfig.node.json",
        ".gitignore",
    }
    for repo in REPOS:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(ROOT / repo),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=True,
        )
        for name in result.stdout.decode().split("\0"):
            if not name:
                continue
            path = Path(name)
            if (
                path.parts[0] not in {"src", "tests", "public"}
                and name not in root_names
            ):
                continue
            if any(
                part.startswith(".") and part != ".gitignore" for part in path.parts
            ):
                continue
            if path.suffix.lower() in {".sqlite", ".db", ".key", ".pem", ".image"}:
                raise ValueError("Private/runtime suffix in export candidates")
            files[repo + "/" + name] = ROOT / repo / name
    hashes = {}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            if (
                path.is_symlink()
                or path.is_junction()
                or not path.resolve().is_relative_to(ROOT)
            ):
                raise ValueError("Redirected source in export")
            for ancestor in path.parents:
                if ancestor == ROOT:
                    break
                if ancestor.is_symlink() or ancestor.is_junction():
                    raise ValueError("Redirected source ancestor")
            raw = path.read_bytes()
            archive.writestr(name, raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        readme = (ROOT / "docs/SHOWCASE.md").read_text(encoding="utf-8")
        for name in ("INTERNSHIP-ARCHITECTURE.md", "INTERNSHIP-INTERVIEW.md"):
            readme = readme.replace("(" + name + ")", "(docs/" + name + ")")
        archive.writestr("README.md", readme)
        archive.writestr(
            ".gitignore",
            "**/.venv/\n**/node_modules/\n**/__pycache__/\n**/.pytest_cache/\n"
            "**/.mypy_cache/\n**/.ruff_cache/\n**/dist/\n**/*.tsbuildinfo\n"
            "local-smoke/\nlocal-tools/\n**/.env\n**/.env.*\n"
            "**/*.sqlite*\n**/*.db\n**/*.key\n**/*.pem\n",
        )
        archive.writestr(
            "source-manifest.json",
            json.dumps(
                {
                    "files": hashes,
                    "scope": "ALLOWLISTED_SOURCE_ONLY_NO_RUNTIME_DATA_OR_ENVIRONMENTS",
                },
                indent=2,
            ),
        )
    return hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    files = export(args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "files": len(files),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
