"""Finite AST rules R1-R17 for the frozen MARKETDATA-002 package.

Every rule is deterministic, read-only, AST-based and test-side.  The
traversal, path sets, identifier positions, import-resolution grammar,
string-constant scan and call-site identities are exactly the ones frozen by
section 27 of ``tasks/MARKETDATA-002.md``.  The declared non-goals of the
finite grammar are recorded in that section and are never restated here as
mechanically proven.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
QUANTFORGE_ROOT = SRC_ROOT / "quantforge"
PACKAGE_ROOT = QUANTFORGE_ROOT / "marketdata_alpaca"
TESTS_ROOT = REPO_ROOT / "tests"
ALLOWED_TEST_ROOT = TESTS_ROOT / "marketdata_alpaca"
TRANSPORT_PATH = PACKAGE_ROOT / "transport.py"
ORACLE_AI_NETWORK_EXCEPTION_PATH = TRANSPORT_PATH
ORACLE_AI_NETWORK_EXCEPTION_ROOTS = frozenset({"http", "ssl"})
SOURCE_PATH = PACKAGE_ROOT / "source.py"
COMPOSITION_PATH = PACKAGE_ROOT / "composition.py"
SMOKE_PATH = QUANTFORGE_ROOT / "marketdata_smoke.py"
MODULE_NAMES = (
    "__init__.py",
    "transport.py",
    "client.py",
    "source.py",
    "composition.py",
)
PUBLIC_DEFINED_NAMES = frozenset(
    {
        "AlpacaIexLatestTradeSource",
        "build_alpaca_iex_source",
        "TransportResponse",
        "HttpsGet",
    }
)
PROTECTED_CREDENTIAL_NAMES = frozenset(
    {"CredentialBoundary", "SecretMaterial", "CredentialLease"}
)
ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "hashlib",
        "json",
        "re",
        "typing",
    }
)
ALLOWED_QUANTFORGE_IMPORTS = {
    "transport.py": ("http", "ssl"),
    "composition.py": (
        "quantforge.credentials",
        "quantforge.credentials_windows",
    ),
    "source.py": ("quantforge.marketdata", "quantforge.provenance"),
}
NETWORK_ROOTS = frozenset(
    {
        "socket",
        "urllib",
        "urllib3",
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
        "selectors",
        "socketserver",
    }
)
SDK_ROOTS = frozenset(
    {
        "alpaca",
        "alpaca_py",
        "alpaca_trade_api",
        "ib_insync",
        "ibapi",
        "ccxt",
        "binance",
        "coinbase",
        "yfinance",
        "polygon",
    }
)
CAPABILITY_ROOTS = frozenset(
    {
        "os",
        "sys",
        "getpass",
        "keyring",
        "dotenv",
        "pathlib",
        "io",
        "tempfile",
        "shutil",
        "subprocess",
        "multiprocessing",
        "ctypes",
        "pty",
        "importlib",
        "pickle",
        "marshal",
        "shelve",
        "random",
        "uuid",
        "secrets",
        "time",
        "sched",
        "logging",
        "sqlite3",
        "duckdb",
    }
)
DURABLE_ROOTS = frozenset(
    {
        "quantforge.execstate",
        "quantforge.execution_ledger",
        "quantforge.operator",
        "quantforge.broker",
        "quantforge.live",
        "quantforge.predispatch",
        "quantforge.autolab",
        "quantforge.data",
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
CLOCK_IDENTITIES = frozenset(
    {
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.datetime.fromtimestamp",
        "datetime.date.today",
    }
)
FORBIDDEN_HOST_LITERALS = frozenset(
    {
        "http://",
        "https://",
        "api.alpaca.markets",
        "paper-api.alpaca.markets",
        "alpaca.markets",
    }
)
FORBIDDEN_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CONNECTION_ATTRIBUTES = frozenset(
    {"putrequest", "putheader", "endheaders", "getresponse", "close"}
)
RESPONSE_ATTRIBUTES = frozenset({"status", "getheader", "read"})
FORBIDDEN_PROXY_NAMES = frozenset(
    {
        "set_tunnel",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "HTTPRedirectHandler",
        "RedirectHandler",
    }
)
FORBIDDEN_RETRY_NAMES = frozenset({"retry", "backoff", "sleep"})
SERIALIZATION_ATTRIBUTES = frozenset(
    {"model_dump", "model_dump_json", "dict", "json", "to_dict", "as_dict"}
)
EXPECTED_PUTHEADER_NAMES = (
    "APCA-API-KEY-ID",
    "APCA-API-SECRET-KEY",
    "Accept",
    "Accept-Encoding",
)
TRIPWIRE_ALLOWANCE = Path("quantforge/marketdata_alpaca/source.py")


def python_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def package_sources() -> dict[str, ast.Module]:
    return {
        name: _parse(PACKAGE_ROOT / name)
        for name in MODULE_NAMES
        if (PACKAGE_ROOT / name).is_file()
    }


def source_asts() -> dict[Path, ast.Module]:
    return {path: _parse(path) for path in python_files(QUANTFORGE_ROOT)}


def test_asts() -> dict[Path, ast.Module]:
    return {path: _parse(path) for path in python_files(TESTS_ROOT)}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def parse_source(source: str, path: Path) -> ast.Module:
    return ast.parse(source, filename=str(path))


def dotted_module(path: Path) -> str:
    if path.is_relative_to(SRC_ROOT):
        relative = path.relative_to(SRC_ROOT).with_suffix("")
    elif path.is_relative_to(TESTS_ROOT):
        relative = path.relative_to(TESTS_ROOT).with_suffix("")
    else:
        relative = path.with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def import_aliases(
    tree: ast.Module, path: Path | None = None
) -> dict[str, str]:
    """Map every import-bound local name to its dotted identity."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                aliases[bound] = bound
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and path is not None:
                module_name = dotted_module(path)
                parts = module_name.split(".") if module_name else []
                if path.name == "__init__.py":
                    package_parts = parts
                else:
                    package_parts = parts[:-1]
                keep = len(package_parts) - (node.level - 1)
                base = ".".join(package_parts[:keep])
                module = (base + "." + module) if base and module else (
                    base or module
                )
            for alias in node.names:
                bound = alias.asname or alias.name
                prefix = module + "." if module else ""
                aliases[bound] = prefix + alias.name
    return aliases


