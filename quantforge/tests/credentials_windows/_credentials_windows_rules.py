"""Finite AST rules W1..W16 for the CREDENTIAL-002 package.

Every rule is deterministic, test-side, read-only and AST-based over the
path sets frozen by the contract:

    P1  src/quantforge/credentials_windows/**
    P2  src/quantforge/**/*.py
    P3  tests/**/*.py

The identifier positions every name-based rule inspects are exactly the
ones frozen in section 33 of ``tasks/CREDENTIAL-002.md``: ``Name.id``,
``Attribute.attr``, import ``alias.name``/``alias.asname``, class and
function names, ``arg.arg``, ``keyword.arg`` and global/nonlocal names.
Comments, docstrings and string literals are outside this grammar.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
QUANTFORGE_ROOT = SRC_ROOT / "quantforge"
PACKAGE_ROOT = QUANTFORGE_ROOT / "credentials_windows"
CREDENTIALS_ROOT = QUANTFORGE_ROOT / "credentials"
TESTS_ROOT = REPO_ROOT / "tests"
ALLOWED_TEST_ROOT = TESTS_ROOT / "credentials_windows"

# MARKETDATA-002 consumer amendment (frozen section 32 clauses (a) and (b)).
# The production caller and importer allow-lists were exactly EMPTY and now
# name exactly one reviewed consumer module.  No wildcard is introduced.
PRODUCTION_BUILD_CALLER_ALLOW_LIST = frozenset(
    {QUANTFORGE_ROOT / "marketdata_alpaca" / "composition.py"}
)
PRODUCTION_PACKAGE_IMPORTER_ALLOW_LIST = frozenset(
    {QUANTFORGE_ROOT / "marketdata_alpaca" / "composition.py"}
)

PACKAGE_MODULE_NAMES = (
    "__init__.py",
    "backend.py",
    "native.py",
    "composition.py",
)
ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "ctypes",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "json",
        "sys",
        "typing",
    }
)
FORBIDDEN_IMPORT_ROOTS = frozenset(
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
FORBIDDEN_LOADER_NAMES = frozenset(
    {
        "windll",
        "CDLL",
        "PyDLL",
        "OleDLL",
        "LibraryLoader",
        "LoadLibrary",
        "GetProcAddress",
    }
)
FORBIDDEN_PRINCIPAL_NAMES = frozenset(
    {
        "LogonUserW",
        "LogonUserA",
        "ImpersonateLoggedOnUser",
        "RevertToSelf",
        "SetThreadToken",
        "DuplicateToken",
        "DuplicateTokenEx",
        "OpenProcessToken",
        "OpenThreadToken",
        "CreateProcessAsUserW",
        "CreateProcessWithTokenW",
        "CreateProcessWithLogonW",
    }
)
FORBIDDEN_CLOCK_CALLS = frozenset(
    {
        "now",
        "utcnow",
        "today",
        "fromtimestamp",
    }
)
ALLOWED_PUBLIC_NAMES = frozenset(
    {
        "build_credential_boundary",
        "WindowsCredentialManagerBackend",
        "CredentialReadSeam",
        "Advapi32CredentialReadSeam",
    }
)
APPROVED_BACKEND_CLASS = "WindowsCredentialManagerBackend"
APPROVED_SEAM_CLASSES = frozenset(
    {"CredentialReadSeam", "Advapi32CredentialReadSeam"}
)
FORBIDDEN_BASE_NAMES = frozenset(
    {
        "WindowsCredentialManagerBackend",
        "CredentialReadSeam",
        "Advapi32CredentialReadSeam",
        "CredentialBackend",
        "RejectingCredentialBackend",
    }
)
PROTECTED_BOUNDARY_NAME = "CredentialBoundary"
PROTECTED_SECRET_NAMES = ("SecretMaterial", "CredentialLease")
BUILD_FUNCTION_NAME = "build_credential_boundary"
BACKEND_CLASS_NAME = "WindowsCredentialManagerBackend"
SEAM_CLASS_NAME = "Advapi32CredentialReadSeam"
PACKAGE_DOTTED = "quantforge.credentials_windows"
COMPOSITION_PATH = PACKAGE_ROOT / "composition.py"
FORBIDDEN_MODEL_ENTRY_POINTS = frozenset(
    {
        "model_dump",
        "model_dump_json",
        "to_dict",
        "as_dict",
        "dict",
        "json",
    }
)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def parse_source(source: str, path: Path | None = None) -> ast.Module:
    name = str(path) if path is not None else "<fixture>"
    return ast.parse(source, filename=name)


def python_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.py") if "__pycache__" not in path.parts
    )


def package_asts() -> dict[str, ast.Module]:
    return {
        name: parse(PACKAGE_ROOT / name) for name in PACKAGE_MODULE_NAMES
    }


def src_asts() -> dict[Path, ast.Module]:
    return {path: parse(path) for path in python_files(QUANTFORGE_ROOT)}


def test_asts() -> dict[Path, ast.Module]:
    return {path: parse(path) for path in python_files(TESTS_ROOT)}


def dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def identifier_positions(tree: ast.Module) -> list[tuple[str, str]]:
    """Return (kind, name) for every frozen identifier position."""
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append(("name", node.id))
        elif isinstance(node, ast.Attribute):
            found.append(("attribute", node.attr))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                found.append(("import", alias.name))
                if alias.asname:
                    found.append(("import", alias.asname))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(("function", node.name))
            for argument in node.args.args:
                found.append(("argument", argument.arg))
            found.extend(("argument", item.arg) for item in node.args.kwonlyargs)
        elif isinstance(node, ast.ClassDef):
            found.append(("class", node.name))
        elif isinstance(node, ast.arg):
            found.append(("argument", node.arg))
        elif isinstance(node, ast.keyword) and node.arg:
            found.append(("keyword", node.arg))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found.extend(("scope", name) for name in node.names)
    return found


def containing_package(path: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(SRC_ROOT).parts[:-1]
    except ValueError:
        return path.relative_to(REPO_ROOT).parts[:-1]


def resolved_module_for_import(path: Path, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    parts = containing_package(path)
    base = parts[: len(parts) - (node.level - 1)]
    if node.module:
        return ".".join((*base, node.module))
    return ".".join(base)


def resolved_import_modules(path: Path, tree: ast.Module) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolved_module_for_import(path, node)
            if module:
                modules.append(module)
    return modules


def import_bindings(path: Path, tree: ast.Module) -> dict[str, str]:
    """Map module-level bound names to their dotted identity."""
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                key = alias.asname or alias.name.split(".")[0]
                bindings[key] = alias.name if alias.asname else key
        elif isinstance(node, ast.ImportFrom):
            module = resolved_module_for_import(path, node)
            for alias in node.names:
                if alias.name == "*":
                    continue
                key = alias.asname or alias.name
                bindings[key] = (
                    f"{module}.{alias.name}" if module else alias.name
                )
    return bindings


def callee_identity(func: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(func, ast.Name):
        return bindings.get(func.id, func.id)
    if isinstance(func, ast.Attribute):
        text = dotted(func)
        if text is None:
            return None
        root, _, rest = text.partition(".")
        base = bindings.get(root)
        if base is not None:
            return f"{base}.{rest}" if rest else base
        return text
    return None


def definition_names(path: Path, tree: ast.Module) -> list[str]:
    """Public names *defined* by this module (mirrors the frozen oracle)."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            names.append(node.target.id)
    return [name for name in names if not name.startswith("_")]


