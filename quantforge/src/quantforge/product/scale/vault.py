from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from quantforge.experiments import canonical_json
from quantforge.product.storage import local_root

from .registry import identifier
from .storage import ScaleError, digest, publish, read_json


def private_key_path(program: str) -> Path:
    """Host credential location, never supplied to Lab/context/evidence."""
    return (
        Path.home() / ".quantforge" / "research_vault" / (identifier(program) + ".key")
    )


def seal(
    root: Path,
    program: str,
    payload: dict[str, Any],
    binding: dict[str, Any],
    *,
    key_path: Path | None = None,
) -> dict[str, Any]:
    key_path = key_path or private_key_path(program)
    if ".autolab" in {part.casefold() for part in key_path.parts} or key_path.resolve().is_relative_to(
        root.resolve()
    ):
        raise ScaleError("KEY_MUST_BE_OUTSIDE_ARTIFACTS")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    local_root(key_path.parent)
    key = secrets.token_bytes(32)
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    nonce = secrets.token_bytes(12)
    header = {
        "schema_version": "scale-benchmark-vault-v1",
        "program_id": program,
        "binding": binding,
        "plaintext_sha256": digest(payload),
    }
    ciphertext = AESGCM(key).encrypt(
        nonce, canonical_json(payload).encode(), canonical_json(header).encode()
    )
    manifest = {
        **header,
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
    }
    publish(root / "benchmark.sealed.json", manifest)
    return {
        name: manifest[name]
        for name in (
            "schema_version",
            "program_id",
            "binding",
            "plaintext_sha256",
            "ciphertext_sha256",
        )
    }


def inspect_seal(root: Path) -> dict[str, Any]:
    manifest = read_json(root / "benchmark.sealed.json")
    try:
        cipher = base64.b64decode(manifest["ciphertext"], validate=True)
    except (ValueError, KeyError):
        raise ScaleError("INVALID_SEALED_PAYLOAD") from None
    if hashlib.sha256(cipher).hexdigest() != manifest["ciphertext_sha256"]:
        raise ScaleError("SEALED_CONTENT_MISMATCH")
    return {
        name: manifest[name]
        for name in (
            "schema_version",
            "program_id",
            "binding",
            "plaintext_sha256",
            "ciphertext_sha256",
        )
    }


class HostBenchmarkCapability:
    """Host-only decrypt capability. Arbitrary same-user native code is out of scope."""

    def __init__(self, program: str, key_path: Path | None = None) -> None:
        self.program = identifier(program)
        path = key_path or private_key_path(program)
        local_root(path.parent)
        if path.is_symlink() or ".autolab" in {part.casefold() for part in path.parts}:
            raise ScaleError("INVALID_HOST_KEY_PATH")
        self._key = path.read_bytes()
        if len(self._key) != 32:
            raise ScaleError("INVALID_HOST_KEY")

    def decrypt(self, root: Path, *, expected: dict[str, Any]) -> dict[str, Any]:
        import json

        manifest = read_json(root / "benchmark.sealed.json")
        if inspect_seal(root) != expected or manifest["program_id"] != self.program:
            raise ScaleError("BENCHMARK_BINDING_MISMATCH")
        header = {
            k: manifest[k]
            for k in ("schema_version", "program_id", "binding", "plaintext_sha256")
        }
        try:
            raw = AESGCM(self._key).decrypt(
                base64.b64decode(manifest["nonce"], validate=True),
                base64.b64decode(manifest["ciphertext"], validate=True),
                canonical_json(header).encode(),
            )
            payload = json.loads(raw)
        except (InvalidTag, ValueError, KeyError):
            raise ScaleError("BENCHMARK_AUTHENTICATION_FAILED") from None
        if (
            not isinstance(payload, dict)
            or canonical_json(payload).encode() != raw
            or digest(payload) != manifest["plaintext_sha256"]
        ):
            raise ScaleError("BENCHMARK_PLAINTEXT_MISMATCH")
        return payload