def resolved_import_modules(path: Path, tree: ast.Module) -> list[str]:
    """Resolve every import of a file, including relative imports."""
    modules: list[str] = []
    module_name = dotted_module(path)
    parts = module_name.split(".") if module_name else []
    if path.name == "__init__.py":
        package_parts = parts
    else:
        package_parts = parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package_parts) - (node.level - 1)
                base = ".".join(package_parts[:keep])
                if node.module:
                    module = (base + "." + node.module) if base else node.module
                else:
                    module = base
            else:
                module = node.module or ""
            if module:
                modules.append(module)
    return modules


def identifier_positions(tree: ast.Module) -> list[tuple[str, str]]:
    """Yield every frozen identifier position with its kind."""
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
                    found.append(("asname", alias.asname))
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            found.append(("definition", node.name))
        elif isinstance(node, ast.arg):
            found.append(("argument", node.arg))
        elif isinstance(node, ast.keyword) and node.arg is not None:
            found.append(("keyword", node.arg))
        elif isinstance(node, ast.Global):
            for name in node.names:
                found.append(("global", name))
        elif isinstance(node, ast.Nonlocal):
            for name in node.names:
                found.append(("nonlocal", name))
    return found


def string_constants(tree: ast.Module) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def parents_of(tree: ast.Module) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def inside_loop(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, (ast.For, ast.AsyncFor, ast.While)):
            return True
        current = parents.get(id(current))
    return False


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        if base is None:
            return None
        return base + "." + node.attr
    return None


def resolve(node: ast.AST, aliases: dict[str, str]) -> str | None:
    dotted = dotted_name(node)
    if dotted is None:
        return None
    first, _, rest = dotted.partition(".")
    base = aliases.get(first)
    if base is None:
        return dotted
    return base + ("." + rest if rest else "")