CLASS_BLOCKS = (
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
    """Class-owned names, descending class-body blocks but not methods."""
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
        elif isinstance(node, CLASS_BLOCKS):
            for nested in _nested_statement_lists(node):
                found |= _class_level_names(nested)
    return found


def _instance_attribute_names(node: ast.ClassDef) -> set[str]:
    found: set[str] = set()
    for inner in ast.walk(node):
        targets: list[ast.AST] = []
        if isinstance(inner, ast.Assign):
            targets = list(inner.targets)
        elif isinstance(inner, ast.AnnAssign):
            targets = [inner.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                found.add(target.attr)
    return found


def class_bindings(node: ast.ClassDef) -> set[str]:
    """Class-owned bindings plus self.<name> instance attributes."""
    return _class_level_names(node.body) | _instance_attribute_names(node)


def _is_literal_constant(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _is_literal_constant(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal_constant(item) for item in node.elts)
    return False


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            names.add(node.target.id)
    return names


# --------------------------------------------------------------------------
# P1 rules
# --------------------------------------------------------------------------


def w1_package_file_set(package: dict[str, ast.Module]) -> list[str]:
    actual = tuple(sorted((PACKAGE_ROOT / name).name for name in package))
    expected = tuple(sorted(PACKAGE_MODULE_NAMES))
    return [] if actual == expected else [f"W1 file set {actual}"]


def w2_import_allow_list(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    for name, tree in package.items():
        path = PACKAGE_ROOT / name
        for module in resolved_import_modules(path, tree):
            root = module.split(".")[0]
            if module.startswith(PACKAGE_DOTTED):
                continue
            if module == "quantforge.credentials" or module.startswith(
                "quantforge.credentials."
            ):
                continue
            if root in ALLOWED_IMPORT_ROOTS or root in {"quantforge"}:
                if root == "quantforge":
                    problems.append(f"W2 forbidden root {module} in {name}")
                continue
            problems.append(f"W2 forbidden import {module} in {name}")
    return problems


def w3_forbidden_builtins(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    for name, tree in package.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_BUILTINS:
                    problems.append(f"W3 forbidden call {node.func.id} {name}")
                if node.func.id == "type":
                    starred = any(
                        isinstance(argument, ast.Starred)
                        for argument in node.args
                    )
                    if len(node.args) != 1 or starred:
                        problems.append(f"W3 dynamic type() in {name}")
    return problems


def w4_no_clock_reads(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    for name, tree in package.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            text = dotted(node.func)
            if text is None:
                continue
            tail = text.split(".")[-1]
            if tail in FORBIDDEN_CLOCK_CALLS:
                problems.append(f"W4 clock read {text} in {name}")
            if text.startswith("time."):
                problems.append(f"W4 clock module {text} in {name}")
    return problems


def w5_backend_class_bindings(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    for name, tree in package.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bindings = class_bindings(node)
            bases = {
                item
                for base in node.bases
                for item in (dotted(base),)
                if item is not None
            }
            if (
                {"provider_id", "acquire"} & bindings
                and node.name != APPROVED_BACKEND_CLASS
            ):
                problems.append(f"W5 backend binder {node.name} in {name}")
            if "read" in bindings and node.name not in APPROVED_SEAM_CLASSES:
                problems.append(f"W5 read binder {node.name} in {name}")
            for base in bases:
                tail = base.split(".")[-1]
                if tail in FORBIDDEN_BASE_NAMES:
                    problems.append(f"W5 forbidden base {base} in {name}")
    return problems


def w6_native_loading(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    win_dll_calls: list[tuple[str, bool]] = []
    prototype_assignments: set[str] = set()
    for name, tree in package.items():
        module_level = _module_level_names(tree)
        for node in ast.walk(tree):
            for kind, value in identifier_positions(
                ast.Module(body=[node], type_ignores=[])
            ):
                if value in FORBIDDEN_LOADER_NAMES:
                    problems.append(f"W6 loader name {value} in {name}")
            if isinstance(node, ast.Attribute):
                if node.attr in FORBIDDEN_LOADER_NAMES:
                    problems.append(f"W6 loader attribute {node.attr} {name}")
                if (
                    node.attr in {"wintypes", "windll", "WinDLL"}
                    and isinstance(node.value, ast.Name)
                    and node.value.id in module_level
                ):
                    problems.append(f"W6 module-level {node.attr} in {name}")
            if isinstance(node, ast.Call):
                text = dotted(node.func)
                if text == "ctypes.WinDLL":
                    literal = (
                        isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == "advapi32"
                    )
                    win_dll_calls.append((name, literal))
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr in {"restype", "argtypes"}
                    ):
                        prototype_assignments.add(target.attr)
    if len(win_dll_calls) != 1:
        problems.append(f"W6 WinDLL call count {len(win_dll_calls)}")
    else:
        name, literal = win_dll_calls[0]
        if name != "native.py" or not literal:
            problems.append(f"W6 WinDLL form in {name}")
    if prototype_assignments != {"restype", "argtypes"}:
        problems.append(f"W6 prototype binding {sorted(prototype_assignments)}")
    return problems


def _matches_cred_api_symbol(name: str) -> bool:
    if not (name.startswith("Cred") and name.endswith("W")):
        return False
    middle = name[4:-1]
    return bool(middle) and all(
        character.isascii() and character.isalpha() for character in middle
    )


def w7_credential_symbols(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    allowed = {"CredReadW", "CredFree"}
    for name, tree in package.items():
        for _kind, value in identifier_positions(tree):
            if value in allowed:
                continue
            if _matches_cred_api_symbol(value):
                problems.append(f"W7 credential symbol {value} in {name}")
            if value in FORBIDDEN_PRINCIPAL_NAMES:
                problems.append(f"W16 principal symbol {value} in {name}")
    return problems


def w8_module_level_values(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    for name, tree in package.items():
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is not None and not _is_literal_constant(value):
                problems.append(f"W8 module value in {name}")
    return problems


def w9_no_serialization_or_rendering(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    for name, tree in package.items():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                problems.append(f"W9 print in {name}")
            if isinstance(node, ast.ClassDef):
                methods = {
                    item.name
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                if methods & FORBIDDEN_MODEL_ENTRY_POINTS:
                    problems.append(f"W9 serialization entry in {name}")
                for item in node.body:
                    if not isinstance(item, ast.FunctionDef):
                        continue
                    if item.name not in {"__repr__", "__str__"}:
                        continue
                    returns = [
                        statement.value
                        for statement in ast.walk(item)
                        if isinstance(statement, ast.Return)
                    ]
                    for value in returns:
                        if isinstance(value, ast.Constant):
                            continue
                        if isinstance(value, ast.Name):
                            continue
                        problems.append(f"W9 rendered data in {name}.{item.name}")
    for name, tree in package.items():
        if "logging" in {
            module.split(".")[0]
            for module in resolved_import_modules(PACKAGE_ROOT / name, tree)
        }:
            problems.append(f"W9 logging import in {name}")
    return problems


def w10_protected_name_absence(
    src: dict[Path, ast.Module], package: dict[str, ast.Module]
) -> list[str]:
    problems: list[str] = []
    for path, tree in src.items():
        if path.is_relative_to(PACKAGE_ROOT):
            continue
        if path.is_relative_to(CREDENTIALS_ROOT):
            continue
        for _kind, value in identifier_positions(tree):
            if value == PROTECTED_BOUNDARY_NAME:
                problems.append(f"W10 boundary name in {path}")
    for name, tree in package.items():
        for _kind, value in identifier_positions(tree):
            if value in PROTECTED_SECRET_NAMES:
                problems.append(f"W10 secret name {value} in {name}")
    composition = package.get("composition.py")
    if composition is not None:
        occurrences = [
            value
            for _kind, value in identifier_positions(composition)
            if value == PROTECTED_BOUNDARY_NAME
        ]
        if len(occurrences) != 2:
            problems.append("W10 composition occurrence count")
        calls = [
            node
            for node in ast.walk(composition)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == PROTECTED_BOUNDARY_NAME
        ]
        if len(calls) != 1:
            problems.append("W10 composition construction count")
    return problems


def _package_identity(suffix: str) -> tuple[str, str]:
    return f"{PACKAGE_DOTTED}.{suffix}", f"{PACKAGE_DOTTED}.{suffix}"


def build_function_identities() -> set[str]:
    return {
        f"{PACKAGE_DOTTED}.{BUILD_FUNCTION_NAME}",
        f"{PACKAGE_DOTTED}.composition.{BUILD_FUNCTION_NAME}",
    }


def construction_identities() -> set[str]:
    return {
        f"{PACKAGE_DOTTED}.backend.{BACKEND_CLASS_NAME}",
        f"{PACKAGE_DOTTED}.native.{SEAM_CLASS_NAME}",
    }


def w11_build_callers(modules: dict[Path, ast.Module]) -> list[str]:
    problems: list[str] = []
    identities = build_function_identities()
    for path, tree in modules.items():
        bindings = import_bindings(path, tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            identity = callee_identity(node.func, bindings)
            if identity not in identities:
                continue
            if path.is_relative_to(ALLOWED_TEST_ROOT):
                continue
            if path in PRODUCTION_BUILD_CALLER_ALLOW_LIST:
                continue
            problems.append(f"W11 build caller in {path}")
    return problems


def w12_construction_sites(modules: dict[Path, ast.Module]) -> list[str]:
    problems: list[str] = []
    identities = construction_identities()
    allowed_production = {COMPOSITION_PATH}
    for path, tree in modules.items():
        bindings = import_bindings(path, tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            identity = callee_identity(node.func, bindings)
            if identity not in identities:
                continue
            if path.is_relative_to(ALLOWED_TEST_ROOT):
                continue
            if path in allowed_production:
                continue
            problems.append(f"W12 construction in {path}")
    return problems


def w13_package_importers(modules: dict[Path, ast.Module]) -> list[str]:
    problems: list[str] = []
    for path, tree in modules.items():
        for module in resolved_import_modules(path, tree):
            if module == PACKAGE_DOTTED or module.startswith(
                PACKAGE_DOTTED + "."
            ):
                if path.is_relative_to(PACKAGE_ROOT):
                    continue
                if path.is_relative_to(ALLOWED_TEST_ROOT):
                    continue
                if path in PRODUCTION_PACKAGE_IMPORTER_ALLOW_LIST:
                    continue
                problems.append(f"W13 importer {path} -> {module}")
    return problems


def w14_public_surface(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    init = package["__init__.py"]
    values = {
        node.targets[0].id: node.value
        for node in init.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
    }
    all_node = values.get("__all__")
    expected = ("build_credential_boundary",)
    if not isinstance(all_node, ast.Tuple):
        problems.append("W14 __all__ missing")
    else:
        actual = tuple(
            element.value
            for element in all_node.elts
            if isinstance(element, ast.Constant)
        )
        if actual != expected or len(actual) != len(all_node.elts):
            problems.append("W14 __all__ value")
    for name, tree in package.items():
        for defined in definition_names(PACKAGE_ROOT / name, tree):
            if defined not in ALLOWED_PUBLIC_NAMES:
                problems.append(f"W14 public name {defined} in {name}")
    return problems


def w15_native_call_sites(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    read_names: set[str] = set()
    free_names: set[str] = set()
    read_calls = 0
    free_calls = 0
    free_inside_finally = False
    for tree in package.values():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Attribute)
                and isinstance(node.targets[0].value, ast.Name)
                and node.targets[0].value.id == "self"
                and isinstance(node.value, ast.Attribute)
            ):
                if node.value.attr == "CredReadW":
                    read_names.add(node.targets[0].attr)
                elif node.value.attr == "CredFree":
                    free_names.add(node.targets[0].attr)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                if node.func.attr in read_names:
                    read_calls += 1
                if node.func.attr in free_names:
                    free_calls += 1
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for statement in node.finalbody:
                for inner in ast.walk(statement):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and isinstance(inner.func.value, ast.Name)
                        and inner.func.value.id == "self"
                        and inner.func.attr in free_names
                    ):
                        free_inside_finally = True
    if read_calls != 1:
        problems.append(f"W15 CredReadW call sites {read_calls}")
    if free_calls != 1:
        problems.append(f"W15 CredFree call sites {free_calls}")
    if not free_inside_finally:
        problems.append("W15 CredFree not inside try/finally")
    if not read_names or not free_names:
        problems.append("W15 native function bindings missing")
    return problems


def _binding_text(target: ast.AST) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    return dotted(target)


def w16_handle_authority(package: dict[str, ast.Module]) -> list[str]:
    problems: list[str] = []
    handle_bindings: set[str] = set()
    handle_assignments = 0
    for tree in package.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (
                isinstance(value, ast.Call)
                and dotted(value.func) == "ctypes.WinDLL"
            ):
                continue
            handle_assignments += 1
            for target in node.targets:
                text = _binding_text(target)
                if text is None:
                    problems.append("W16 handle form")
                else:
                    handle_bindings.add(text)
    if handle_assignments != 1 or len(handle_bindings) != 1:
        problems.append(f"W16 handle bindings {sorted(handle_bindings)}")
    for name, tree in package.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                owner = dotted(node.value)
                if (
                    owner in handle_bindings
                    and node.attr not in {"CredReadW", "CredFree"}
                ):
                    problems.append(
                        f"W16 handle attribute {node.attr} in {name}"
                    )
            if (
                isinstance(node, ast.Subscript)
                and dotted(node.value) in handle_bindings
            ):
                problems.append(f"W16 handle subscript in {name}")
            if (
                isinstance(node, ast.Call)
                and dotted(node.func) in {"getattr", "builtins.getattr"}
                and node.args
                and dotted(node.args[0]) in handle_bindings
            ):
                problems.append(f"W16 dynamic handle in {name}")
    return problems


PACKAGE_RULES = (
    ("W1", w1_package_file_set),
    ("W2", w2_import_allow_list),
    ("W3", w3_forbidden_builtins),
    ("W4", w4_no_clock_reads),
    ("W5", w5_backend_class_bindings),
    ("W6", w6_native_loading),
    ("W7", w7_credential_symbols),
    ("W8", w8_module_level_values),
    ("W9", w9_no_serialization_or_rendering),
    ("W14", w14_public_surface),
    ("W15", w15_native_call_sites),
    ("W16", w16_handle_authority),
)
