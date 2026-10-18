"""Hash-verified SQLite event journal with transactional idempotency."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantforge.product.storage import local_root


class PlatformError(ValueError):
    """A bounded, non-secret platform refusal."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class _VerifiedHead:
    seq: int
    item_hash: str
    marker: tuple[int, int, int]


class _Connection(sqlite3.Connection):
    _managed: bool = False
    _verified: _VerifiedHead | None = None


def _marker(db: sqlite3.Connection) -> tuple[int, int, int]:
    return (
        db.total_changes,
        db.execute("PRAGMA data_version").fetchone()[0],
        db.execute("PRAGMA schema_version").fetchone()[0],
    )


class Ledger:
    def __init__(self, root: Path) -> None:
        self.root = local_root(root)
        self.path = self.root / "platform.sqlite"
        if self.path.is_symlink():
            raise PlatformError("REDIRECTED_LEDGER")
        # SQLite creates a WAL database's -wal/-shm files on first use and
        # deletes them when the last connection closes, so threads opening at
        # the same instant race to create them and all but one fail with
        # SQLITE_READONLY. Serialise connection creation. See
        # docs/WAL-CONNECTION-LIFETIME-20261016.md.
        self._connect_lock = threading.Lock()
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS events "
                "(seq INTEGER PRIMARY KEY, previous TEXT, payload TEXT, hash TEXT)"
            )
            for op in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS deny_{op.lower()} BEFORE {op} "
                    "ON events BEGIN SELECT RAISE(ABORT,'append-only'); END"
                )

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        local_root(self.root)
        if self.path.is_symlink():
            raise PlatformError("REDIRECTED_LEDGER")
        # sqlite3.connect() is lazy: the shared-memory files are created by the
        # first statement, so the pragma below stays inside the lock.
        with self._connect_lock:
            db = sqlite3.connect(
                self.path.as_uri() + "?mode=ro" if readonly else str(self.path),
                uri=readonly,
                timeout=10,
                factory=_Connection,
            )
            if not readonly:
                db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            assert isinstance(db, _Connection)
            db._managed = True
            db._verified = None
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            if isinstance(db, _Connection):
                db._managed = False
                db._verified = None
            db.close()

    @staticmethod
    def _verified_rows(db: sqlite3.Connection) -> Iterator[dict[str, Any]]:
        previous = "0" * 64
        count = 0
        for seq, before, raw, item_hash in db.execute(
            "SELECT seq,previous,payload,hash FROM events ORDER BY seq"
        ):
            item = json.loads(raw)
            if (
                seq != count + 1
                or before != previous
                or canonical(item) != raw
                or digest({"previous": previous, "payload": item}) != item_hash
            ):
                raise PlatformError("LEDGER_INTEGRITY_FAILED")
            count += 1
            previous = item_hash
            yield {**item, "seq": seq, "event_hash": item_hash}
        if isinstance(db, _Connection) and db._managed and db.in_transaction:
            db._verified = _VerifiedHead(count, previous, _marker(db))

    @classmethod
    def history(cls, db: sqlite3.Connection) -> list[dict[str, Any]]:
        return list(cls._verified_rows(db))

    @classmethod
    def projection(cls, db: sqlite3.Connection, *, limit: int = 100) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise PlatformError("LEDGER_PROJECTION_LIMIT_INVALID")
        tail: deque[dict[str, Any]] = deque(maxlen=limit)
        total = 0
        for row in cls._verified_rows(db):
            total += 1
            tail.append(row)
        return {
            "verification_scope": "FULL_CHAIN_AT_READ_SNAPSHOT",
            "events_total": total,
            "tail": list(tail),
            "head_hash": tail[-1]["event_hash"] if tail else "0" * 64,
        }

    @classmethod
    def _append_head(cls, db: sqlite3.Connection) -> tuple[int, str]:
        cached = db._verified if isinstance(db, _Connection) and db._managed else None
        if cached is not None and db.in_transaction and cached.marker == _marker(db):
            current = db.execute(
                "SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if (current or (0, "0" * 64)) == (cached.seq, cached.item_hash):
                return cached.seq, cached.item_hash
        last = None
        for row in cls._verified_rows(db):
            last = row
        return (last["seq"], last["event_hash"]) if last else (0, "0" * 64)

    @classmethod
    def append(cls, db: sqlite3.Connection, **payload: Any) -> dict[str, Any]:
        if {"seq", "event_hash", "previous"} & payload.keys():
            raise PlatformError("RESERVED_METADATA")
        seq, before = cls._append_head(db)
        value = {"at": datetime.now(UTC).isoformat(), **payload}
        item_hash = digest({"previous": before, "payload": value})
        changes = db.total_changes
        cursor = db.execute(
            "INSERT INTO events(previous,payload,hash) VALUES(?,?,?)",
            (before, canonical(value), item_hash),
        )
        if cursor.lastrowid != seq + 1 or db.total_changes != changes + 1:
            raise PlatformError("LEDGER_APPEND_UNEXPECTED_EFFECT")
        if isinstance(db, _Connection) and db._managed and db.in_transaction:
            db._verified = _VerifiedHead(seq + 1, item_hash, _marker(db))
        return {**value, "seq": seq + 1, "event_hash": item_hash}

    def read_projection(self, *, limit: int = 100) -> dict[str, Any]:
        with closing(self.connect(readonly=True)) as db:
            db.execute("BEGIN")
            return self.projection(db, limit=limit)

    def read(self) -> list[dict[str, Any]]:
        db = self.connect(readonly=True)
        try:
            return self.history(db)
        finally:
            db.close()

    def page(
        self, *, after: int = 0, limit: int = 100, anchor: str | None = None
    ) -> dict[str, Any]:
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 500
            or (
                after > 0
                and (
                    not isinstance(anchor, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", anchor)
                )
            )
            or (after == 0 and anchor not in {None, "0" * 64})
        ):
            raise PlatformError("LEDGER_PAGE_CURSOR_INVALID")
        with closing(self.connect(readonly=True)) as db:
            db.execute("BEGIN")
            head = db.execute(
                "SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            maximum = head[0] if head else 0
            if after > maximum:
                raise PlatformError("LEDGER_PAGE_CURSOR_AHEAD")
            previous = "0" * 64
            if after:
                prior = db.execute(
                    "SELECT previous,payload,hash FROM events WHERE seq=?", (after,)
                ).fetchone()
                if not prior:
                    raise PlatformError("LEDGER_PAGE_ANCHOR_MISSING")
                payload = json.loads(prior[1])
                if (
                    prior[2] != anchor
                    or canonical(payload) != prior[1]
                    or digest({"previous": prior[0], "payload": payload}) != prior[2]
                ):
                    raise PlatformError("LEDGER_PAGE_ANCHOR_CONFLICT")
                previous = prior[2]
            initial_anchor = previous
            events: list[dict[str, Any]] = []
            for seq, before, raw, item_hash in db.execute(
                "SELECT seq,previous,payload,hash FROM events WHERE seq>? "
                "ORDER BY seq LIMIT ?",
                (after, limit),
            ):
                payload = json.loads(raw)
                if (
                    seq != after + len(events) + 1
                    or before != previous
                    or canonical(payload) != raw
                    or digest({"previous": previous, "payload": payload}) != item_hash
                ):
                    raise PlatformError("LEDGER_PAGE_INTEGRITY_FAILED")
                events.append({**payload, "seq": seq, "event_hash": item_hash})
                previous = item_hash
            cursor = events[-1]["seq"] if events else after
            if cursor < maximum and len(events) < limit:
                raise PlatformError("LEDGER_PAGE_SEQUENCE_GAP")
            return {
                "schema_version": "qf-ledger-page-v1",
                "root_id": hashlib.sha256(str(self.root).encode()).hexdigest()[:24],
                "verification_scope": "CHAIN_SEGMENT_ONLY_NOT_FULL_AUDIT",
                "after": after,
                "anchor": initial_anchor,
                "events": events,
                "next_cursor": cursor,
                "next_anchor": previous,
                "head_seq": maximum,
                "head_hash": head[1] if head else "0" * 64,
                "has_more": cursor < maximum,
                "observed_at": datetime.now(UTC).isoformat(),
            }