def r1_file_set() -> list[str]:
    problems: list[str] = []
    if not PACKAGE_ROOT.is_dir():
        return ["R1 package directory is missing"]
    present = sorted(path.name for path in PACKAGE_ROOT.glob("*.py"))
    if present != sorted(MODULE_NAMES):
        problems.append(f"R1 package file set is {present}")
    return problems


def r2_network_import_authority() -> list[str]:
    problems: list[str] = []
    for path in python_files(QUANTFORGE_ROOT):
        tree = _parse(path)
        roots = {
            module.split(".")[0]
            for module in resolved_import_modules(path, tree)
        }
        if path != TRANSPORT_PATH and roots & {"http", "ssl"}:
            problems.append(f"R2 network import outside transport in {path}")
    for name, tree in package_sources().items():
        path = PACKAGE_ROOT / name
        for module in resolved_import_modules(path, tree):
            root = module.split(".")[0]
            if root in NETWORK_ROOTS or root in SDK_ROOTS:
                problems.append(f"R2 forbidden network root {root} in {name}")
    return problems


def r3_capability_denylist() -> list[str]:
    problems: list[str] = []
    for name, tree in package_sources().items():
        path = PACKAGE_ROOT / name
        for module in resolved_import_modules(path, tree):
            root = module.split(".")[0]
            if root in CAPABILITY_ROOTS:
                problems.append(
                    f"R3 forbidden capability root {root} in {name}"
                )
    return problems


def r4_forbidden_calls() -> list[str]:
    problems: list[str] = []
    for name, tree in package_sources().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_BUILTINS:
                    problems.append(
                        f"R4 forbidden call {node.func.id} in {name}"
                    )
                if node.func.id == "type":
                    plain = [
                        argument
                        for argument in node.args
                        if not isinstance(argument, ast.Starred)
                    ]
                    if len(plain) != 1:
                        problems.append(f"R4 dynamic type call in {name}")
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_BUILTINS:
                    problems.append(
                        f"R4 forbidden call .{node.func.attr} in {name}"
                    )
    return problems


def r5_no_clock_access() -> list[str]:
    problems: list[str] = []
    constructions: list[tuple[str, ast.Call, dict[str, str]]] = []
    for name, tree in package_sources().items():
        aliases = import_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                resolved = resolve(node, aliases)
                if resolved is None:
                    continue
                if resolved in CLOCK_IDENTITIES:
                    problems.append(f"R5 clock accessor {resolved} in {name}")
                if resolved.split(".")[0] in {"time", "sched"}:
                    problems.append(f"R5 time accessor {resolved} in {name}")
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name
            ) and aliases.get(node.func.id) == "datetime.datetime":
                constructions.append((name, node, aliases))
    if len(constructions) != 1 or constructions[0][0] != "composition.py":
        problems.append("R5 datetime construction is not the frozen literal")
        return problems
    _, call, aliases = constructions[0]
    arguments = list(call.args) + [
        keyword.value for keyword in call.keywords
    ]
    for argument in arguments:
        if isinstance(argument, ast.Constant):
            continue
        if isinstance(argument, ast.Name) and aliases.get(
            argument.id
        ) == "datetime.UTC":
            continue
        problems.append("R5 datetime literal argument is not frozen")
    return problems


