"""Static R1-R17 contract parity and negative mutation oracles."""

from __future__ import annotations

import ast
import builtins
import importlib
import os
import socket
from datetime import datetime as RealDateTime
from pathlib import Path
from types import SimpleNamespace

import _marketdata_alpaca_rules as rules
import pytest

_REAL_PACKAGE_ROOT = rules.PACKAGE_ROOT
_RULE_NAMES = tuple(name for name, _rule in rules.RULES)


@pytest.fixture(scope="module")
def rule_results() -> dict[str, list[str]]:
    return rules.run_rules()


@pytest.mark.parametrize("name", _RULE_NAMES)
def test_r1_to_r17_are_green(
    name: str, rule_results: dict[str, list[str]]
) -> None:
    assert rule_results[name] == []


def test_oracle_ai_exception_is_exact() -> None:
    assert rules.ORACLE_AI_NETWORK_EXCEPTION_PATH == rules.TRANSPORT_PATH
    assert rules.ORACLE_AI_NETWORK_EXCEPTION_ROOTS == frozenset({"http", "ssl"})
    for path in rules.PACKAGE_ROOT.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path == rules.TRANSPORT_PATH:
            assert "import http.client" in source
            assert "import ssl" in source
        else:
            assert "import http" not in source
            assert "import ssl" not in source


