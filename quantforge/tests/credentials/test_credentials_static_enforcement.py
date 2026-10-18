"""Static rules F1..F9, oracles AB/AH/AI and the dependency direction."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
QUANTFORGE_ROOT = SRC_ROOT / "quantforge"
PACKAGE_ROOT = QUANTFORGE_ROOT / "credentials"
PACKAGE_MODULE_NAMES = (
    "__init__.py",
    "boundary.py",
    "errors.py",
    "models.py",
    "provider.py",
    "secrets.py",
)
PROTECTED_ROOTS = frozenset(
    {
        "frontier",
        "execstate",
        "execution_ledger",
        "operator",
        "provenance",
        "marketdata",
        "broker",
        "live",
        "predispatch",
        "risk",
        "domain",
        "portfolio",
        "models",
        "strategies",
        "backtest",
        "validation",
        "experiments",
        "factors",
        "data",
        "execution",
        "autolab",
    }
)
FORBIDDEN_ROOTS = frozenset(
    {
        "os",
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "websocket",
        "websockets",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "asyncio",
        "subprocess",
        "multiprocessing",
        "ctypes",
        "pty",
        "keyring",
        "getpass",
        "secrets",
        "dotenv",
        "uuid",
        "random",
        "time",
        "sched",
        "pathlib",
        "io",
        "tempfile",
        "shutil",
        "sqlite3",
        "duckdb",
        "importlib",
        "pickle",
        "marshal",
        "shelve",
        "logging",
    }
)
ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "dataclasses",
        "datetime",
        "hashlib",
        "json",
        "re",
        "typing",
        "enum",
        "pydantic",
    }
)
FORBIDDEN_BUILTINS = frozenset(
    {
        "__import__",
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "breakpoint",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
    }
)
FORBIDDEN_CALLEES = frozenset(
    {
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.datetime.fromtimestamp",
        "datetime.date.today",
        "utcnow",
        "today",
        "fromtimestamp",
    }
)
NETWORK_ROOTS = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "websocket",
        "websockets",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "asyncio",
    }
)
SECRET_CLASS_IDENTITIES = frozenset(
    {
        "quantforge.credentials.secrets.SecretMaterial",
        "quantforge.credentials.secrets.CredentialLease",
    }
)
BOUNDARY_IDENTITIES = frozenset(
    {
        "quantforge.credentials.boundary.CredentialBoundary",
        "quantforge.credentials.CredentialBoundary",
    }
)
PROTECTED_SHORT_NAMES = frozenset(
    {"SecretMaterial", "CredentialLease", "CredentialBoundary"}
)

# CREDENTIAL-002 amendment (frozen sections 19..21).
WINDOWS_PACKAGE_ROOT = QUANTFORGE_ROOT / "credentials_windows"
WINDOWS_MODULE_NAMES = (
    "__init__.py",
    "backend.py",
    "native.py",
    "composition.py",
)
# MARKETDATA-002 consumer amendment (frozen section 32 clause (c)): the
# current-snapshot importer allow-list gains exactly one reviewed consumer
# module and no wildcard.
MARKETDATA_ALPACA_COMPOSITION_PATH = (
    QUANTFORGE_ROOT / "marketdata_alpaca" / "composition.py"
)
WINDOWS_CURRENT_SNAPSHOT_ALLOW_LIST = frozenset(
    WINDOWS_PACKAGE_ROOT / name for name in WINDOWS_MODULE_NAMES
) | frozenset({MARKETDATA_ALPACA_COMPOSITION_PATH})
WINDOWS_COMPOSITION_PATH = WINDOWS_PACKAGE_ROOT / "composition.py"
PROTECTED_BOUNDARY_NAME = "CredentialBoundary"
PROTECTED_SECRET_NAMES = frozenset({"SecretMaterial", "CredentialLease"})
APPROVED_WINDOWS_BACKEND = "WindowsCredentialManagerBackend"
APPROVED_WINDOWS_SEAMS = frozenset(
    {"CredentialReadSeam", "Advapi32CredentialReadSeam"}
)
FORBIDDEN_WINDOWS_BASES = frozenset(
    {
        "WindowsCredentialManagerBackend",
        "CredentialReadSeam",
        "Advapi32CredentialReadSeam",
        "CredentialBackend",
        "RejectingCredentialBackend",
    }
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _package_asts() -> dict[str, ast.Module]:
    return {
        name: _parse(PACKAGE_ROOT / name) for name in PACKAGE_MODULE_NAMES
    }


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.py") if "__pycache__" not in path.parts
    )


def _src_asts() -> dict[Path, ast.Module]:
    return {path: _parse(path) for path in _python_files(QUANTFORGE_ROOT)}


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _containing_package(path: Path) -> tuple[str, ...]:
    return path.relative_to(SRC_ROOT).parts[:-1]


def _resolved_module_for_import(path: Path, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    parts = _containing_package(path)
    base = parts[: len(parts) - (node.level - 1)]
    if node.module:
        return ".".join((*base, node.module))
    return ".".join(base)


def _resolved_import_modules(path: Path, tree: ast.Module) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _resolved_module_for_import(path, node)
            if module:
                modules.append(module)
    return modules


def _import_bindings(path: Path, tree: ast.Module) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    bindings[alias.name.split(".")[0]] = alias.name.split(".")[
                        0
                    ]
        elif isinstance(node, ast.ImportFrom):
            module = _resolved_module_for_import(path, node)
            for alias in node.names:
                if alias.name == "*":
                    bindings["*"] = module
                elif alias.asname:
                    bindings[alias.asname] = f"{module}.{alias.name}"
                else:
                    bindings[alias.name] = (
                        f"{module}.{alias.name}" if module else alias.name
                    )
    return bindings


def _normalize_identity(identity: str) -> str:
    prefix = "quantforge.credentials."
    if not identity.startswith(prefix):
        return identity
    tail = identity[len(prefix) :]
    if "." in tail:
        return identity
    return {
        "SecretMaterial": "quantforge.credentials.secrets.SecretMaterial",
        "CredentialLease": "quantforge.credentials.secrets.CredentialLease",
        "CredentialBoundary": (
            "quantforge.credentials.boundary.CredentialBoundary"
        ),
    }.get(tail, identity)


def _protected_name_calls(
    path: Path, tree: ast.Module
) -> list[tuple[str, int]]:
    bindings = _import_bindings(path, tree)
    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        identity: str | None = None
        if isinstance(func, ast.Name):
            identity = bindings.get(func.id)
            if identity is None and func.id in PROTECTED_SHORT_NAMES:
                identity = "<unresolved>"
        elif isinstance(func, ast.Attribute):
            dotted = _dotted_name(func)
            if dotted is not None:
                root, _, rest = dotted.partition(".")
                base = bindings.get(root)
                if base is not None:
                    identity = f"{base}.{rest}" if rest else base
                elif root == "quantforge":
                    identity = dotted
                if identity is not None:
                    identity = _normalize_identity(identity)
                if identity is None and dotted.split(".")[-1] in (
                    PROTECTED_SHORT_NAMES
                ):
                    identity = "<unresolved>"
        if identity is not None:
            results.append((identity, node.lineno))
    return results


def _direct_class_bindings(cls: ast.ClassDef) -> set[str]:
    found: set[str] = set()
    for node in cls.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found.update(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            found.add(node.target.id)
    return found


def _windows_package_asts() -> dict[str, ast.Module]:
    """ASTs of the four frozen CREDENTIAL-002 modules."""
    return {
        name: _parse(WINDOWS_PACKAGE_ROOT / name)
        for name in WINDOWS_MODULE_NAMES
    }


def _identifier_names(tree: ast.Module) -> list[str]:
    """Frozen identifier positions of the CREDENTIAL-002 grammar."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.append(alias.name)
                if alias.asname:
                    names.append(alias.asname)
        elif isinstance(
            node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            names.append(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.extend(node.names)
        else:
            argument = getattr(node, "arg", None)
            if isinstance(argument, str):
                names.append(argument)
    return names


_CLASS_BLOCKS = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Match,
)


def _nested_statement_lists(node: ast.AST) -> list[list[ast.stmt]]:
    if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        return [node.body, node.orelse]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [node.body]
    if isinstance(node, ast.Try):
        return [
            node.body,
            node.orelse,
            node.finalbody,
            *(handler.body for handler in node.handlers),
        ]
    if isinstance(node, ast.Match):
        return [case.body for case in node.cases]
    return []


def _class_level_names(body: list[ast.stmt]) -> set[str]:
    """Class-owned names, descending class-body blocks but not method bodies."""
    found: set[str] = set()
    for node in body:
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found.update(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            found.add(node.target.id)
        elif isinstance(node, _CLASS_BLOCKS):
            for nested in _nested_statement_lists(node):
                found |= _class_level_names(nested)
    return found


def _instance_attribute_names(cls: ast.ClassDef) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(cls):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                found.add(target.attr)
    return found


def _deep_class_bindings(cls: ast.ClassDef) -> set[str]:
    """Class-owned bindings plus self.<name> instance attributes."""
    return _class_level_names(cls.body) | _instance_attribute_names(cls)


def test_f1_imports_resolve_into_the_allow_list() -> None:
    for name, tree in _package_asts().items():
        path = PACKAGE_ROOT / name
        for module in _resolved_import_modules(path, tree):
            root = module.split(".")[0]
            allowed = (
                root in ALLOWED_IMPORT_ROOTS
                or module == "quantforge.frontier"
                or module.startswith(
                    ("quantforge.frontier.", "quantforge.credentials")
                )
            )
            assert allowed, (name, module)


def test_f2_no_forbidden_module_root() -> None:
    for name, tree in _package_asts().items():
        path = PACKAGE_ROOT / name
        for module in _resolved_import_modules(path, tree):
            assert module.split(".")[0] not in FORBIDDEN_ROOTS, (name, module)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                dotted = _dotted_name(node)
                if dotted is not None:
                    assert (
                        dotted.split(".")[0] not in FORBIDDEN_ROOTS
                    ), (name, dotted)


def test_f3_no_forbidden_bare_builtin_call() -> None:
    for name, tree in _package_asts().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_BUILTINS, (
                    name,
                    node.lineno,
                )


def test_f4_no_forbidden_callee_identity() -> None:
    for name, tree in _package_asts().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLEES, (
                    name,
                    node.lineno,
                )
                continue
            dotted = _dotted_name(node.func)
            if dotted is None:
                continue
            assert dotted not in FORBIDDEN_CALLEES, (name, dotted)
            assert dotted.split(".")[0] not in FORBIDDEN_ROOTS, (name, dotted)


def test_f5_module_file_set_is_exactly_frozen() -> None:
    files = sorted(path.name for path in PACKAGE_ROOT.glob("*.py"))
    assert files == sorted(PACKAGE_MODULE_NAMES)


def test_f6_secret_classes_are_structurally_frozen() -> None:
    tree = _package_asts()["secrets.py"]
    classes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    assert set(classes) == {"SecretMaterial", "CredentialLease"}
    required = {
        "__repr__",
        "__str__",
        "__copy__",
        "__deepcopy__",
        "__reduce__",
        "__reduce_ex__",
    }
    forbidden = {
        "model_dump",
        "model_dump_json",
        "dict",
        "json",
        "to_dict",
        "as_dict",
    }
    for name, cls in classes.items():
        # An empty bases list means exactly ``object`` is inherited.
        assert len(cls.bases) <= 1, name
        if cls.bases:
            base = cls.bases[0]
            assert isinstance(base, ast.Name) and base.id == "object", name
        bindings = _direct_class_bindings(cls)
        assert required <= bindings, name
        assert not (forbidden & bindings), name


def test_f7_construction_authority_is_exactly_frozen() -> None:
    secret_sites: list[Path] = []
    boundary_sites: list[tuple[Path, int]] = []
    for path, tree in _src_asts().items():
        for identity, lineno in _protected_name_calls(path, tree):
            if identity == "<unresolved>":
                pytest.fail(
                    f"unresolved protected-name call at {path}:{lineno}"
                )
            if identity in SECRET_CLASS_IDENTITIES:
                secret_sites.append(path)
            elif identity in BOUNDARY_IDENTITIES:
                boundary_sites.append((path, lineno))
    assert [path for path, _lineno in boundary_sites] == [
        WINDOWS_COMPOSITION_PATH
    ]
    assert len(boundary_sites) == 1
    assert secret_sites
    assert set(secret_sites) == {PACKAGE_ROOT / "boundary.py"}


def test_f7_identifier_absence_is_exact() -> None:
    for path, tree in _src_asts().items():
        if path.is_relative_to(PACKAGE_ROOT):
            continue
        if path.is_relative_to(WINDOWS_PACKAGE_ROOT):
            continue
        for identifier in _identifier_names(tree):
            assert identifier != PROTECTED_BOUNDARY_NAME, path
    for name, tree in _windows_package_asts().items():
        for identifier in _identifier_names(tree):
            assert identifier not in PROTECTED_SECRET_NAMES, name
    composition = _windows_package_asts()["composition.py"]
    occurrences = [
        identifier
        for identifier in _identifier_names(composition)
        if identifier == PROTECTED_BOUNDARY_NAME
    ]
    assert len(occurrences) == 2
    constructions = [
        node
        for node in ast.walk(composition)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == PROTECTED_BOUNDARY_NAME
    ]
    assert len(constructions) == 1


def test_f7_unresolved_protected_name_fails_closed() -> None:
    tree = ast.parse(
        "from quantforge.credentials import *\nCredentialBoundary()\n"
    )
    probe_path = QUANTFORGE_ROOT / "synthetic_probe.py"
    results = _protected_name_calls(probe_path, tree)
    assert results
    assert results[0][0] == "<unresolved>"


def test_f8a_only_approved_classes_bind_provider_id_and_acquire() -> None:
    provider_bindings: list[tuple[Path, str]] = []
    acquire_bindings: list[tuple[Path, str]] = []
    scanned: dict[Path, ast.Module] = {
        PACKAGE_ROOT / name: tree
        for name, tree in _package_asts().items()
    }
    scanned.update(
        {
            WINDOWS_PACKAGE_ROOT / name: tree
            for name, tree in _windows_package_asts().items()
        }
    )
    for path, tree in scanned.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bindings = _deep_class_bindings(node)
            if "provider_id" in bindings:
                provider_bindings.append((path, node.name))
            if "acquire" in bindings:
                acquire_bindings.append((path, node.name))
    assert provider_bindings == [
        (PACKAGE_ROOT / "provider.py", "CredentialBackend"),
        (PACKAGE_ROOT / "provider.py", "RejectingCredentialBackend"),
        (WINDOWS_PACKAGE_ROOT / "backend.py", APPROVED_WINDOWS_BACKEND),
    ]
    assert sorted(acquire_bindings) == sorted(
        [
            (PACKAGE_ROOT / "provider.py", "CredentialBackend"),
            (PACKAGE_ROOT / "provider.py", "RejectingCredentialBackend"),
            (PACKAGE_ROOT / "boundary.py", "CredentialBoundary"),
            (WINDOWS_PACKAGE_ROOT / "backend.py", APPROVED_WINDOWS_BACKEND),
        ]
    )


def test_f8_amended_windows_package_authority() -> None:
    read_binders: list[tuple[str, str]] = []
    for name, tree in _windows_package_asts().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bindings = _deep_class_bindings(node)
            if "read" in bindings:
                read_binders.append((name, node.name))
            for base in node.bases:
                text = _dotted_name(base)
                assert text is None or text.split(".")[-1] not in (
                    FORBIDDEN_WINDOWS_BASES
                ), (name, node.name, text)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "type"
            ):
                assert len(node.args) == 1, (name, node.lineno)
                assert not any(
                    isinstance(argument, ast.Starred)
                    for argument in node.args
                ), (name, node.lineno)
    assert sorted(read_binders) == sorted(
        [
            ("native.py", "CredentialReadSeam"),
            ("native.py", "Advapi32CredentialReadSeam"),
        ]
    )


def test_f8b_single_rejecting_singleton_and_default_binding() -> None:
    provider_tree = _package_asts()["provider.py"]
    assignments = [
        node
        for node in provider_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_REJECTING_BACKEND"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    value = assignments[0].value
    assert isinstance(value, ast.Call)
    assert isinstance(value.func, ast.Name)
    assert value.func.id == "RejectingCredentialBackend"
    for name, tree in _package_asts().items():
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(
                node.value, ast.Call
            ):
                func = node.value.func
                if (
                    isinstance(func, ast.Name) and func.id.endswith("Backend")
                ):
                    targets = {
                        target.id
                        for target in node.targets
                        if isinstance(target, ast.Name)
                    }
                    assert targets == {"_REJECTING_BACKEND"}, (
                        name,
                        node.lineno,
                    )
    boundary_tree = _package_asts()["boundary.py"]
    default: ast.AST | None = None
    for node in ast.walk(boundary_tree):
        if isinstance(node, ast.ClassDef) and node.name == "CredentialBoundary":
            for member in node.body:
                if (
                    isinstance(member, ast.FunctionDef)
                    and member.name == "__init__"
                ):
                    assert member.args.kwonlyargs[-1].arg == "backend"
                    default = member.args.kw_defaults[-1]
    assert isinstance(default, ast.Name)
    assert default.id == "_REJECTING_BACKEND"


def test_f8c_no_dynamic_class_creation() -> None:
    for name, tree in _package_asts().items():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "type"
            ):
                assert len(node.args) == 1, (name, node.lineno)
                assert not any(
                    isinstance(argument, ast.Starred)
                    for argument in node.args
                ), (name, node.lineno)


def test_f8d_protected_roots_define_no_backend_shaped_class() -> None:
    for path, tree in _src_asts().items():
        relative = path.relative_to(QUANTFORGE_ROOT)
        if relative.parts[0] not in PROTECTED_ROOTS:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bindings = _direct_class_bindings(node)
                assert not (
                    {"provider_id", "acquire"} & bindings
                ), (path, node.name)


def test_f8e_oracle_ah_backend_allow_list_is_finite() -> None:
    approved: list[tuple[Path, str]] = []
    backend_state: set[str] = set()
    for path, tree in _src_asts().items():
        for node in ast.walk(tree):
            if isinstance(
                node, ast.ClassDef
            ) and {"provider_id", "acquire"} <= _direct_class_bindings(node):
                approved.append((path, node.name))
        if path.is_relative_to(PACKAGE_ROOT):
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Name)
                            and "BACKEND" in target.id
                        ):
                            backend_state.add(target.id)
    assert approved == [
        (PACKAGE_ROOT / "provider.py", "CredentialBackend"),
        (PACKAGE_ROOT / "provider.py", "RejectingCredentialBackend"),
        (WINDOWS_PACKAGE_ROOT / "backend.py", APPROVED_WINDOWS_BACKEND),
    ]
    assert backend_state == {"_REJECTING_BACKEND"}