def r6_credential_locality() -> list[str]:
    problems: list[str] = []
    credential_modules = (
        "quantforge.credentials",
        "quantforge.credentials_windows",
    )
    for name, tree in package_sources().items():
        path = PACKAGE_ROOT / name
        for module in resolved_import_modules(path, tree):
            for prefix in credential_modules:
                if (
                    (module == prefix or module.startswith(prefix + "."))
                    and path != COMPOSITION_PATH
                ):
                    problems.append(
                        f"R6 credential import in {name}: {module}"
                    )
    for name, tree in package_sources().items():
        reveal_sites = 0
        for kind, value in identifier_positions(tree):
            if kind == "name" and value in PROTECTED_CREDENTIAL_NAMES:
                problems.append(f"R6 protected name {value} in {name}")
            if kind == "attribute" and value == "reveal":
                reveal_sites += 1
        if name == "composition.py":
            if reveal_sites != 1:
                problems.append("R6 reveal call site count")
        elif reveal_sites:
            problems.append(f"R6 reveal outside composition in {name}")
    callers: list[Path] = []
    for path in python_files(QUANTFORGE_ROOT):
        tree = _parse(path)
        aliases = import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            resolved = resolve(node.func, aliases)
            if resolved in {
                "quantforge.credentials_windows.build_credential_boundary",
                ("quantforge.credentials_windows.composition."
                "build_credential_boundary"),
            }:
                callers.append(path)
    if callers != [COMPOSITION_PATH]:
        problems.append(f"R6 build callers are {callers}")
    return problems


def r7_price_locality() -> list[str]:
    """Bound the new package's PRICE-001 imports and the marketdata tripwire.

    Within the new package only ``source.py`` may import the public
    ``quantforge.marketdata`` module (never a submodule) or
    ``quantforge.provenance``.  Outside the frozen ``quantforge.marketdata``
    package, ``quantforge.marketdata`` may be imported only by that same
    ``source.py``; the frozen baseline importers of ``quantforge.provenance``
    are pre-existing and are not this rule's subject.
    """
    problems: list[str] = []
    for path in python_files(QUANTFORGE_ROOT):
        tree = _parse(path)
        for module in resolved_import_modules(path, tree):
            if path.is_relative_to(PACKAGE_ROOT):
                if module == "quantforge.marketdata" and path not in {
                    SOURCE_PATH,
                    SMOKE_PATH,
                }:
                    problems.append(f"R7 marketdata import in {path}")
                if module.startswith("quantforge.marketdata."):
                    problems.append(f"R7 private marketdata import in {path}")
                if (
                    module == "quantforge.provenance"
                    and path != SOURCE_PATH
                ):
                    problems.append(f"R7 provenance import in {path}")
                continue
            if path.is_relative_to(QUANTFORGE_ROOT / "marketdata"):
                continue
            if module == "quantforge.marketdata":
                if path != SMOKE_PATH:
                    problems.append(f"R7 marketdata importer {path}")
            elif module.startswith("quantforge.marketdata."):
                problems.append(f"R7 marketdata importer {path}")
    return problems


