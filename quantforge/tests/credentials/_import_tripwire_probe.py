"""Sentinel: the credentials package must not reach a forbidden capability."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pydantic  # noqa: F401

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
        "ctypes",
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

from quantforge.credentials import (
    CredentialBoundary,
    CredentialPurpose,
    CredentialReference,
    CredentialScope,
    CredentialUnavailableError,
)

_scope = CredentialScope(
    schema="credential-scope-v1",
    provider="rejecting-credential-backend-v1",
    backend="broker-main",
    environment="PAPER",
    account_reference="acct-001",
    consumer="broker-adapter",
    purpose=CredentialPurpose.ORDER_SUBMIT,
)
_reference = CredentialReference(value="deploy-ref-001")

try:
    CredentialBoundary().acquire(
        _scope, _reference, requested_at=datetime(2030, 1, 1, tzinfo=UTC)
    )
except CredentialUnavailableError:
    pass
else:  # pragma: no cover - the rejecting default must never issue a lease
    raise SystemExit("the rejecting default returned a lease")

print("CREDENTIAL_TRIPWIRE_OK")
