from __future__ import annotations

import ast
import builtins
import inspect
import io
import sys
from pathlib import Path
from typing import Any, NoReturn

import pytest

import quantforge.research as research_api
from quantforge.data import ParquetDataCatalog
from quantforge.experiments import ExperimentRecord, ExperimentStore
from quantforge.research import (
    ExperimentSpec,
    ResearchAdmissionError,
    ResearchRequest,
    admit_research_request,
)

from ._research_fixtures import complete_inputs, request_fields


def test_public_api_has_only_recipe_admission_and_no_results_or_oos_authority() -> None:
    assert set(research_api.__all__) == {
        "ResearchRequest",
        "ExperimentSpec",
        "ResearchAdmissionError",
        "admit_research_request",
    }
    assert len(research_api.__all__) == 4
    assert set(ResearchRequest.model_fields) == set(request_fields())
    assert set(ExperimentSpec.model_fields) == {
        "schema_version",
        "request",
        "recipe",
        "config_hash",
    }
    parameters = inspect.signature(admit_research_request).parameters
    assert tuple(parameters) == (
        "request",
        "proposal",
        "dataset_snapshot",
        "data_conventions",
        "code_context",
        "split_timestamps",
    )
    assert parameters["request"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for name, parameter in parameters.items():
        if name != "request":
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
    spec = admit_research_request(**complete_inputs())
    assert spec.recipe["oos_boundary"] == "UNSEALED_NO_TEST_PERMISSION"
    for name in (
        "metrics",
        "results",
        "oos_result",
        "test_permission",
        "seal",
        "seal_id",
        "run",
        "execute",
        "evaluate",
        "save",
        "persist",
        "reproduce",
        "report",
    ):
        assert not hasattr(spec, name)
        assert not hasattr(spec.request, name)
    assert "experiment_id" not in spec.to_config()


def test_research_source_has_no_forbidden_imports_or_operational_calls() -> None:
    root = Path(__file__).parents[2] / "src" / "quantforge" / "research"
    # 001B adds explicit retained-data/file authority in its separate submodule.
    # This original no-I/O oracle continues to cover every unchanged 001A module.
    sources = [root / name for name in ("__init__.py", "admission.py", "models.py")]
    assert sources, "Task A must have inspectable research source"
    forbidden_modules = {
        "asyncio",
        "http",
        "httpx",
        "requests",
        "aiohttp",
        "urllib",
        "websocket",
        "socket",
        "ssl",
        "subprocess",
        "multiprocessing",
        "random",
        "secrets",
        "uuid",
        "time",
        "os",
        "pathlib",
        "sqlite3",
        "duckdb",
        "qlib",
        "nautilus_trader",
        "alpaca",
        "account",
        "accounts",
        "broker",
        "credentials",
        "credentials_windows",
        "transport",
        "network",
        "operator",
        "live",
        "execution",
        "execution_ledger",
        "execstate",
        "portfolio",
        "risk",
        "predispatch",
        "frontier",
        "provenance",
        "marketdata",
        "integrations",
        "autolab",
    }
    forbidden_calls = {
        "open",
        "read",
        "write",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "mkdir",
        "unlink",
        "rename",
        "touch",
        "exists",
        "stat",
        "resolve",
        "connect",
        "execute",
        "executemany",
        "save",
        "persist",
        "dispatch",
        "now",
        "utcnow",
        "today",
        "time",
        "monotonic",
        "perf_counter",
        "uuid4",
        "getenv",
        "urandom",
        "eval",
        "exec",
        "compile",
        "__import__",
        "import_module",
        "ParquetDataCatalog",
        "ExperimentStore",
        "ExperimentRecord",
        "logical_content_hash",
        "canonicalize_bars",
        "prepare_bars",
        "validate_bars",
        "read_parquet",
        "scan_parquet",
        "read_csv",
        "scan_csv",
        "run_vectorized_backtest",
        "generate_moving_average_signals",
        "run_holdout_validation",
        "run_walk_forward_validation",
        "apply_fill",
        "place_order",
        "submit_order",
        "cancel_order",
        "execute_paper_order",
    }
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert set(alias.name.split(".")).isdisjoint(forbidden_modules), (
                        path,
                        alias.name,
                    )
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert set(module.split(".")).isdisjoint(forbidden_modules), (
                    path,
                    module,
                )
                for alias in node.names:
                    assert alias.name not in forbidden_calls or (
                        module == "re" and alias.name == "compile"
                    ), (path, alias.name)
                    aliases[alias.asname or alias.name] = module + "." + alias.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                target = aliases.get(node.func.id, node.func.id)
                name = target.split(".")[-1]
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
                target = (
                    aliases.get(node.func.value.id, node.func.value.id) + "." + name
                    if isinstance(node.func.value, ast.Name)
                    else name
                )
            else:
                continue
            if target == "re.compile":
                continue  # Compiling an identifier regex is a pure validation operation.
            assert name not in forbidden_calls, (path, node.lineno, name)


def test_success_failure_and_replay_perform_no_io_execution_clock_or_entropy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = complete_inputs()
    invalid = complete_inputs()
    invalid["proposal"]["backtest"]["fee_bps"] = 0
    expected = admit_research_request(**complete_inputs()).to_json()

    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("research admission attempted an operational call")

    error: ResearchAdmissionError | None = None
    with monkeypatch.context() as isolated:
        isolated.setattr(builtins, "open", forbidden)
        isolated.setattr(io, "open", forbidden)
        isolated.setattr(Path, "open", forbidden)
        isolated.setattr(ParquetDataCatalog, "__init__", forbidden)
        for name in ("read", "read_snapshot", "verify", "write", "query"):
            isolated.setattr(ParquetDataCatalog, name, forbidden)
        isolated.setattr(ExperimentStore, "__init__", forbidden)
        isolated.setattr(ExperimentStore, "save", forbidden)
        isolated.setattr(ExperimentRecord, "create", forbidden)
        operations = {
            "duckdb": ("connect",),
            "sqlite3": ("connect",),
            "polars": ("read_parquet", "scan_parquet", "read_csv", "scan_csv"),
            "time": ("time", "monotonic", "perf_counter"),
            "uuid": ("uuid4",),
            "random": ("random", "getrandbits"),
            "os": ("getenv", "urandom"),
        }
        for module_name, names in operations.items():
            module = sys.modules.get(module_name)
            if module is not None:
                for name in names:
                    isolated.setattr(module, name, forbidden)
        calculation_names = (
            "logical_content_hash",
            "canonicalize_bars",
            "prepare_bars",
            "validate_bars",
            "run_vectorized_backtest",
            "generate_moving_average_signals",
            "run_holdout_validation",
            "run_walk_forward_validation",
        )
        for module_name, module in tuple(sys.modules.items()):
            if module is None or not module_name.startswith(
                (
                    "quantforge.data",
                    "quantforge.backtest",
                    "quantforge.strategies",
                    "quantforge.validation",
                    "quantforge.research",
                )
            ):
                continue
            for name in calculation_names:
                if hasattr(module, name):
                    isolated.setattr(module, name, forbidden)
        admitted = admit_research_request(**valid).to_json()
        replayed = ExperimentSpec.from_json(expected).to_json()
        try:
            admit_research_request(**invalid)
        except ResearchAdmissionError as caught:
            error = caught
    assert admitted == replayed == expected
    assert error is not None