def r8_literal_and_verb_authority() -> list[str]:
    problems: list[str] = []
    host_total = 0
    for name, tree in package_sources().items():
        for literal in string_constants(tree):
            if literal == "data.alpaca.markets":
                host_total += 1
            if literal in FORBIDDEN_HOST_LITERALS:
                problems.append(f"R8 forbidden host literal in {name}")
            if literal.upper() in FORBIDDEN_VERBS:
                problems.append(f"R8 mutation verb literal in {name}")
        for kind, value in identifier_positions(tree):
            if value in FORBIDDEN_PROXY_NAMES:
                problems.append(f"R8 forbidden name {value} in {name}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if isinstance(node.value, ast.Name):
                if (
                    node.value.id == "connection"
                    and node.attr not in CONNECTION_ATTRIBUTES
                ):
                    problems.append(
                        f"R8 connection attribute {node.attr} in {name}"
                    )
                if (
                    node.value.id == "response"
                    and node.attr not in RESPONSE_ATTRIBUTES
                ):
                    problems.append(
                        f"R8 response attribute {node.attr} in {name}"
                    )
    if host_total != 1:
        problems.append(f"R8 vendor host constant count is {host_total}")
    return problems


def _fetch_alias_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "_fetch"
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def r9_single_attempt_authority() -> list[str]:
    problems: list[str] = []
    fetch_total = 0
    transport_total = 0
    for name, tree in package_sources().items():
        parents = parents_of(tree)
        if any(isinstance(node, ast.While) for node in ast.walk(tree)):
            problems.append(f"R9 while statement in {name}")
        fetch_aliases = _fetch_alias_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            is_fetch = False
            if isinstance(callee, ast.Attribute) and callee.attr == "_fetch" or isinstance(callee, ast.Name) and callee.id in fetch_aliases:
                is_fetch = True
            if is_fetch:
                fetch_total += 1
                if inside_loop(node, parents):
                    problems.append(f"R9 fetch call inside a loop in {name}")
            if (
                isinstance(callee, ast.Attribute)
                and callee.attr == "get"
                and isinstance(callee.value, ast.Attribute)
                and callee.value.attr == "_transport"
            ):
                transport_total += 1
                if inside_loop(node, parents):
                    problems.append(f"R9 transport call in a loop in {name}")
        for kind, value in identifier_positions(tree):
            if value in FORBIDDEN_RETRY_NAMES:
                problems.append(f"R9 retry name {value} in {name}")
    if fetch_total != 1:
        problems.append(f"R9 fetch call sites are {fetch_total}")
    if transport_total != 1:
        problems.append(f"R9 transport call sites are {transport_total}")
    return problems


def _assigned_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            names.add(node.target.id)
    return names


def _defined_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    names.update(_assigned_names(tree))
    return names


def r10_surface_and_state() -> list[str]:
    problems: list[str] = []
    package = package_sources()
    init = package.get("__init__.py")
    if init is None:
        problems.append("R10 missing __init__.py")
    else:
        exported: ast.AST | None = None
        for node in init.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__all__"
            ):
                exported = node.value
        if not isinstance(exported, ast.Tuple):
            problems.append("R10 __all__ is not a tuple")
        else:
            values = tuple(
                element.value
                for element in exported.elts
                if isinstance(element, ast.Constant)
            )
            if values != ("build_alpaca_iex_source",):
                problems.append(f"R10 __all__ is {values}")
    defined: set[str] = set()
    for tree in package.values():
        defined.update(_defined_names(tree))
    public = {value for value in defined if not value.startswith("_")}
    if public != PUBLIC_DEFINED_NAMES:
        problems.append(f"R10 public definitions are {sorted(public)}")
    for name, tree in package.items():
        for node in tree.body:
            value: ast.AST | None = None
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
            if value is None:
                continue
            if isinstance(value, ast.Constant):
                continue
            if isinstance(value, ast.Tuple) and all(
                isinstance(element, ast.Constant)
                for element in value.elts
            ):
                continue
            if (
                name == "composition.py"
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "datetime"
            ):
                continue
            problems.append(f"R10 module-level value in {name}")
    return problems


def r11_durability() -> list[str]:
    problems: list[str] = []
    for name, tree in package_sources().items():
        path = PACKAGE_ROOT / name
        for module in resolved_import_modules(path, tree):
            for prefix in DURABLE_ROOTS:
                if module == prefix or module.startswith(prefix + "."):
                    problems.append(f"R11 durable import {module} in {name}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                problems.append(f"R11 print call in {name}")
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in SERIALIZATION_ATTRIBUTES
            ):
                problems.append(
                    f"R11 serialization call .{node.func.attr} in {name}"
                )
    return problems


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def r12_amendment_parity() -> list[str]:
    problems: list[str] = []
    windows_rules = load_module(
        "marketdata_alpaca_windows_rules_probe",
        TESTS_ROOT / "credentials_windows" / "_credentials_windows_rules.py",
    )
    expected_production = frozenset({COMPOSITION_PATH})
    if (
        windows_rules.PRODUCTION_BUILD_CALLER_ALLOW_LIST
        != expected_production
    ):
        problems.append("R12 W11 production caller allow-list")
    if (
        windows_rules.PRODUCTION_PACKAGE_IMPORTER_ALLOW_LIST
        != expected_production
    ):
        problems.append("R12 W13 production importer allow-list")
    credentials_static = load_module(
        "marketdata_alpaca_credentials_static_probe",
        TESTS_ROOT / "credentials" / "test_credentials_static_enforcement.py",
    )
    expected_snapshot = (
        frozenset(
            credentials_static.WINDOWS_PACKAGE_ROOT / module
            for module in credentials_static.WINDOWS_MODULE_NAMES
        )
        | expected_production
    )
    if (
        credentials_static.WINDOWS_CURRENT_SNAPSHOT_ALLOW_LIST
        != expected_snapshot
    ):
        problems.append("R12 F9(a) current-snapshot allow-list")
    marketdata_static = load_module(
        "marketdata_alpaca_marketdata_static_probe",
        TESTS_ROOT / "marketdata" / "test_static_enforcement.py",
    )
    if (
        marketdata_static.MARKETDATA_ALPACA_IMPORTER_ALLOW_LIST
        != frozenset(
            {TRIPWIRE_ALLOWANCE, Path("quantforge/marketdata_smoke.py")}
        )
    ):
        problems.append("R12 PRICE-001 dependency tripwire allowance")
    if ORACLE_AI_NETWORK_EXCEPTION_PATH != TRANSPORT_PATH:
        problems.append("R12 Oracle AI exception path")
    if ORACLE_AI_NETWORK_EXCEPTION_ROOTS != frozenset({"http", "ssl"}):
        problems.append("R12 Oracle AI exception roots")
    return problems