def test_f9a_current_snapshot_allow_list_is_empty() -> None:
    for path, tree in _src_asts().items():
        if path.is_relative_to(PACKAGE_ROOT):
            continue
        for module in _resolved_import_modules(path, tree):
            if (
                module == "quantforge.credentials"
                or module.startswith("quantforge.credentials.")
            ):
                assert (
                    path in WINDOWS_CURRENT_SNAPSHOT_ALLOW_LIST
                ), path


def test_f9a_prefix_collision_is_not_a_credentials_import() -> None:
    probe = ast.parse(
        "import quantforge.credentials_windows_extra\n"
    )
    probe_path = WINDOWS_PACKAGE_ROOT / "synthetic_probe.py"
    for module in _resolved_import_modules(probe_path, probe):
        assert not (
            module == "quantforge.credentials"
            or module.startswith("quantforge.credentials.")
        )


def test_f9b_protected_roots_never_import_credentials() -> None:
    for path, tree in _src_asts().items():
        relative = path.relative_to(QUANTFORGE_ROOT)
        if relative.parts[0] not in PROTECTED_ROOTS:
            continue
        for module in _resolved_import_modules(path, tree):
            assert not (
                module == "quantforge.credentials"
                or module.startswith("quantforge.credentials.")
            ), path
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                dotted = _dotted_name(node)
                if dotted is not None:
                    assert not dotted.startswith(
                        "quantforge.credentials"
                    ), (path, dotted)


