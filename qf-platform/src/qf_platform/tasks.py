"""Durable offline tasks; host-only tools, append-only evidence and safe replay."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from quantforge.product.storage import local_root, writer_lock

from . import task_state_codec as codec
from . import task_tools
from .storage import PlatformError, canonical, digest
from .view_cache import GenerationViewCache

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
_ID = re.compile(r"task-[a-f0-9]{32}\Z")
_KEY = re.compile(r"[a-zA-Z0-9_-]{1,80}\Z")


class TaskService:
    def __init__(
        self,
        root: Path,
        *,
        namespace: Literal["assistant-tasks", "agent-tasks"] = "assistant-tasks",
        cache_views: bool = False,
    ) -> None:
        if namespace not in {"assistant-tasks", "agent-tasks"}:
            raise PlatformError("TASK_NAMESPACE_INVALID")
        directory = local_root(root) / namespace
        directory.mkdir(exist_ok=True)
        self.root = local_root(directory)
        self.path = self.root / "tasks.sqlite"
        self._view_cache = GenerationViewCache(self.path) if cache_views else None
        # SQLite creates a WAL database's -wal/-shm files on first use and
        # deletes them when the last connection closes, so threads opening at
        # the same instant race to create them and all but one fail with
        # SQLITE_READONLY. Serialise connection creation. See
        # docs/WAL-CONNECTION-LIFETIME-20261016.md.
        self._connect_lock = threading.Lock()
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS tasks "
                "(id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, "
                "signature TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (task_id TEXT NOT NULL, "
                "seq INTEGER NOT NULL, payload TEXT NOT NULL, previous TEXT NOT NULL, "
                "hash TEXT NOT NULL, PRIMARY KEY(task_id,seq), "
                "FOREIGN KEY(task_id) REFERENCES tasks(id))"
            )
            for table in ("tasks", "events"):
                for operation in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                        f"BEFORE {operation} ON {table} BEGIN "
                        "SELECT RAISE(ABORT,'append-only'); END"
                    )
            codec.initialize(db)

    def connect(self) -> sqlite3.Connection:
        local_root(self.root)
        if self.path.is_symlink():
            raise PlatformError("TASK_STORE_REDIRECTED")
        # sqlite3.connect() is lazy: the first statement creates the
        # shared-memory files, so the pragmas stay inside the lock.
        with self._connect_lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _verified_rows(
        db: sqlite3.Connection, task_id: str, *, expand_states: bool = True
    ) -> Iterator[dict[str, Any]]:
        if not _ID.fullmatch(task_id):
            raise PlatformError("TASK_ID_INVALID")
        count = 0
        previous = "0" * 64
        verified_blobs: OrderedDict[str, None] = OrderedDict()
        for seq, raw, before, item_hash in db.execute(
            "SELECT seq,payload,previous,hash FROM events WHERE task_id=? ORDER BY seq",
            (task_id,),
        ):
            value = json.loads(raw)
            if (
                seq != count + 1
                or before != previous
                or raw != canonical(value)
                or digest({"previous": before, "payload": value}) != item_hash
                or value["state"]["id"] != task_id
            ):
                raise PlatformError("TASK_HISTORY_INTEGRITY_FAILED")
            if value.get("state_codec") is not None:
                if value["state_codec"] != codec.CODEC:
                    raise PlatformError("TASK_STATE_CODEC_UNSUPPORTED")
                codec.verify(db, value["state"], verified_blobs)
                if expand_states:
                    value["state"] = codec.unpack(db, value["state"])
            count += 1
            previous = item_hash
            yield {**value, "seq": seq, "event_hash": item_hash}
        if not count:
            raise PlatformError("TASK_NOT_FOUND")

    @classmethod
    def _history(cls, db: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
        return list(cls._verified_rows(db, task_id))

    @classmethod
    def _projected_history(
        cls, db: sqlite3.Connection, task_id: str
    ) -> list[dict[str, Any]]:
        own_snapshot = not db.in_transaction
        if own_snapshot:
            db.execute("BEGIN")
        try:
            return cls._projected_snapshot(db, task_id)
        finally:
            if own_snapshot:
                db.rollback()

    @classmethod
    def _projected_snapshot(
        cls, db: sqlite3.Connection, task_id: str
    ) -> list[dict[str, Any]]:
        events = []
        latest = None
        state_codec = None
        for row in cls._verified_rows(db, task_id, expand_states=False):
            latest, state_codec = row["state"], row.get("state_codec")
            events.append({key: value for key, value in row.items() if key != "state"})
        assert isinstance(latest, dict)
        events[-1]["state"] = codec.unpack(db, latest) if state_codec else latest
        return events

    @classmethod
    def _append(
        cls,
        db: sqlite3.Connection,
        state: dict[str, Any],
        kind: str,
        summary: str,
        **details: Any,
    ) -> None:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT 1 FROM events WHERE task_id=? LIMIT 1", (state["id"],)
        ).fetchone()
        latest = cls._latest_row(db, state["id"]) if existing else None
        sequence = latest["seq"] if latest else 0
        previous = latest["event_hash"] if latest else "0" * 64
        cls._insert_verified(db, state, kind, summary, sequence, previous, **details)

    @staticmethod
    def _insert_verified(
        db: sqlite3.Connection,
        state: dict[str, Any],
        kind: str,
        summary: str,
        sequence: int,
        previous: str,
        **details: Any,
    ) -> tuple[int, str]:
        # Only a full history verified in THIS write transaction, or the head
        # returned by this method in that same transaction, may reach here.
        # No history verification is reused across commits or tool execution.
        if not db.in_transaction:
            raise PlatformError("TASK_APPEND_TRANSACTION_REQUIRED")
        head = db.execute(
            "SELECT seq,hash FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1",
            (state["id"],),
        ).fetchone()
        expected = (sequence, previous) if sequence else None
        if head != expected:
            raise PlatformError("TASK_APPEND_HEAD_CONFLICT")
        at = datetime.now(UTC).isoformat()
        state["updated_at"] = at
        payload = {
            "at": at,
            "kind": kind,
            "summary": summary,
            "details": details,
            "state": codec.pack(db, state),
            "state_codec": codec.CODEC,
        }
        checksum = digest({"previous": previous, "payload": payload})
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (
                state["id"],
                sequence + 1,
                canonical(payload),
                previous,
                checksum,
            ),
        )
        return sequence + 1, checksum

    @staticmethod
    def _snapshot(history: list[dict[str, Any]]) -> dict[str, Any]:
        state: dict[str, Any] = copy.deepcopy(history[-1]["state"])
        state.update(
            schema_version="qf-task-v1",
            version=history[-1]["seq"],
            ledger_hash=history[-1]["event_hash"],
            observed_at=datetime.now(UTC).isoformat(),
            events=[
                {
                    key: row[key]
                    for key in ("seq", "at", "kind", "summary", "details", "event_hash")
                }
                for row in history
            ],
        )
        return state

    def get(self, task_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            return self._snapshot(self._projected_history(db, task_id))

    @classmethod
    def _latest_row(cls, db: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        latest = None
        for row in cls._verified_rows(db, task_id, expand_states=False):
            latest = row
        assert latest is not None
        if latest.get("state_codec"):
            latest["state"] = codec.unpack(db, latest["state"])
        return latest

    def close_view_cache(self) -> None:
        if self._view_cache is not None:
            self._view_cache.close()

    def view(self, task_id: str, *, deep_audit: bool = False) -> dict[str, Any]:
        """V3 full-chain state projection, with history fetched separately."""
        if self._view_cache is not None:
            return self._view_cache.read(
                task_id, lambda db: self._view_at_snapshot(db, task_id), deep=deep_audit
            )
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            return self._view_at_snapshot(db, task_id)

    def _view_at_snapshot(self, db: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        latest = self._latest_row(db, task_id)
        state = self._snapshot([latest])
        state.pop("events")
        return {
            "schema_version": "qf-task-view-v3",
            "namespace": self.root.name,
            "root_id": hashlib.sha256(str(self.root).encode()).hexdigest()[:24],
            "task_id": task_id,
            "verification_scope": "FULL_TASK_CHAIN_AND_CONTENT_AT_READ_SNAPSHOT",
            "history_embedded": False,
            "events_total": latest["seq"],
            "head_seq": latest["seq"],
            "head_hash": latest["event_hash"],
            "state": state,
        }

    def event_page(
        self,
        task_id: str,
        *,
        root_id: str,
        head_seq: int,
        head_hash: str,
        after: int = 0,
        anchor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        expected_root = hashlib.sha256(str(self.root).encode()).hexdigest()[:24]
        if not _ID.fullmatch(task_id) or root_id != expected_root:
            raise PlatformError("TASK_PAGE_ROOT_OR_TASK_CONFLICT")
        if (
            type(head_seq) is not int
            or type(after) is not int
            or not 0 <= after <= head_seq
            or type(limit) is not int
            or not 1 <= limit <= 500
            or not isinstance(head_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", head_hash)
        ):
            raise PlatformError("TASK_PAGE_CURSOR_INVALID")
        if (
            after
            and (
                not isinstance(anchor, str) or not re.fullmatch(r"[a-f0-9]{64}", anchor)
            )
        ) or (after == 0 and anchor not in {None, "0" * 64}):
            raise PlatformError("TASK_PAGE_ANCHOR_INVALID")
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            blobs: OrderedDict[str, None] = OrderedDict()

            def checked(seq: int) -> tuple[dict[str, Any], str]:
                row = db.execute(
                    "SELECT payload,previous,hash FROM events "
                    "WHERE task_id=? AND seq=?",
                    (task_id, seq),
                ).fetchone()
                if row is None:
                    raise PlatformError("TASK_PAGE_EVENT_MISSING")
                raw, before, checksum = row
                value = json.loads(raw)
                if (
                    raw != canonical(value)
                    or digest({"previous": before, "payload": value}) != checksum
                    or value["state"]["id"] != task_id
                ):
                    raise PlatformError("TASK_PAGE_EVENT_INTEGRITY_FAILED")
                if value.get("state_codec") is not None:
                    if value["state_codec"] != codec.CODEC:
                        raise PlatformError("TASK_STATE_CODEC_UNSUPPORTED")
                    codec.verify(db, value["state"], blobs)
                return value, checksum

            _, current_head = checked(head_seq)
            if current_head != head_hash:
                raise PlatformError("TASK_PAGE_HEAD_CONFLICT")
            previous = "0" * 64
            if after:
                _, previous = checked(after)
                if previous != anchor:
                    raise PlatformError("TASK_PAGE_ANCHOR_CONFLICT")
            initial = previous
            events: list[dict[str, Any]] = []
            for seq, before in db.execute(
                "SELECT seq,previous FROM events "
                "WHERE task_id=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                (task_id, after, head_seq, limit),
            ):
                value, checksum = checked(seq)
                if seq != after + len(events) + 1 or before != previous:
                    raise PlatformError("TASK_PAGE_CHAIN_GAP_OR_CONFLICT")
                events.append(
                    {key: value[key] for key in ("at", "kind", "summary", "details")}
                    | {"seq": seq, "event_hash": checksum}
                )
                previous = checksum
            cursor = events[-1]["seq"] if events else after
            if cursor < head_seq and len(events) < limit:
                raise PlatformError("TASK_PAGE_CHAIN_GAP_OR_CONFLICT")
            return {
                "schema_version": "qf-task-event-page-v3",
                "task_id": task_id,
                "namespace": self.root.name,
                "root_id": expected_root,
                "verification_scope": "CHAIN_SEGMENT_AND_CONTENT_NOT_FULL_AUDIT",
                "head_seq": head_seq,
                "head_hash": head_hash,
                "after": after,
                "anchor": initial,
                "events": events,
                "next_cursor": cursor,
                "next_anchor": previous,
                "has_more": cursor < head_seq,
                "observed_at": datetime.now(UTC).isoformat(),
            }

    def list(self) -> dict[str, Any]:
        if self._view_cache is not None:
            return self._view_cache.read("__list__", self._list_at_snapshot)
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            return self._list_at_snapshot(db)

    def _list_at_snapshot(self, db: sqlite3.Connection) -> dict[str, Any]:
        items = []
        for (task_id,) in db.execute(
            "SELECT id FROM tasks ORDER BY created_at DESC,id DESC LIMIT 100"
        ).fetchall():
            value = self._snapshot([self._latest_row(db, task_id)])
            items.append(
                {
                    key: value[key]
                    for key in (
                        "id",
                        "request",
                        "status",
                        "created_at",
                        "updated_at",
                        "completed_steps",
                        "total_steps",
                        "version",
                        "error",
                    )
                }
            )
        return {
            "schema_version": "qf-task-list-v1",
            "mode": "OFFLINE_DEMO",
            "paid_model_calls": 0,
            "items": items,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def create(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        if not _KEY.fullmatch(request_id):
            raise PlatformError("TASK_IDEMPOTENCY_KEY_REQUIRED")
        request = task_tools.TaskRequest.model_validate(payload)
        signature = digest(request.model_dump())
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT id,signature FROM tasks WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior:
                if prior[1] != signature:
                    raise PlatformError("TASK_IDEMPOTENCY_CONFLICT")
                return self._snapshot(self._projected_history(db, prior[0]))
            if db.execute("SELECT count(*) FROM tasks").fetchone()[0] >= 100:
                raise PlatformError("TASK_WORKSPACE_BUDGET_EXHAUSTED")
            task_id = "task-" + uuid.uuid4().hex
            stamp = datetime.now(UTC).isoformat()
            steps = task_tools.plan(request)
            state = {
                "id": task_id,
                "request": request.model_dump(),
                "mode": "OFFLINE_DEMO",
                "planner": "SCRIPTED_DEMO_V1",
                "paid_model_calls": 0,
                "live_authority": "NONE",
                "created_at": stamp,
                "updated_at": stamp,
                "status": "PENDING",
                "plan": steps,
                "completed_steps": 0,
                "total_steps": len(steps),
                "experiments_used": 0,
                "evidence": [],
                "report": None,
                "error": None,
            }
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?)",
                (task_id, request_id, signature, stamp),
            )
            self._append(
                db,
                state,
                "TASK_CREATED",
                "离线研究任务已登记",
                planner="SCRIPTED_DEMO_V1",
                max_experiments=request.max_experiments,
            )
            return self._snapshot(self._projected_history(db, task_id))

    def control(self, task_id: str, action: str) -> dict[str, Any]:
        if action not in {"cancel", "resume"}:
            raise PlatformError("TASK_CONTROL_INVALID")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = copy.deepcopy(self._projected_history(db, task_id)[-1]["state"])
            if action == "cancel":
                if state["status"] in {"PENDING", "RUNNING"}:
                    state["status"] = "CANCELLED"
                    self._append(db, state, "TASK_CANCELLED", "任务已停止")
            else:
                if state["status"] != "CANCELLED":
                    raise PlatformError("TASK_RESUME_REQUIRES_CANCELLED")
                state["status"] = "PENDING"
                self._append(db, state, "TASK_RESUMED", "沿用原任务与证据继续离线步骤")
            return self._snapshot(self._projected_history(db, task_id))

    def tick(self) -> bool:
        # The crash-released process lock covers a whole pure tool step.
        try:
            with writer_lock(self.root):
                return self._step()
        except OSError:
            return False

    def _step(self) -> bool:
        with closing(self.connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                selected = None
                for (task_id,) in db.execute(
                    "SELECT id FROM tasks ORDER BY created_at,id"
                ).fetchall():
                    latest = self._latest_row(db, task_id)
                    if latest["state"]["status"] not in TERMINAL:
                        selected = (task_id, latest)
                        break
                if selected is None:
                    return False
                task_id, latest = selected
                state = copy.deepcopy(latest["state"])
                step = state["plan"][state["completed_steps"]]
                state["status"] = "RUNNING"
                version, _ = self._insert_verified(
                    db,
                    state,
                    "TOOL_STARTED",
                    "开始验证并执行受限工具",
                    latest["seq"],
                    latest["event_hash"],
                    tool=step["tool"],
                    input=step["input"],
                    step=state["completed_steps"] + 1,
                )
            started = time.perf_counter()
            try:
                if (
                    step["tool"] == "run_experiment"
                    and state["experiments_used"] >= state["request"]["max_experiments"]
                ):
                    raise PlatformError("TASK_EXPERIMENT_BUDGET_EXHAUSTED")
                evidence = task_tools.execute(
                    step["tool"], step["input"], state["evidence"]
                )
                failure = None
            except (ValueError, TypeError, KeyError, ArithmeticError):
                evidence = None
                failure = "TASK_TOOL_VALIDATION_OR_COMPUTE_FAILED"
            with db:
                db.execute("BEGIN IMMEDIATE")
                current = self._latest_row(db, task_id)
                if current["seq"] != version:
                    return True
                head = (current["seq"], current["event_hash"])
                if failure:
                    state.update(status="FAILED", error=failure)
                    self._insert_verified(
                        db, state, "TASK_FAILED", "工具未完成，结果待核查", *head
                    )
                else:
                    assert evidence is not None
                    state["evidence"].append(evidence)
                    state["completed_steps"] += 1
                    state["experiments_used"] += int(step["tool"] == "run_experiment")
                    head = self._insert_verified(
                        db,
                        state,
                        "TOOL_COMPLETED",
                        "工具结果与证据已提交",
                        *head,
                        tool=step["tool"],
                        evidence_id=evidence["evidence_id"],
                        duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    )
                    if state["completed_steps"] == state["total_steps"]:
                        state["report"] = task_tools.report(state)
                        state["status"] = "SUCCEEDED"
                        self._insert_verified(
                            db, state, "REPORT_READY", "比较报告已完成，可导出", *head
                        )
            return True
