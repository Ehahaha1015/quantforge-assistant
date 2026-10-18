"""INV-2 Enforcement: Product package must never import LLM SDKs.

This test implements the AST tripwire required by ADR-001 Section "Enforcement".
It scans all Python files in src/quantforge/ and fails if any module imports
an LLM SDK or HTTP client that could be used against a model endpoint.

Enforcement mechanism: Static analysis via AST walking.
Failure mode: Fail-closed - any forbidden import causes test failure.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

# Product package root
PRODUCT_ROOT = Path(__file__).parent.parent.parent / "src" / "quantforge"

# Forbidden imports per ADR-001
FORBIDDEN_IMPORTS = {
    # LLM SDKs
    "openai",
    "anthropic",
    "google.generativeai",
    "litellm",
    "cohere",
    "mistralai",
    "together",
    "replicate",
    # HTTP clients that could reach model endpoints
    # Note: http.client is allowed (used in marketdata_alpaca with frozen endpoints)
    "requests",
    "httpx",
    "urllib.request",
    "urllib3",
    "aiohttp",
}


def find_python_files(root: Path) -> Iterator[Path]:
    """Find all Python files in product package."""
    return root.rglob("*.py")


def extract_imports(tree: ast.AST) -> list[str]:
    """Extract all import names from AST."""
    imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    return imports


def test_inv2_no_llm_sdk_imports():
    """INV-2: Product package must never import LLM SDKs.

    This is the primary enforcement test for ADR-001.

    Failure indicates:
    - An LLM SDK was imported in the product package
    - The AI/product boundary has been violated
    - INV-2 is broken

    If this test fails:
    1. Remove the forbidden import
    2. Move LLM-related code to qf-lab/ (external intelligence layer)
    3. Re-run this test to verify compliance
    """
    violations = []

    for py_file in find_python_files(PRODUCT_ROOT):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
            imports = extract_imports(tree)

            for import_name in imports:
                # Check if import matches any forbidden pattern
                for forbidden in FORBIDDEN_IMPORTS:
                    if import_name == forbidden or import_name.startswith(
                        f"{forbidden}."
                    ):
                        relative_path = py_file.relative_to(PRODUCT_ROOT)
                        violations.append(
                            f"{relative_path}: imports '{import_name}' "
                            f"(forbidden by INV-2)"
                        )

        except SyntaxError:
            # If file has syntax error, let other tests catch it
            continue

    assert not violations, (
        f"INV-2 violation detected!\n\n"
        f"The following files import LLM SDKs or HTTP clients in the product package:\n"
        f"{chr(10).join('  - ' + v for v in violations)}\n\n"
        f"Per ADR-001, the product package (src/quantforge/) must never import LLM SDKs.\n"
        f"Move LLM-related code to qf-lab/ (external intelligence layer)."
    )


def test_inv2_http_client_is_stdlib_only():
    """Verify http.client usage is limited to approved modules.

    http.client is allowed in marketdata_alpaca because it uses frozen
    endpoints and is tightly controlled. This test ensures it's not used
    elsewhere without review.
    """
    approved_http_client_users = {
        "marketdata_alpaca/transport.py",
        "marketdata_alpaca/client.py",
    }

    violations = []

    for py_file in find_python_files(PRODUCT_ROOT):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        imports = extract_imports(tree)

        if "http.client" in imports or "http" in imports:
            relative_path = str(py_file.relative_to(PRODUCT_ROOT)).replace("\\", "/")

            if relative_path not in approved_http_client_users:
                violations.append(
                    f"{relative_path}: imports http.client (requires explicit approval)"
                )

    assert not violations, (
        f"Unapproved http.client usage detected:\n"
        f"{chr(10).join('  - ' + v for v in violations)}\n\n"
        f"http.client is only approved for use in marketdata_alpaca.\n"
        f"For other HTTP needs, add explicit approval to this test."
    )


def test_inv2_smoke_test_violations():
    """Smoke test: Verify test correctly detects violations.

    This test ensures the enforcement mechanism actually works by testing
    against known-bad code samples.
    """
    # Test case 1: Direct import
    code_with_openai = "import openai\nprint('hello')"
    tree = ast.parse(code_with_openai)
    imports = extract_imports(tree)
    assert "openai" in imports, "Test mechanism failed to detect direct import"

    # Test case 2: From import
    code_with_from = "from anthropic import Anthropic"
    tree = ast.parse(code_with_from)
    imports = extract_imports(tree)
    assert "anthropic" in imports, "Test mechanism failed to detect from import"

    # Test case 3: Submodule import
    code_with_submodule = "import requests.auth"
    tree = ast.parse(code_with_submodule)
    imports = extract_imports(tree)
    assert "requests.auth" in imports, "Test mechanism failed to detect submodule"


@pytest.mark.parametrize(
    "forbidden_import",
    [
        "openai",
        "anthropic",
        "google.generativeai",
        "litellm",
        "cohere",
        "mistralai",
        "requests",
        "httpx",
        "urllib.request",
        "urllib3",
        "aiohttp",
    ],
)
def test_inv2_each_forbidden_import_is_blocked(forbidden_import: str):
    """Verify each forbidden import would be caught by the test.

    This parameterized test ensures every entry in FORBIDDEN_IMPORTS is
    actually checked by the enforcement logic.
    """
    test_code = f"import {forbidden_import}"
    tree = ast.parse(test_code)
    imports = extract_imports(tree)

    # Verify import is detected
    assert forbidden_import in imports or any(
        imp.startswith(f"{forbidden_import}.") for imp in imports
    ), f"Test would not catch import of {forbidden_import}"

    # Verify import is in forbidden set
    assert any(
        forbidden_import == forbidden or forbidden_import.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_IMPORTS
    ), f"{forbidden_import} not in FORBIDDEN_IMPORTS"


def test_inv2_product_root_exists():
    """Sanity check: Verify product root path is correct."""
    assert PRODUCT_ROOT.exists(), f"Product root not found: {PRODUCT_ROOT}"
    assert PRODUCT_ROOT.is_dir(), f"Product root is not a directory: {PRODUCT_ROOT}"
    assert (PRODUCT_ROOT / "__init__.py").exists(), (
        f"Product root missing __init__.py: {PRODUCT_ROOT}"
    )
