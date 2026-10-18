from __future__ import annotations

import ast
from pathlib import Path

import quantforge.execution_ledger as package

_ALLOWED_FILES = {
    "__init__.py",
    "_canonical.py",
    "adapter.py",
    "errors.py",
    "identity.py",
    "ledger.py",
    "models.py",
    "projection.py",
    "reconciliation.py",
    "sidecar.py",
}

_FORBIDDEN_MODULES = {
}

_FORBIDDEN_TOKENS = (
    "socket",
    "urllib",
    "requests",
    "aiohttp",
    "websocket",
    "subprocess",
    "os.environ",
    "getenv",
    "keyring",
    "datetime.now",
    "utcnow",
    "time.time",
    "uuid4",
)

_ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "dataclasses",
    "datetime",
    "enum",
    "hashlib",
    "json",
    "os",
    "pathlib",
    "re",
    "sqlite3",
    "typing",
    "pydantic",
    "quantforge",
}


def test_slice0_module_file_scope() -> None:
    package_dir = Path(package.__file__).resolve().parent
    present = {
        path.name
        for path in package_dir.iterdir()
        if path.is_file() and path.suffix == ".py"
    }
    assert present == _ALLOWED_FILES
    assert present.isdisjoint(_FORBIDDEN_MODULES)


def test_slice0_contains_no_forbidden_capability_tokens() -> None:
    package_dir = Path(package.__file__).resolve().parent
    for path in sorted(package_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN_TOKENS:
            assert token not in text, (path.name, token)


def test_slice0_imports_stay_within_the_allowlist() -> None:
    package_dir = Path(package.__file__).resolve().parent
    for path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.level == 0
            ):
                roots.add(node.module.split(".", 1)[0])
        assert roots <= _ALLOWED_IMPORT_ROOTS, (path.name, roots)
