"""SQLite lifetime and append-only screenshot journal; no model/network effects."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantforge.product.storage import local_root

from .storage import PlatformError, canonical, digest


class ChartRepository:
    def __init__(self, root: Path) -> None:
        self.root = local_root(root)
        self.path = self.root / "charts.sqlite"
        self._connect_lock = threading.Lock()
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings "
                "(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, "
                "request_id TEXT UNIQUE NOT NULL, signature TEXT NOT NULL, "
                "payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, "
                "payload TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL)"
            )
            for operation in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS chart_events_no_{operation.lower()} "
                    f"BEFORE {operation} ON events "
                    "BEGIN SELECT RAISE(ABORT,'append-only'); END"
                )

    def connect(self) -> sqlite3.Connection:
        local_root(self.root)
        if self.path.is_symlink():
            raise PlatformError("CHART_STORE_REDIRECTED")
        # Preserve the observed WAL first-statement race fence.
        with self._connect_lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def events(db: sqlite3.Connection) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        previous = "0" * 64
        for seq, raw, before, checksum in db.execute(
            "SELECT seq,payload,previous,hash FROM events ORDER BY seq"
        ):
            value = json.loads(raw)
            if (
                seq != len(values) + 1
                or before != previous
                or raw != canonical(value)
                or digest({"previous": before, "payload": value}) != checksum
            ):
                raise PlatformError("CHART_HISTORY_INTEGRITY_FAILED")
            values.append(value)
            previous = checksum
        return values

    @classmethod
    def append(cls, db: sqlite3.Connection, kind: str, **fields: Any) -> None:
        values = cls.events(db)
        row = db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = row[0] if row else "0" * 64
        value = {"kind": kind, "at": datetime.now(UTC).isoformat(), **fields}
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?)",
            (
                len(values) + 1,
                canonical(value),
                previous,
                digest({"previous": previous, "payload": value}),
            ),
        )
