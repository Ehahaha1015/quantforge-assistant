"""Bounded process-memory audit reuse, invalidated by one SQLite observer and files."""

from __future__ import annotations

import copy
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantforge.product.storage import local_root

from .storage import PlatformError, canonical

REUSED_SCOPE = "FULL_AUDIT_REUSED_FOR_UNCHANGED_SQLITE_GENERATION"


class GenerationViewCache:
    """Process-memory reuse, not a durable signature or hostile raw-file proof."""

    def __init__(self, path: Path, *, capacity: int = 16) -> None:
        self.path = path
        self.capacity = capacity
        self.lock = threading.RLock()
        self.db: sqlite3.Connection | None = None
        self.file_id: tuple[int, int] | None = None
        self.entries: OrderedDict[str, tuple[int, tuple[Any, ...], dict[str, Any]]] = (
            OrderedDict()
        )

    def _files(self) -> tuple[Any, ...]:
        local_root(self.path.parent)
        values: list[tuple[int, ...] | None] = []
        for path in (self.path, self.path.with_name(self.path.name + "-wal")):
            if path.is_symlink():
                raise PlatformError("TASK_STORE_REDIRECTED")
            if path.exists():
                stat = path.stat()
                values.append(
                    (
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                    )
                )
            else:
                values.append(None)
        if values[0] is None:
            raise PlatformError("TASK_STORE_MISSING")
        return tuple(values)

    def close(self) -> None:
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None
            self.file_id = None
            self.entries.clear()

    def _connection(self) -> sqlite3.Connection:
        files = self._files()
        current_id = (files[0][0], files[0][1])
        if self.db is not None and self.file_id != current_id:
            self.close()
        if self.db is None:
            self.db = sqlite3.connect(self.path, timeout=1, check_same_thread=False)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.file_id = current_id
        return self.db

    @staticmethod
    def _version(db: sqlite3.Connection) -> int:
        return int(db.execute("PRAGMA data_version").fetchone()[0])

    @staticmethod
    def _response(
        value: dict[str, Any], *, hit: bool, generation: int
    ) -> dict[str, Any]:
        result = copy.deepcopy(value)
        observation = result["state"] if "state" in result else result
        audited_at = observation["observed_at"]
        if hit:
            result["verification_scope"] = REUSED_SCOPE
            observation["observed_at"] = datetime.now(UTC).isoformat()
        result["verification_details"] = {
            "method": "UNCHANGED_SQLITE_GENERATION_REUSE" if hit else "FULL_SCAN",
            "audited_at": audited_at,
            "observer_generation": generation,
            "boundary": "TRUSTED_PROCESS_SQLITE_COMMITS_AND_FILE_IDENTITIES_"
            "NOT_EXTERNAL_SIGNATURE_OR_HOSTILE_RAW_FILE_PROOF",
        }
        return result

    def read(
        self,
        key: str,
        audit: Callable[[sqlite3.Connection], dict[str, Any]],
        *,
        deep: bool = False,
    ) -> dict[str, Any]:
        with self.lock:
            db = self._connection()
            # Fence writers only for the short generation check. Never hold a
            # write reservation during the O(N) full audit.
            try:
                db.execute("BEGIN IMMEDIATE")
                version, files = self._version(db), self._files()
                entry = self.entries.get(key)
                if (
                    not deep
                    and entry is not None
                    and entry[0] == version
                    and entry[1] == files
                ):
                    self.entries.move_to_end(key)
                    return self._response(entry[2], hit=True, generation=version)
                if any(
                    item[0] != version or item[1] != files
                    for item in self.entries.values()
                ):
                    self.entries.clear()
                elif deep:
                    self.entries.pop(key, None)
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode not in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                }:
                    raise
                # A long append owns the writer reservation, but WAL still
                # permits a fully audited read snapshot. Never reuse an entry
                # when its generation fence could not be acquired.
                self.entries.clear()
            finally:
                db.rollback()
            before_version, before_files = self._version(db), self._files()
            db.execute("BEGIN")
            try:
                result = audit(db)
            except BaseException:
                self.entries.clear()
                raise
            finally:
                db.rollback()
            after_version, after_files = self._version(db), self._files()
            if (after_files[0][0], after_files[0][1]) != self.file_id:
                self.close()
                raise PlatformError("TASK_STORE_REPLACED_DURING_AUDIT")
            if (
                before_version == after_version
                and before_files == after_files
                and len(canonical(result).encode()) <= 2_000_000
            ):
                self.entries[key] = (after_version, after_files, copy.deepcopy(result))
                while len(self.entries) > self.capacity:
                    self.entries.popitem(last=False)
            return self._response(result, hit=False, generation=before_version)