def test_no_secret_derived_identity_primitives_in_the_package() -> None:
    for name, tree in _package_asts().items():
        for module in _resolved_import_modules(PACKAGE_ROOT / name, tree):
            assert module.split(".")[0] not in {"hmac", "secrets", "uuid"}, (
                name,
                module,
            )
        source = (PACKAGE_ROOT / name).read_text(encoding="utf-8")
        assert "hmac" not in source, name
    hashing_files = {
        name
        for name, tree in _package_asts().items()
        if any(
            isinstance(node, ast.Attribute)
            and _dotted_name(node) == "hashlib.sha256"
            for node in ast.walk(tree)
        )
    }
    assert hashing_files == {"models.py"}


def test_oracle_ab_static_fence_and_import_tripwire() -> None:
    for name, tree in _package_asts().items():
        for module in _resolved_import_modules(PACKAGE_ROOT / name, tree):
            assert module.split(".")[0] not in FORBIDDEN_ROOTS, (name, module)
    probe = Path(__file__).with_name("_import_tripwire_probe.py")
    completed = subprocess.run(
        [sys.executable, str(probe)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert completed.stdout.strip() == "CREDENTIAL_TRIPWIRE_OK"


def test_oracle_ai_transport_lock_is_unchanged() -> None:
    adapter_path = QUANTFORGE_ROOT / "execution_ledger" / "adapter.py"
    tree = _parse(adapter_path)
    locked = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name == "LockedBrokerAdapter"
    ]
    assert len(locked) == 1
    methods = {
        node.name: node
        for node in locked[0].body
        if isinstance(node, ast.FunctionDef)
    }
    for name in ("submit", "query_order", "cancel_order"):
        assert name in methods
        assert any(
            isinstance(node, ast.Raise) for node in ast.walk(methods[name])
        ), name
    exception_path = QUANTFORGE_ROOT / "marketdata_alpaca" / "transport.py"
    exception_roots = frozenset({"http", "ssl"})
    for path, module_tree in _src_asts().items():
        for module in _resolved_import_modules(path, module_tree):
            root = module.split(".")[0]
            if path == exception_path and root in exception_roots:
                continue
            assert root not in NETWORK_ROOTS, (path, module)