def r13_import_allow_list() -> list[str]:
    problems: list[str] = []
    for name, tree in package_sources().items():
        path = PACKAGE_ROOT / name
        for module in resolved_import_modules(path, tree):
            root = module.split(".")[0]
            if root == "quantforge":
                if module.startswith("quantforge.marketdata_alpaca"):
                    continue
                allowed = ALLOWED_QUANTFORGE_IMPORTS.get(name, ())
                if not any(
                    module == entry or module.startswith(entry + ".")
                    for entry in allowed
                ):
                    problems.append(f"R13 quantforge import {module} in {name}")
                continue
            if root in {"http", "ssl"}:
                if name != "transport.py":
                    problems.append(f"R13 network import {module} in {name}")
                continue
            if root not in ALLOWED_IMPORT_ROOTS:
                problems.append(f"R13 import root {root} in {name}")
    return problems


def _constant_table(tree: ast.Module) -> dict[str, ast.Constant]:
    table: dict[str, ast.Constant] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(
            node.value, ast.Constant
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    table[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
        ):
            table[node.target.id] = node.value
    return table


def _resolve_constant(
    node: ast.AST, constants: dict[str, ast.Constant]
) -> ast.Constant | None:
    if isinstance(node, ast.Constant):
        return node
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _literal_name(
    node: ast.Call, constants: dict[str, ast.Constant]
) -> str | None:
    if not node.args:
        return None
    resolved = _resolve_constant(node.args[0], constants)
    if resolved is not None and isinstance(resolved.value, str):
        return resolved.value
    return None


def _keyword_map(call: ast.Call) -> dict[str, ast.AST]:
    return {
        keyword.arg: keyword.value
        for keyword in call.keywords
        if keyword.arg is not None
    }


def _call_details() -> tuple[dict[str, int], dict[str, object]]:
    counts = {
        "context": 0,
        "connection": 0,
        "putrequest": 0,
        "putheader": 0,
        "endheaders": 0,
        "getresponse": 0,
        "read": 0,
        "close": 0,
    }
    constants: dict[str, ast.Constant] = {}
    putheader_names: list[str | None] = []
    connection_call: ast.Call | None = None
    putrequest_call: ast.Call | None = None
    ssl_attributes: list[str] = []
    forbidden: list[str] = []
    for tree in package_sources().values():
        aliases = import_aliases(tree)
        constants.update(_constant_table(tree))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and resolve(
                node.value, aliases
            ) == "ssl":
                ssl_attributes.append(node.attr)
            if not isinstance(node, ast.Call):
                continue
            resolved = resolve(node.func, aliases)
            if resolved == "ssl.create_default_context":
                counts["context"] += 1
            elif resolved == "http.client.HTTPSConnection":
                counts["connection"] += 1
                connection_call = node
            elif resolved == "http.client.HTTPConnection":
                forbidden.append("http.client.HTTPConnection")
            elif isinstance(node.func, ast.Attribute) and isinstance(
                node.func.value, ast.Name
            ):
                base = node.func.value.id
                attribute = node.func.attr
                if base == "connection":
                    if attribute in counts:
                        counts[attribute] += 1
                    if attribute == "putrequest":
                        putrequest_call = node
                    if attribute == "putheader":
                        putheader_names.append(
                            _literal_name(node, constants)
                        )
                    if attribute in {
                        "request",
                        "connect",
                        "set_tunnel",
                        "send",
                        "sendall",
                    }:
                        forbidden.append(attribute)
                if base == "response" and attribute == "read":
                    counts["read"] += 1
    details: dict[str, object] = {
        "putheader_names": putheader_names,
        "connection_call": connection_call,
        "putrequest_call": putrequest_call,
        "ssl_attributes": ssl_attributes,
        "forbidden": forbidden,
        "constants": constants,
    }
    return counts, details


def _check_connection_call(
    call: ast.Call, constants: dict[str, ast.Constant]
) -> list[str]:
    problems: list[str] = []
    keywords = _keyword_map(call)
    host = _resolve_constant(keywords.get("host"), constants)
    port = _resolve_constant(keywords.get("port"), constants)
    timeout = _resolve_constant(keywords.get("timeout"), constants)
    context = keywords.get("context")
    if not (
        host is not None and host.value == "data.alpaca.markets"
    ):
        problems.append("R14 connection host argument")
    if not (port is not None and port.value == 443):
        problems.append("R14 connection port argument")
    if not (timeout is not None and timeout.value == 5.0):
        problems.append("R14 connection timeout argument")
    if not isinstance(context, ast.Name):
        problems.append("R14 connection context argument")
    return problems


def _check_putrequest_call(
    call: ast.Call, constants: dict[str, ast.Constant]
) -> list[str]:
    problems: list[str] = []
    if not call.args:
        return ["R14 putrequest arguments"]
    method = _resolve_constant(call.args[0], constants)
    if not (method is not None and method.value == "GET"):
        problems.append("R14 putrequest method argument")
    if len(call.args) < 2 or not isinstance(call.args[1], ast.Name):
        problems.append("R14 putrequest target argument")
    keywords = _keyword_map(call)
    skip_host = keywords.get("skip_host")
    skip_encoding = keywords.get("skip_accept_encoding")
    if not (
        isinstance(skip_host, ast.Constant) and skip_host.value is False
    ):
        problems.append("R14 skip_host argument")
    if not (
        isinstance(skip_encoding, ast.Constant)
        and skip_encoding.value is True
    ):
        problems.append("R14 skip_accept_encoding argument")
    return problems


def r14_stdlib_call_model() -> list[str]:
    problems: list[str] = []
    counts, details = _call_details()
    for key in (
        "context",
        "connection",
        "putrequest",
        "endheaders",
        "getresponse",
        "read",
        "close",
    ):
        if counts[key] != 1:
            problems.append(f"R14 {key} count {counts[key]}")
    if counts["putheader"] != 4:
        problems.append(f"R14 putheader count {counts['putheader']}")
    names = tuple(details["putheader_names"])
    if names != EXPECTED_PUTHEADER_NAMES:
        problems.append(f"R14 putheader names {names}")
    attributes = set(details["ssl_attributes"])
    if attributes != {"create_default_context"}:
        problems.append(f"R14 ssl attributes {sorted(attributes)}")
    for attribute in details["forbidden"]:
        problems.append(f"R14 forbidden call {attribute}")
    connection_call = details["connection_call"]
    constants = details["constants"]
    if isinstance(connection_call, ast.Call):
        problems.extend(
            _check_connection_call(connection_call, constants)
        )
    putrequest_call = details["putrequest_call"]
    if isinstance(putrequest_call, ast.Call):
        problems.extend(_check_putrequest_call(putrequest_call, constants))
    return problems


def _build_caller_identities() -> set[str]:
    return {
        "quantforge.marketdata_alpaca.build_alpaca_iex_source",
        "quantforge.marketdata_alpaca.composition.build_alpaca_iex_source",
    }


def _caller_paths(paths: list[Path]) -> list[Path]:
    callers: list[Path] = []
    identities = _build_caller_identities()
    for path in paths:
        tree = _parse(path)
        aliases = import_aliases(tree, path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if resolve(node.func, aliases) in identities or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "_capture_smoke_observation"
            ):
                callers.append(path)
    return callers


def r15_production_caller_allow_list() -> list[str]:
    problems: list[str] = []
    production = [
        path
        for path in python_files(QUANTFORGE_ROOT)
        if not path.is_relative_to(PACKAGE_ROOT)
    ]
    for path in _caller_paths(production):
        if path != SMOKE_PATH:
            problems.append(f"R15 production caller in {path}")
    tests = [
        path
        for path in python_files(TESTS_ROOT)
        if not path.is_relative_to(ALLOWED_TEST_ROOT)
    ]
    for path in _caller_paths(tests):
        problems.append(f"R15 test caller in {path}")
    return problems


def r16_production_importer_allow_list() -> list[str]:
    problems: list[str] = []
    for path in python_files(QUANTFORGE_ROOT):
        if path.is_relative_to(PACKAGE_ROOT):
            continue
        for module in resolved_import_modules(path, _parse(path)):
            if module == "quantforge.marketdata_alpaca":
                if path != SMOKE_PATH:
                    problems.append(f"R16 production importer {path}")
            elif module.startswith("quantforge.marketdata_alpaca."):
                problems.append(f"R16 production importer {path}")
    for path in python_files(TESTS_ROOT):
        if path.is_relative_to(ALLOWED_TEST_ROOT):
            continue
        for module in resolved_import_modules(path, _parse(path)):
            if module == "quantforge.marketdata_alpaca" or module.startswith(
                "quantforge.marketdata_alpaca."
            ):
                problems.append(f"R16 test importer {path}")
    return problems


def r17_synthetic_seam_construction() -> list[str]:
    problems: list[str] = []
    paths = list(python_files(QUANTFORGE_ROOT)) + list(
        python_files(TESTS_ROOT)
    )
    for path in paths:
        tree = _parse(path)
        aliases = import_aliases(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "_fetch"
                and isinstance(node.ctx, ast.Store)
                and path != SOURCE_PATH
            ):
                problems.append(f"R17 fetch assignment in {path}")
            if not isinstance(node, ast.Call):
                continue
            identity = resolve(node.func, aliases)
            if identity is None:
                continue
            tail = identity.split(".")[-1]
            allowed_test = path.is_relative_to(ALLOWED_TEST_ROOT)
            if tail == "AlpacaIexLatestTradeSource" and path != SOURCE_PATH:
                problems.append(f"R17 adapter construction in {path}")
            if (
                tail == "_create_source"
                and path != COMPOSITION_PATH
                and not allowed_test
            ):
                problems.append(f"R17 factory caller in {path}")
            if (
                tail == "_build_source"
                and path != COMPOSITION_PATH
                and not allowed_test
            ):
                problems.append(f"R17 build caller in {path}")
    return problems


RULES = (
    ("R1", r1_file_set),
    ("R2", r2_network_import_authority),
    ("R3", r3_capability_denylist),
    ("R4", r4_forbidden_calls),
    ("R5", r5_no_clock_access),
    ("R6", r6_credential_locality),
    ("R7", r7_price_locality),
    ("R8", r8_literal_and_verb_authority),
    ("R9", r9_single_attempt_authority),
    ("R10", r10_surface_and_state),
    ("R11", r11_durability),
    ("R12", r12_amendment_parity),
    ("R13", r13_import_allow_list),
    ("R14", r14_stdlib_call_model),
    ("R15", r15_production_caller_allow_list),
    ("R16", r16_production_importer_allow_list),
    ("R17", r17_synthetic_seam_construction),
)


def run_rules() -> dict[str, list[str]]:
    return {name: rule() for name, rule in RULES}
