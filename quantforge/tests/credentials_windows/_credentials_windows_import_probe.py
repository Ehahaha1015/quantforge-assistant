"""Sentinel: importing credentials_windows reaches no forbidden capability."""

from __future__ import annotations

import ctypes
import sys

import pydantic  # noqa: F401

import quantforge.credentials
import quantforge.frontier  # noqa: F401

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


class _ForbiddenFinder:
    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> None:
        root = fullname.split(".")[0]
        if root in FORBIDDEN_ROOTS:
            raise ImportError(f"forbidden capability imported: {root}")


sys.meta_path.insert(0, _ForbiddenFinder())

_handle_calls: list[int] = []


def _forbidden_handle(*_args: object, **_kwargs: object) -> object:
    _handle_calls.append(1)
    raise AssertionError("no native handle may be created at import")


ctypes.WinDLL = _forbidden_handle  # type: ignore[assignment]

import quantforge.credentials_windows as package
from quantforge.credentials_windows import (
    backend,
    composition,
    native,
)

assert package.__all__ == ("build_credential_boundary",)
assert callable(package.build_credential_boundary)
assert backend.WindowsCredentialManagerBackend.__name__ == (
    "WindowsCredentialManagerBackend"
)
assert native.Advapi32CredentialReadSeam.__name__ == (
    "Advapi32CredentialReadSeam"
)
assert composition.build_credential_boundary.__name__ == (
    "build_credential_boundary"
)
assert _handle_calls == []

print("CREDENTIAL_WINDOWS_TRIPWIRE_OK")
