"""PRICE-001 static enforcement and tripwires (contract sections 22-26)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPO_ROOT / "src"
_PACKAGE_DIR = _SOURCE_ROOT / "quantforge" / "marketdata"
_MODULE_FILES = {
    "__init__.py",
    "canonical.py",
    "errors.py",
    "models.py",
    "producer.py",
    "source.py",
}
# MARKETDATA-002 consumer amendment (frozen section 32 clause (g)): exactly
# two reviewed consumer modules may import the public marketdata module;
# submodule imports from those files still fail closed.
MARKETDATA_ALPACA_IMPORTER_ALLOW_LIST = frozenset(
    {
        Path("quantforge/marketdata_alpaca/source.py"),
        Path("quantforge/marketdata_smoke.py"),
    }
)
_ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "hashlib",
    "json",
    "re",
    "typing",
    "pydantic",
}
_ALLOWED_QUANTFORGE_MODULES = {"quantforge.frontier", "quantforge.provenance"}
_FORBIDDEN_BARE_BUILTINS = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "input",
    "locals",
    "open",
    "setattr",
    "vars",
}
_FORBIDDEN_MODULE_ROOTS = {
    "aiohttp",
    "asyncio",
    "ctypes",
    "dotenv",
    "duckdb",
    "ftplib",
    "getpass",
    "http",
    "httpx",
    "importlib",
    "io",
    "keyring",
    "marshal",
    "multiprocessing",
    "os",
    "pathlib",
    "pickle",
    "pty",
    "random",
    "requests",
    "sched",
    "secrets",
    "selectors",
    "shelve",
    "shutil",
    "socket",
    "socketserver",
    "sqlite3",
    "ssl",
    "subprocess",
    "telnetlib",
    "tempfile",
    "time",
    "urllib",
    "urllib3",
    "uuid",
    "websocket",
    "websockets",
    "xmlrpc",
}
_FORBIDDEN_CALLEE_IDENTITIES = {
    "datetime.date.today",
    "datetime.datetime.fromtimestamp",
    "datetime.datetime.now",
    "datetime.datetime.today",
    "datetime.datetime.utcnow",
    "decimal.localcontext",
    "decimal.setcontext",
    "object.__delattr__",
    "object.__setattr__",
}
_PROTECTED = "PriceFreshnessEvidence"
_CANONICAL = ("quantforge.frontier.models", _PROTECTED)
_DEFINING_MODULE = "quantforge.frontier.models"


def _path_info(relpath: Path) -> tuple[str, str]:
    parts = list(relpath.with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    modname = ".".join(parts)
    pkgpath = modname if is_package else ".".join(parts[:-1])
    return modname, pkgpath


def _module_table(
    tree: ast.Module, modname: str, pkgpath: str
) -> tuple[dict[str, tuple[str, ...]], bool]:
    table: dict[str, tuple[str, ...]] = {}
    rebound = False
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                table[bound] = (
                    "module",
                    alias.name if alias.asname else alias.name.split(".")[0],
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level == 1:
                    base = pkgpath
                else:
                    base = ".".join(pkgpath.split(".")[: -(node.level - 1)])
                module = base + ("." + node.module if node.module else "")
            else:
                module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                table[alias.asname or alias.name] = (
                    "entity",
                    module,
                    alias.name,
                )
        elif (
            isinstance(node, ast.ClassDef)
            and modname == _DEFINING_MODULE
            and node.name == _PROTECTED
        ):
            table[node.name] = ("class", *_CANONICAL)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(sub, ast.Name) and sub.id == _PROTECTED
                for target in targets
                for sub in ast.walk(target)
            ):
                rebound = True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == _PROTECTED
        ):
            rebound = True
    return table, rebound


def _all_module_tables() -> dict[str, dict[str, tuple[str, ...]]]:
    tables: dict[str, dict[str, tuple[str, ...]]] = {}
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relpath = path.relative_to(_SOURCE_ROOT)
        modname, pkgpath = _path_info(relpath)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        table, _ = _module_table(tree, modname, pkgpath)
        tables[modname] = table
    return tables


_TABLES = _all_module_tables()


def _module_attribute(module: str, attr: str) -> tuple[str, ...]:
    candidate = module + "." + attr
    if candidate in _TABLES:
        return ("module", candidate)
    bound = _TABLES.get(module, {}).get(attr)
    return bound if bound is not None else ("module", candidate)


def _resolve(
    node: ast.expr, table: dict[str, tuple[str, ...]]
) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return table.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, table)
        if base is None:
            return None
        if base[0] == "module":
            return _module_attribute(base[1], node.attr)
        if base[0] == "entity":
            imported_module = base[1] + "." + base[2]
            if imported_module in _TABLES:
                return _module_attribute(imported_module, node.attr)
        return ("member",)
    return None


def _identity(
    resolved: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    if resolved is None:
        return None
    if resolved[0] == "class":
        return resolved
    if resolved[0] == "entity":
        module, name = resolved[1], resolved[2]
        if (module, name) == _CANONICAL:
            return ("class", *_CANONICAL)
        hop = _TABLES.get(module, {}).get(name)
        if hop is None:
            return None
        if hop[0] == "class" and tuple(hop[1:]) == _CANONICAL:
            return ("class", *_CANONICAL)
        if hop[0] == "entity" and (hop[1], hop[2]) == _CANONICAL:
            return ("class", *_CANONICAL)
        return None
    return None


def _protected_call(func: ast.expr, table: dict[str, tuple[str, ...]]) -> bool:
    if isinstance(func, ast.Name):
        bound = table.get(func.id)
        return func.id == _PROTECTED or (
            bound is not None and bound[0] == "entity" and bound[2] == _PROTECTED
        )
    if isinstance(func, ast.Attribute):
        return func.attr == _PROTECTED
    return False


def _enclosing(tree: ast.Module, lineno: int) -> list[str]:
    scopes: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end:
                scopes.append((node.lineno, node.name))
    return [name for _, name in sorted(scopes, reverse=True)]


def _site_verdict(relpath: Path, scopes: list[str], canonical: bool) -> str:
    if not canonical:
        return "UNRESOLVED"
    posix = relpath.as_posix()
    if posix == "quantforge/marketdata/producer.py" and (
        "produce_price_freshness_evidence" in scopes
    ):
        return "CATEGORY_1"
    if posix == "quantforge/frontier/models.py" and ("_parse_frontier_model" in scopes):
        return "CATEGORY_2"
    return "FORBIDDEN"


def _scan(source: str, relpath: Path) -> list[tuple[int, str]]:
    modname, pkgpath = _path_info(relpath)
    tree = ast.parse(source)
    table, rebound = _module_table(tree, modname, pkgpath)
    saved_table = _TABLES.get(modname)
    _TABLES[modname] = table
    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _protected_call(node.func, table):
            continue
        if rebound:
            verdict = "UNRESOLVED"
        else:
            canonical = _identity(_resolve(node.func, table)) is not None
            verdict = _site_verdict(relpath, _enclosing(tree, node.lineno), canonical)
        results.append((node.lineno, verdict))
    if saved_table is None:
        _TABLES.pop(modname, None)
    else:
        _TABLES[modname] = saved_table
    return results


def _package_sources() -> dict[str, tuple[Path, ast.Module]]:
    sources: dict[str, tuple[Path, ast.Module]] = {}
    for path in sorted(_PACKAGE_DIR.glob("*.py")):
        sources[path.name] = (
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
    return sources


def _package_relpath(name: str) -> Path:
    return Path("quantforge") / "marketdata" / name


def _imported_modules(tree: ast.Module, pkgpath: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.name if alias.asname else alias.name.split(".")[0]
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level == 1:
                    base = pkgpath
                else:
                    base = ".".join(pkgpath.split(".")[: -(node.level - 1)])
                modules.add(base + ("." + node.module if node.module else ""))
            elif node.module:
                modules.add(node.module)
    return modules


def test_f1_import_allow_list_is_exact() -> None:
    for name, (path, tree) in _package_sources().items():
        _, pkgpath = _path_info(_package_relpath(name))
        for module in _imported_modules(tree, pkgpath):
            root = module.split(".")[0]
            if root == "quantforge":
                assert (
                    module in _ALLOWED_QUANTFORGE_MODULES
                    or module == "quantforge.marketdata"
                    or module.startswith("quantforge.marketdata.")
                ), (path.name, module)
            else:
                assert root in _ALLOWED_IMPORT_ROOTS, (path.name, module)


def test_f2_no_forbidden_module_qualified_callee() -> None:
    for name, (_, tree) in _package_sources().items():
        modname, pkgpath = _path_info(_package_relpath(name))
        table, _ = _module_table(tree, modname, pkgpath)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            resolved = _resolve(node.func, table)
            if resolved is None:
                continue
            if resolved[0] == "module":
                dotted = resolved[1]
                assert dotted not in _FORBIDDEN_CALLEE_IDENTITIES, (name, dotted)
                assert dotted.split(".")[0] not in _FORBIDDEN_MODULE_ROOTS, (
                    name,
                    dotted,
                )


def test_f3_no_forbidden_bare_builtin_calls() -> None:
    for name, (_, tree) in _package_sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in _FORBIDDEN_BARE_BUILTINS, (
                    name,
                    node.func.id,
                )


def test_f4_module_file_set_is_exactly_six() -> None:
    present = {path.name for path in _PACKAGE_DIR.glob("*.py")}
    assert present == _MODULE_FILES


def _f5_hits(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    table, _ = _module_table(
        tree, "quantforge.marketdata.models", "quantforge.marketdata"
    )
    hits: list[tuple[int, str]] = []

    def root_of(resolved: tuple[str, ...] | None) -> str | None:
        if resolved is None:
            return None
        if resolved[0] == "module":
            return resolved[1].split(".")[0]
        if resolved[0] in ("class", "entity"):
            candidate = resolved[1]
            return candidate.split(".")[0]
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr in {"model_construct", "model_copy"}:
            resolved = _resolve(node.func.value, table)
            root = root_of(resolved)
            if resolved is None or root in {"quantforge", "pydantic"}:
                hits.append(
                    (node.lineno, "MB1" if attr == "model_construct" else "MB2")
                )
        elif (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "object"
            and attr in {"__setattr__", "__delattr__"}
        ):
            hits.append((node.lineno, "MB3"))
    return hits


def test_f5_package_contains_no_model_bypass_calls() -> None:
    for name, (path, _) in _package_sources().items():
        hits = _f5_hits(path.read_text(encoding="utf-8"))
        assert hits == [], (name, hits)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            (
                "from quantforge.frontier import PriceFreshnessEvidence\n"
                "PriceFreshnessEvidence.model_construct(schema='x')\n"
            ),
            {"MB1"},
        ),
        (
            (
                "from . import models\n"
                "models.VerifiedPriceObservation.model_construct(schema='x')\n"
            ),
            {"MB1"},
        ),
        (
            "def f(obj):\n    return obj.model_construct(schema='x')\n",
            {"MB1"},
        ),
        (
            "def f(obj):\n    return obj.model_copy(update={})\n",
            {"MB2"},
        ),
        (
            (
                "from quantforge.frontier import PriceFreshnessEvidence\n"
                "PriceFreshnessEvidence.model_copy(update={})\n"
            ),
            {"MB2"},
        ),
        (
            "def f(obj):\n    object.__setattr__(obj, 'x', 1)\n",
            {"MB3"},
        ),
        (
            "def f(obj):\n    object.__delattr__(obj, 'x')\n",
            {"MB3"},
        ),
        ("import decimal\ndecimal.Decimal.model_copy(update={})\n", set()),
        ("def f(obj):\n    return obj.model_dump(), obj.copy()\n", set()),
    ],
)
def test_f5_patterns_fire_and_benign_receivers_do_not(
    source: str, expected: set[str]
) -> None:
    assert {pattern for _, pattern in _f5_hits(source)} == expected


def test_sentinel_probe_never_fires() -> None:
    probe = Path(__file__).with_name("sentinel_probe.py")
    completed = subprocess.run(
        [sys.executable, str(probe)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert "SENTINELS_CLEAN" in completed.stdout


def test_pa_current_tree_green_with_exact_categories() -> None:
    sites: list[tuple[str, int, str]] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relpath = path.relative_to(_SOURCE_ROOT)
        source = path.read_text(encoding="utf-8")
        for lineno, verdict in _scan(source, relpath):
            sites.append((relpath.as_posix(), lineno, verdict))
    assert [verdict for _, _, verdict in sites] == ["CATEGORY_2", "CATEGORY_1"]
    assert sites[0][0] == "quantforge/frontier/models.py"
    assert sites[0][1] == 1104
    assert sites[1][0] == "quantforge/marketdata/producer.py"
    assert "FORBIDDEN" not in {verdict for _, _, verdict in sites}
    assert "UNRESOLVED" not in {verdict for _, _, verdict in sites}


_ADMITTED_CASES = [
    (
        "t1_canonical_direct",
        "quantforge/broker/t1.py",
        (
            "from quantforge.frontier.models import PriceFreshnessEvidence\n"
            "PriceFreshnessEvidence()\n"
        ),
        "FORBIDDEN",
    ),
    (
        "t2_direct_alias",
        "quantforge/broker/t2.py",
        (
            "from quantforge.frontier.models import PriceFreshnessEvidence as PFE\n"
            "PFE()\n"
        ),
        "FORBIDDEN",
    ),
    (
        "t3_public_attribute",
        "quantforge/broker/t3.py",
        ("import quantforge.frontier\nquantforge.frontier.PriceFreshnessEvidence()\n"),
        "FORBIDDEN",
    ),
    (
        "t4_public_alias",
        "quantforge/broker/t4.py",
        ("import quantforge.frontier as frontier\nfrontier.PriceFreshnessEvidence()\n"),
        "FORBIDDEN",
    ),
    (
        "t5_from_package",
        "quantforge/broker/t5.py",
        (
            "from quantforge.frontier import PriceFreshnessEvidence\n"
            "PriceFreshnessEvidence()\n"
        ),
        "FORBIDDEN",
    ),
    (
        "t6_from_package_alias",
        "quantforge/broker/t6.py",
        "from quantforge.frontier import PriceFreshnessEvidence as PFE\nPFE()\n",
        "FORBIDDEN",
    ),
    (
        "t7_review1_sibling_alias",
        "quantforge/broker/evil.py",
        (
            "from quantforge.frontier.evaluate import PriceFreshnessEvidence\n"
            "PriceFreshnessEvidence()\n"
        ),
        "FORBIDDEN",
    ),
    (
        "t8_category_1",
        "quantforge/marketdata/producer.py",
        (
            "from ..frontier import PriceFreshnessEvidence\n"
            "def produce_price_freshness_evidence(**kwargs):\n"
            "    return PriceFreshnessEvidence()\n"
        ),
        "CATEGORY_1",
    ),
    (
        "t9_category_2",
        "quantforge/frontier/models.py",
        (
            "from .models import PriceFreshnessEvidence\n"
            "def _parse_frontier_model():\n"
            "    return PriceFreshnessEvidence()\n"
        ),
        "CATEGORY_2",
    ),
    (
        "t10_outside_categories",
        "quantforge/broker/evil.py",
        (
            "from quantforge.frontier import PriceFreshnessEvidence\n"
            "def manufacture():\n"
            "    return PriceFreshnessEvidence()\n"
        ),
        "FORBIDDEN",
    ),
    (
        "t12_star_import",
        "quantforge/broker/t12.py",
        "from quantforge.frontier import *\nPriceFreshnessEvidence()\n",
        "UNRESOLVED",
    ),
    (
        "t12_local_rebinding",
        "quantforge/broker/t12b.py",
        (
            "from quantforge.frontier import PriceFreshnessEvidence\n"
            "PriceFreshnessEvidence = object\n"
            "PriceFreshnessEvidence()\n"
        ),
        "UNRESOLVED",
    ),
    (
        "t12_two_hop_chain",
        "quantforge/broker/t12c.py",
        (
            "from quantforge.marketdata.models import PriceFreshnessEvidence\n"
            "PriceFreshnessEvidence()\n"
        ),
        "UNRESOLVED",
    ),
]


@pytest.mark.parametrize(
    ("case", "relpath", "source", "expected"),
    _ADMITTED_CASES,
    ids=[case for case, _, _, _ in _ADMITTED_CASES],
)
def test_pa_admitted_and_fail_closed_cases(
    case: str, relpath: str, source: str, expected: str
) -> None:
    results = _scan(source, Path(relpath))
    assert len(results) == 1, (case, results)
    assert results[0][1] == expected, (case, results)


def test_pa_benign_non_protected_constructor_is_unaffected() -> None:
    source = "import decimal\ndecimal.Decimal('1.0')\n"
    assert _scan(source, Path("quantforge/broker/benign.py")) == []


def test_dependency_direction_tripwire() -> None:
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relpath = path.relative_to(_SOURCE_ROOT)
        if relpath.parts[:2] == ("quantforge", "marketdata"):
            continue
        allowed = relpath in MARKETDATA_ALPACA_IMPORTER_ALLOW_LIST
        _, pkgpath = _path_info(relpath)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module in _imported_modules(tree, pkgpath):
            if allowed and module == "quantforge.marketdata":
                continue
            assert not (
                module == "quantforge.marketdata"
                or module.startswith("quantforge.marketdata.")
            ), (relpath.as_posix(), module)


def test_locked_adapter_still_refuses_transport() -> None:
    from quantforge.execution_ledger.adapter import (
        BrokerAdapterNotImplementedError,
        LockedBrokerAdapter,
    )

    adapter = LockedBrokerAdapter()
    with pytest.raises(BrokerAdapterNotImplementedError):
        adapter.submit(None)  # type: ignore[arg-type]
    with pytest.raises(BrokerAdapterNotImplementedError):
        adapter.query_order("broker1-" + "0" * 32, None)  # type: ignore[arg-type]
    with pytest.raises(BrokerAdapterNotImplementedError):
        adapter.cancel_order("broker1-" + "0" * 32, None)  # type: ignore[arg-type]
