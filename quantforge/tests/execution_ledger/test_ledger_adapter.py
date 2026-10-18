from __future__ import annotations

import ast
import inspect
import os
import random
import socket
import subprocess
import urllib.request
import uuid
from pathlib import Path

import pytest
from _support import make_target

from quantforge.execution_ledger import (
    BrokerAdapterNotImplementedError,
    ExecutionLedgerError,
)
from quantforge.execution_ledger import adapter as adapter_module
from quantforge.execution_ledger.adapter import (
    BrokerAdapter,
    LockedBrokerAdapter,
)

_SECRET = "LEAK-SENTINEL-9cafa6d"

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
    "random",
    "datetime.now",
    "utcnow",
    "time.time",
    "uuid4",
    "secrets",
)


class _Hostile:
    """An argument that must never be touched, formatted, or logged."""

    def __repr__(self) -> str:
        raise AssertionError("argument repr was invoked")

    def __str__(self) -> str:
        raise AssertionError("argument str was invoked")


def _calls(adapter: BrokerAdapter) -> tuple[tuple[str, object], ...]:
    target = make_target(account_reference="acct-" + _SECRET)
    return (
        ("submit", lambda: adapter.submit(_Hostile())),  # type: ignore[arg-type]
        (
            "query_order",
            lambda: adapter.query_order(_Hostile(), target),  # type: ignore[arg-type]
        ),
        (
            "cancel_order",
            lambda: adapter.cancel_order(_Hostile(), target),  # type: ignore[arg-type]
        ),
    )


def test_protocol_surface_and_signatures() -> None:
    assert BrokerAdapter in LockedBrokerAdapter.__mro__
    assert {"submit", "query_order", "cancel_order"} <= set(
        BrokerAdapter.__dict__
    )
    assert list(inspect.signature(BrokerAdapter.submit).parameters) == [
        "self",
        "envelope",
    ]
    assert list(
        inspect.signature(BrokerAdapter.query_order).parameters
    ) == ["self", "submission_id", "target"]
    assert list(
        inspect.signature(BrokerAdapter.cancel_order).parameters
    ) == ["self", "submission_id", "target"]


def test_locked_adapter_is_zero_state() -> None:
    adapter = LockedBrokerAdapter()
    assert LockedBrokerAdapter.__slots__ == ()
    with pytest.raises(AttributeError):
        adapter.unexpected = 1  # type: ignore[attr-defined]


def test_locked_adapter_direct_invocations_fail_closed() -> None:
    adapter = LockedBrokerAdapter()
    for method, call in _calls(adapter):
        with pytest.raises(BrokerAdapterNotImplementedError) as excinfo:
            call()
        assert excinfo.value.method == method
        assert isinstance(excinfo.value, ExecutionLedgerError)


def test_protocol_default_bodies_fail_closed() -> None:
    class Incomplete(BrokerAdapter):
        pass

    adapter = Incomplete()
    for method, call in _calls(adapter):
        with pytest.raises(BrokerAdapterNotImplementedError) as excinfo:
            call()
        assert excinfo.value.method == method


def test_errors_do_not_leak_arguments() -> None:
    adapter = LockedBrokerAdapter()
    for method, call in _calls(adapter):
        with pytest.raises(BrokerAdapterNotImplementedError) as excinfo:
            call()
        error = excinfo.value
        assert method in str(error)
        assert _SECRET not in str(error)
        assert _SECRET not in repr(error)
        assert _SECRET not in repr(error.args)
        assert error.args == (
            f"broker adapter method is not implemented: {method}",
        )


def test_capability_tripwires(monkeypatch: pytest.MonkeyPatch) -> None:
    fired: list[str] = []

    def tripwire(*_args: object, **_kwargs: object) -> object:
        fired.append("fired")
        raise AssertionError("forbidden capability was invoked")

    monkeypatch.setattr(socket, "socket", tripwire)
    monkeypatch.setattr(socket, "create_connection", tripwire)
    monkeypatch.setattr(socket, "getaddrinfo", tripwire)
    monkeypatch.setattr(urllib.request, "urlopen", tripwire)
    monkeypatch.setattr(subprocess, "Popen", tripwire)
    monkeypatch.setattr(os, "getenv", tripwire)
    monkeypatch.setattr(random, "random", tripwire)
    monkeypatch.setattr(uuid, "uuid4", tripwire)

    adapter = LockedBrokerAdapter()
    for _method, call in _calls(adapter):
        with pytest.raises(BrokerAdapterNotImplementedError):
            call()
    assert fired == []


def test_forbidden_import_scan() -> None:
    source = Path(adapter_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
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
    assert roots <= {"__future__", "typing", "quantforge"}
    for token in _FORBIDDEN_TOKENS:
        assert token not in source, token