def _materialize_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mutation_source: str | None = None,
    mutation_name: str | None = None,
    extra_files: tuple[tuple[str, str], ...] = (),
) -> None:
    src_root = tmp_path / "src"
    quantforge_root = src_root / "quantforge"
    package_root = quantforge_root / "marketdata_alpaca"
    tests_root = tmp_path / "tests"
    allowed_test_root = tests_root / "marketdata_alpaca"
    package_root.mkdir(parents=True)
    allowed_test_root.mkdir(parents=True)

    for name in rules.MODULE_NAMES:
        (package_root / name).write_text(
            (_REAL_PACKAGE_ROOT / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    if mutation_name is not None and mutation_source is not None:
        (package_root / mutation_name).write_text(
            mutation_source, encoding="utf-8"
        )
    for relative, source in extra_files:
        target = quantforge_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    values = {
        "SRC_ROOT": src_root,
        "QUANTFORGE_ROOT": quantforge_root,
        "PACKAGE_ROOT": package_root,
        "TESTS_ROOT": tests_root,
        "ALLOWED_TEST_ROOT": allowed_test_root,
        "TRANSPORT_PATH": package_root / "transport.py",
        "SOURCE_PATH": package_root / "source.py",
        "COMPOSITION_PATH": package_root / "composition.py",
        "TRIPWIRE_ALLOWANCE": Path("quantforge/marketdata_alpaca/source.py"),
    }
    for name, value in values.items():
        monkeypatch.setattr(rules, name, value)


def test_each_rule_rejects_a_meaningful_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mutations: tuple[tuple[str, str, str], ...] = (
        ("R1", "", "rogue.py"),
        ("R2", "import socket\n", "client.py"),
        ("R3", "import os\n", "client.py"),
        ("R4", "eval('x')\n", "client.py"),
        ("R5", "import datetime\ndatetime.datetime.now()\n", "client.py"),
        ("R6", "from quantforge.credentials import CredentialBoundary\n", "client.py"),
        ("R7", "import quantforge.marketdata\n", "client.py"),
        ("R8", "METHOD = 'POST'\n", "client.py"),
        ("R9", "while True:\n    pass\n", "client.py"),
        ("R10", "PUBLIC_MUTATION = 1\n", "client.py"),
        ("R11", "print('x')\n", "client.py"),
        ("R13", "import requests\n", "client.py"),
        ("R14", "connection.request('GET', '/')\n", "transport.py"),
    )
    for name, source, filename in mutations:
        _materialize_package(
            monkeypatch,
            tmp_path / name,
            mutation_source=source,
            mutation_name=filename,
        )
        result = dict(rules.RULES)[name]()
        assert result, name
        monkeypatch.undo()

    _materialize_package(
        monkeypatch,
        tmp_path / "R12",
    )

    def fake_load_module(name: str, _path: Path) -> SimpleNamespace:
        if "windows" in name:
            return SimpleNamespace(
                PRODUCTION_BUILD_CALLER_ALLOW_LIST=frozenset(),
                PRODUCTION_PACKAGE_IMPORTER_ALLOW_LIST=frozenset(
                    {rules.COMPOSITION_PATH}
                ),
            )
        if "credentials" in name:
            windows_root = Path("windows")
            modules = ("backend.py",)
            return SimpleNamespace(
                WINDOWS_PACKAGE_ROOT=windows_root,
                WINDOWS_MODULE_NAMES=modules,
                WINDOWS_CURRENT_SNAPSHOT_ALLOW_LIST=frozenset(
                    {windows_root / modules[0], rules.COMPOSITION_PATH}
                ),
            )
        return SimpleNamespace(
            MARKETDATA_ALPACA_IMPORTER_ALLOW_LIST=frozenset(
                {rules.TRIPWIRE_ALLOWANCE}
            )
        )

    monkeypatch.setattr(rules, "load_module", fake_load_module)
    assert rules.r12_amendment_parity()
    monkeypatch.undo()

    for name, source, relative in (
        (
            "R15",
            ("from quantforge.marketdata_alpaca import "
             "build_alpaca_iex_source\n"
             "build_alpaca_iex_source()\n"),
            "other.py",
        ),
        (
            "R16",
            "import quantforge.marketdata_alpaca\n",
            "other.py",
        ),
        (
            "R17",
            ("from quantforge.marketdata_alpaca.source import "
             "AlpacaIexLatestTradeSource\n"
             "AlpacaIexLatestTradeSource()\n"),
            "other.py",
        ),
    ):
        _materialize_package(
            monkeypatch,
            tmp_path / name,
            extra_files=((relative, source),),
        )
        assert dict(rules.RULES)[name]()
        monkeypatch.undo()


def test_negative_fixtures_are_source_only() -> None:
    source = "import socket\nopen('should-not-run')\n"
    tree = ast.parse(source)
    assert isinstance(tree, ast.Module)
    assert "should-not-run" in source


def test_import_tripwire_and_fully_synthetic_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Sentinel(RuntimeError):
        pass

    def trip(*_args: object, **_kwargs: object) -> None:
        raise Sentinel("forbidden ambient capability")

    # Preload upstream models so their unrelated first-import initialization
    # cannot be mistaken for a target-package environment read.
    for module_name in (
        "quantforge.credentials",
        "quantforge.credentials_windows",
        "quantforge.marketdata",
        "quantforge.provenance",
    ):
        importlib.import_module(module_name)

    monkeypatch.setattr(socket, "create_connection", trip)
    monkeypatch.setattr(socket, "getaddrinfo", trip)
    monkeypatch.setattr(os, "getenv", trip)
    monkeypatch.setattr(os.environ, "get", trip)
    monkeypatch.setattr(builtins, "open", trip)

    package = importlib.import_module("quantforge.marketdata_alpaca")
    assert package.__all__ == ("build_alpaca_iex_source",)

    import quantforge.marketdata_alpaca.client as client_module
    import quantforge.marketdata_alpaca.composition as composition_module
    import quantforge.marketdata_alpaca.source as source_module
    import quantforge.marketdata_alpaca.transport as transport_module

    class ClockSentinelDateTime(RealDateTime):
        @classmethod
        def now(cls, *_args: object, **_kwargs: object) -> RealDateTime:
            raise Sentinel("clock read")

        @classmethod
        def utcnow(cls) -> RealDateTime:
            raise Sentinel("clock read")

        @classmethod
        def today(cls) -> RealDateTime:
            raise Sentinel("clock read")

    monkeypatch.setattr(client_module, "datetime", ClockSentinelDateTime)
    monkeypatch.setattr(source_module, "datetime", ClockSentinelDateTime)
    monkeypatch.setattr(
        composition_module, "datetime", ClockSentinelDateTime
    )
    monkeypatch.setattr(transport_module.http.client, "HTTPSConnection", trip)
    monkeypatch.setattr(transport_module.ssl, "create_default_context", trip)

    from quantforge.marketdata import PriceObservationRequest
    from quantforge.marketdata_alpaca.source import _create_source
    from quantforge.marketdata_alpaca.transport import TransportResponse

    body = (
        b'{"trades":{"AAPL":{"t":"2026-09-28T14:03:21.123456789Z",'
        b'"i":1,"x":"V","p":178.26,"s":1,"c":[],"z":"C"}}}'
    )
    calls: list[str] = []

    def fetch(target: str) -> TransportResponse:
        calls.append(target)
        return TransportResponse(
            status=200,
            content_type="application/json",
            content_encoding=None,
            content_length=str(len(body)),
            transfer_encoding=None,
            body=body,
        )

    source = _create_source(fetch=fetch)
    observation = source.observe(
        PriceObservationRequest(
            schema="marketdata-request-v1", symbols=("AAPL",)
        )
    )
    assert len(calls) == 1
    assert observation.risk_prices[0].symbol == "AAPL"
