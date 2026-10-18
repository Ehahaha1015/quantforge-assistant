from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from quantforge.experiments import canonical_json
from quantforge.product.storage import local_root


class ScaleError(ValueError):
    """Sanitized, supported research-scale precondition failure."""


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    local_root(path.parent)
    if path.is_symlink():
        raise ScaleError("REDIRECTED_ARTIFACT")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or raw != canonical_json(value).encode():
        raise ScaleError("NONCANONICAL_ARTIFACT")
    return value


def publish(path: Path, value: Any) -> None:
    """Atomic creation; a caller must hold the host writer lock."""
    local_root(path.parent)
    if path.exists() or path.is_symlink():
        raise ScaleError("ARTIFACT_ALREADY_EXISTS")
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_json(value).encode())
            stream.flush()
            os.fsync(stream.fileno())
        # Link refuses replacement; same-directory rename is fallback on Windows.
        os.link(temporary, path)
        os.unlink(temporary)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Journal:
    def __init__(self, root: Path, name: str = "registry.sqlite", *, read_only: bool = False) -> None:
        self.root = local_root(root)
        path = root / name
        if path.is_symlink():
            raise ScaleError("REDIRECTED_JOURNAL")
        if read_only:
            self.connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            return
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, previous TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL)"
        )
        for op in ("UPDATE", "DELETE"):
            self.connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS deny_{op.lower()} BEFORE {op} ON events BEGIN SELECT RAISE(ABORT,'append-only'); END"
            )
        self.connection.commit()

    def history(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        previous = "0" * 64
        for seq, before, raw, item_hash in self.connection.execute(
            "SELECT seq,previous,payload,hash FROM events ORDER BY seq"
        ):
            item = json.loads(raw)
            if (
                seq != len(result) + 1
                or before != previous
                or canonical_json(item) != raw
                or digest({"previous": previous, "payload": item}) != item_hash
            ):
                raise ScaleError("JOURNAL_INTEGRITY")
            if {"seq", "event_hash", "previous"} & item.keys():
                raise ScaleError("RESERVED_EVENT_METADATA")
            result.append({**item, "seq": seq, "event_hash": item_hash})
            previous = item_hash
        return result

    def append(self, kind: str, **values: Any) -> dict[str, Any]:
        return self.append_many([(kind, values)])[0]

    def append_many(self, items: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        if any({"seq", "event_hash", "previous", "kind"} & values.keys() for _, values in items):
            raise ScaleError("RESERVED_EVENT_METADATA")
        history = self.history()
        previous = history[-1]["event_hash"] if history else "0" * 64
        result: list[dict[str, Any]] = []
        with self.connection:
            for kind, values in items:
                item = {"kind": kind, **values}
                item_hash = digest({"previous": previous, "payload": item})
                self.connection.execute(
                    "INSERT INTO events(previous,payload,hash) VALUES(?,?,?)",
                    (previous, canonical_json(item), item_hash),
                )
                result.append({**item, "seq": len(history) + len(result) + 1, "event_hash": item_hash})
                previous = item_hash
        return result

    def close(self) -> None:
        self.connection.close()
