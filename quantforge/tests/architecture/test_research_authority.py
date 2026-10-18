from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "quantforge"
DENIED = {
    "openai",
    "anthropic",
    "litellm",
    "cohere",
    "mistralai",
    "qf_lab",
    "quantforge_lab",
    "google.generativeai",
}
OPERATIONAL = {
    "broker",
    "live",
    "operator",
    "credentials",
    "credentials_windows",
    "predispatch",
}


def imports(source: str) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def violations(source: str) -> set[str]:
    return {
        name
        for name in imports(source)
        if any(name == bad or name.startswith(bad + ".") for bad in DENIED)
    }


@pytest.mark.parametrize(
    "source",
    [
        "import openai",
        "from qf_lab.gateway import Gateway",
        "import google.generativeai as model",
    ],
)
def test_tripwire_detects_injected_model_dependency(source: str) -> None:
    assert violations(source)


def test_core_never_imports_models_or_lab() -> None:
    for path in PACKAGE.rglob("*.py"):
        assert not violations(path.read_text(encoding="utf-8")), path


def test_product_paths_have_no_live_or_credential_authority() -> None:
    for directory in ("product", "historical"):
        for path in (PACKAGE / directory).rglob("*.py"):
            for name in imports(path.read_text(encoding="utf-8")):
                assert not any(
                    name == f"quantforge.{bad}" or name.startswith(f"quantforge.{bad}.")
                    for bad in OPERATIONAL
                ), path


def test_provider_qualification_remains_offline_without_model_endpoint() -> None:
    provider = (PACKAGE / "historical" / "alphavantage.py").read_text(encoding="utf-8")
    assert "urllib.request" not in imports(provider)
    assert "www.alphavantage.co" in provider
    for path in PACKAGE.rglob("*.py"):
        if "autolab" not in path.parts:
            source = path.read_text(encoding="utf-8")
            assert (
                "api.deepseek.com" not in source and "api.openai.com" not in source
            ), path


def test_new_product_backtests_never_use_implicit_free_costs() -> None:
    for path in (PACKAGE / "product").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "BacktestConfig"
            ):
                assert node.keywords, path
                assert any(
                    keyword.arg == "fee_bps" or keyword.arg is None
                    for keyword in node.keywords
                ), path
