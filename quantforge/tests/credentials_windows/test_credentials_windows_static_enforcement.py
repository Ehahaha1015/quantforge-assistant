"""W1..W16 static enforcement, positive cases and negative mutations.

Every rule is exercised over the real package and over synthetic
source-text fixtures; no fixture is executed and no impersonation API is
ever called.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _credentials_windows_rules import (
    ALLOWED_TEST_ROOT,
    COMPOSITION_PATH,
    PACKAGE_RULES,
    QUANTFORGE_ROOT,
    package_asts,
    parse_source,
    src_asts,
    test_asts,
    w3_forbidden_builtins,
    w4_no_clock_reads,
    w5_backend_class_bindings,
    w6_native_loading,
    w7_credential_symbols,
    w8_module_level_values,
    w9_no_serialization_or_rendering,
    w10_protected_name_absence,
    w11_build_callers,
    w12_construction_sites,
    w13_package_importers,
    w14_public_surface,
    w15_native_call_sites,
    w16_handle_authority,
)

CONTRACT_PATH = QUANTFORGE_ROOT.parents[1] / "tasks" / "CREDENTIAL-002.md"
PRODUCTION_FIXTURE = QUANTFORGE_ROOT / "fixture_module.py"
TEST_FIXTURE = ALLOWED_TEST_ROOT / "fixture_module.py"


def _module(source: str, path: Path) -> dict[Path, ast.Module]:
    return {path: parse_source(source, path)}


@pytest.mark.parametrize(
    ("rule_name", "rule"),
    PACKAGE_RULES,
)
def test_package_rules_are_green(rule_name: str, rule: object) -> None:
    package = package_asts()
    if rule_name == "W1":  # W1 ignores the parsed mapping contents
        problems = rule(package)  # type: ignore[operator]
    else:
        problems = rule(package)  # type: ignore[operator]
    assert problems == [], (rule_name, problems)


def test_repository_wide_rules_are_green() -> None:
    src = src_asts()
    everything = {**src, **test_asts()}
    assert w10_protected_name_absence(src, package_asts()) == []
    assert w11_build_callers(everything) == []
    assert w12_construction_sites(everything) == []
    assert w13_package_importers(everything) == []
    assert w14_public_surface(package_asts()) == []
    assert w15_native_call_sites(package_asts()) == []
    assert w16_handle_authority(package_asts()) == []


def test_w11_rejects_every_unauthorized_caller_form() -> None:
    direct = (
        "from quantforge.credentials_windows import "
        "build_credential_boundary\n"
        "build_credential_boundary()\n"
    )
    aliased = (
        "from quantforge.credentials_windows import "
        "build_credential_boundary as b\n"
        "b()\n"
    )
    attribute = (
        "import quantforge.credentials_windows as cw\n"
        "cw.build_credential_boundary()\n"
    )
    for source in (direct, aliased, attribute):
        assert w11_build_callers(_module(source, PRODUCTION_FIXTURE))
        assert w11_build_callers(_module(source, TEST_FIXTURE)) == []


def test_w12_rejects_unauthorized_construction() -> None:
    backend = (
        "from quantforge.credentials_windows.backend import "
        "WindowsCredentialManagerBackend\n"
        "WindowsCredentialManagerBackend(seam=None)\n"
    )
    seam = (
        "from quantforge.credentials_windows.native import "
        "Advapi32CredentialReadSeam\n"
        "Advapi32CredentialReadSeam()\n"
    )
    for source in (backend, seam):
        assert w12_construction_sites(_module(source, PRODUCTION_FIXTURE))
        assert w12_construction_sites(_module(source, TEST_FIXTURE)) == []
        assert (
            w12_construction_sites(_module(source, COMPOSITION_PATH)) == []
        )


def test_w13_importers_and_prefix_collision() -> None:
    real = (
        "from quantforge.credentials_windows.backend import _Credential\n"
    )
    collision = "import quantforge.credentials_windows_extra\n"
    assert w13_package_importers(_module(real, PRODUCTION_FIXTURE))
    assert w13_package_importers(_module(collision, PRODUCTION_FIXTURE)) == []
    assert w13_package_importers(_module(real, TEST_FIXTURE)) == []
    assert w13_package_importers(_module(real, COMPOSITION_PATH)) == []


def test_w10_rejects_function_local_aliased_boundary_construction() -> None:
    source = (
        "def f():\n"
        "    from quantforge.credentials import CredentialBoundary as CB\n"
        "    return CB(backend=None)\n"
    )
    problems = w10_protected_name_absence(
        _module(source, PRODUCTION_FIXTURE), package_asts()
    )
    assert problems
    allowed_source = (
        "from quantforge.credentials import CredentialBoundary\n"
        "\n"
        "def build_credential_boundary():\n"
        "    return CredentialBoundary(backend=None)\n"
    )
    allowed = w10_protected_name_absence(
        {COMPOSITION_PATH: parse_source(allowed_source)},
        package_asts(),
    )
    assert allowed == []


def test_w16_rejects_principal_and_handle_fixtures() -> None:
    header = (
        "class Seam:\n"
        "    def __init__(self):\n"
        "        self._lib = ctypes.WinDLL(\n"
        "            'advapi32', use_last_error=True\n"
        "        )\n"
        "        self._lib.CredReadW()\n"
        "        self._lib.CredFree()\n"
    )
    named_fixtures = (
        "        self._lib.LogonUserW()\n",
        "        self._lib.ImpersonateLoggedOnUser()\n",
        "        self._lib.OpenProcessToken()\n",
        "        self._lib.SetThreadToken()\n",
    )
    dynamic_fixtures = (
        '        getattr(self._lib, "LogonUserW")()\n',
        '        self._lib["LogonUserW"]\n',
    )
    for source in named_fixtures + dynamic_fixtures:
        assert w16_handle_authority(
            {"native.py": parse_source(header + source)}
        ), source
    for source in named_fixtures:
        assert w7_credential_symbols(
            {"native.py": parse_source(header + source)}
        ), source
    second_handle = (
        "lib = ctypes.WinDLL(\"advapi32\", use_last_error=True)\n"
        "other = ctypes.WinDLL(\"advapi32\", use_last_error=True)\n"
        "other.LogonUserW()\n"
    )
    tree = {"native.py": parse_source(second_handle)}
    assert w6_native_loading(tree)
    assert w16_handle_authority(tree)


def test_w5_rejects_class_laundering_forms() -> None:
    nested = (
        "class Leaky:\n"
        "    if True:\n"
        "        provider_id = 'x'\n"
    )
    instance = (
        "class Leaky:\n"
        "    def __init__(self):\n"
        "        self.provider_id = 'x'\n"
    )
    subclass = "class Leaky(WindowsCredentialManagerBackend):\n    pass\n"
    nested_class = (
        "class Outer:\n"
        "    class Inner:\n"
        "        acquire = None\n"
    )
    for source in (nested, instance, subclass, nested_class):
        assert w5_backend_class_bindings(
            {"backend.py": parse_source(source)}
        ), source
    approved = (
        "class WindowsCredentialManagerBackend:\n"
        "    @property\n"
        "    def provider_id(self):\n"
        "        return 'x'\n"
        "    def acquire(self):\n"
        "        return None\n"
    )
    assert (
        w5_backend_class_bindings(
            {"backend.py": parse_source(approved)}
        )
        == []
    )


def test_w7_does_not_over_match_frozen_boundary_names() -> None:
    allowed = (
        "from quantforge.credentials import CredentialFacts\n"
        "from quantforge.credentials import CredentialScope\n"
        "handle.CredReadW()\n"
        "handle.CredFree()\n"
    )
    assert w7_credential_symbols({"native.py": parse_source(allowed)}) == []
    banned = "handle.CredWriteW()\n"
    assert w7_credential_symbols({"native.py": parse_source(banned)})


def test_w3_w4_w6_w8_w9_negative_fixtures() -> None:
    assert w3_forbidden_builtins(
        {"backend.py": parse_source("getattr(object(), 'x')\n")}
    )
    assert w3_forbidden_builtins(
        {"backend.py": parse_source("type('X', (), {})\n")}
    )
    assert (
        w3_forbidden_builtins(
            {"backend.py": parse_source("type(value)\n")}
        )
        == []
    )
    assert w4_no_clock_reads(
        {"backend.py": parse_source("datetime.now()\n")}
    )
    assert w6_native_loading(
        {"native.py": parse_source('x = ctypes.windll.advapi32\n')}
    )
    assert w6_native_loading(
        {
            "native.py": parse_source(
                "ctypes.WinDLL('advapi32', use_last_error=True)\n"
                "ctypes.WinDLL('advapi32', use_last_error=True)\n"
            )
        }
    )
    assert w8_module_level_values(
        {"backend.py": parse_source("_X = ctypes.c_int\n")}
    )
    assert (
        w8_module_level_values(
            {"backend.py": parse_source("_X = (1, 2, 3)\n")}
        )
        == []
    )
    assert w9_no_serialization_or_rendering(
        {"backend.py": parse_source("print('x')\n")}
    )
    leaked_repr = (
        "class X:\n"
        "    def __repr__(self):\n"
        "        return f'{self.value}'\n"
    )
    assert w9_no_serialization_or_rendering(
        {"backend.py": parse_source(leaked_repr)}
    )
    redacted = (
        "class X:\n"
        "    def __repr__(self):\n"
        "        return _TEXT\n"
    )
    assert (
        w9_no_serialization_or_rendering(
            {"backend.py": parse_source(redacted)}
        )
        == []
    )


def test_w14_and_w16_positive_cases_are_accepted() -> None:
    handle = (
        "lib = ctypes.WinDLL('advapi32', use_last_error=True)\n"
        "read = lib.CredReadW\n"
        "read.restype = ctypes.c_int\n"
        "read.argtypes = []\n"
        "free = lib.CredFree\n"
        "free.restype = None\n"
        "free.argtypes = [ctypes.c_void_p]\n"
    )
    tree = {"native.py": parse_source(handle)}
    assert w16_handle_authority(tree) == []
    assert w6_native_loading(tree) == []


def test_claim_equals_proof_matches_the_frozen_contract() -> None:
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    for index in range(1, 17):
        assert f"W{index} " in text or f"W{index}\n" in text, index
    for phrase in (
        "string-built or dynamically resolved names",
        "getattr/__getattr__/__getattribute__ dynamism",
        "containers, mappings, closures and default-argument objects",
        "runtime rebinding or monkeypatching of imported names",
        "comments, docstrings and string literals (never inspected)",
        "dataflow, provenance and taint analysis (never performed)",
    ):
        assert phrase in text
    for index in (11, 12, 13, 14):
        assert f"W{index} " in text
