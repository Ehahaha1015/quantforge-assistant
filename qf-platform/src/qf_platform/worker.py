"""Independent local workers; leases coordinate liveness, OS locks fence effects."""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import ExitStack, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from quantforge.product.storage import local_root, writer_lock

from .storage import PlatformError

DOMAINS = ("assistant-tasks", "agent-tasks", "simulation", "research-rooms")


class WorkerLease:
    def __init__(self, root: Path, domain: str, *, ttl: int = 15) -> None:
        if domain not in DOMAINS or type(ttl) is not int or not 3 <= ttl <= 300:
            raise PlatformError("WORKER_LEASE_POLICY_INVALID")
        directory = local_root(root) / "workers"
        directory.mkdir(exist_ok=True)
        self.root = local_root(directory)
        self.path = self.root / "leases.sqlite"
        self.domain, self.ttl = domain, ttl
        self.owner = uuid.uuid4().hex
        # SQLite creates a WAL database's -wal/-shm files on first use and
        # deletes them when the last connection closes, so leases opening at
        # the same instant race to create them and all but one fail with
        # SQLITE_READONLY. Serialise connection creation. See
        # docs/WAL-CONNECTION-LIFETIME-20261016.md.
        self._connect_lock = threading.Lock()
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS leases (domain TEXT PRIMARY KEY, "
                "owner TEXT NOT NULL, generation INTEGER NOT NULL, pid INTEGER "
                "NOT NULL, heartbeat TEXT NOT NULL, expires TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS worker_states "
                "(domain TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL, "
                "reason TEXT, observed_at TEXT NOT NULL)"
            )

    def connect(self) -> sqlite3.Connection:
        local_root(self.root)
        if self.path.is_symlink():
            raise PlatformError("WORKER_LEASE_REDIRECTED")
        # sqlite3.connect() is lazy: the first statement creates the
        # shared-memory files, so the pragma stays inside the lock.
        with self._connect_lock:
            db = sqlite3.connect(self.path, timeout=5)
            db.execute("PRAGMA synchronous=FULL")
        return db

    def renew(self, *, now: datetime | None = None) -> bool:
        at = datetime.now(UTC) if now is None else now
        if at.tzinfo is None or at.utcoffset() is None:
            raise PlatformError("WORKER_LEASE_NAIVE_TIME")
        at = at.astimezone(UTC)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT owner,generation,expires FROM leases WHERE domain=?",
                (self.domain,),
            ).fetchone()
            if (
                prior
                and prior[0] != self.owner
                and datetime.fromisoformat(prior[2]) > at
            ):
                return False
            generation = prior[1] + int(prior[0] != self.owner) if prior else 1
            db.execute(
                "INSERT INTO leases VALUES(?,?,?,?,?,?) ON CONFLICT(domain) "
                "DO UPDATE SET owner=excluded.owner,generation=excluded.generation,"
                "pid=excluded.pid,heartbeat=excluded.heartbeat,expires=excluded.expires",
                (
                    self.domain,
                    self.owner,
                    generation,
                    os.getpid(),
                    at.isoformat(),
                    (at + timedelta(seconds=self.ttl)).isoformat(),
                ),
            )
            return True

    def release(self) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE leases SET expires=? WHERE domain=? AND owner=?",
                (datetime.now(UTC).isoformat(), self.domain, self.owner),
            )

    def status(self) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT owner,generation,pid,heartbeat,expires FROM leases "
                "WHERE domain=?",
                (self.domain,),
            ).fetchone()
            activity = db.execute(
                "SELECT owner,state,reason,observed_at FROM worker_states "
                "WHERE domain=?",
                (self.domain,),
            ).fetchone()
        if row is None:
            return None
        result = dict(
            zip(
                ("owner", "generation", "pid", "heartbeat", "expires"), row, strict=True
            )
        )
        result["activity"] = (
            dict(
                zip(("owner", "state", "reason", "observed_at"), activity, strict=True)
            )
            if activity
            else None
        )
        return result

    def record_activity(self, state: str, reason: str | None = None) -> None:
        if state not in {
            "IDLE",
            "STEP_RUNNING",
            "STEP_COMMITTED",
            "STEP_FAILED",
            "LEASE_LOST",
            "STOPPED",
        } or reason not in {None, "STEP_REQUIRES_REVIEW", "LEASE_RENEWAL_FAILED"}:
            raise PlatformError("WORKER_ACTIVITY_INVALID")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT owner FROM leases WHERE domain=?", (self.domain,)
            ).fetchone()
            if not owner or owner[0] != self.owner:
                return
            db.execute(
                "INSERT INTO worker_states VALUES(?,?,?,?,?) ON CONFLICT(domain) "
                "DO UPDATE SET owner=excluded.owner,state=excluded.state,"
                "reason=excluded.reason,observed_at=excluded.observed_at",
                (self.domain, self.owner, state, reason, datetime.now(UTC).isoformat()),
            )


class Worker:
    def __init__(
        self,
        root: Path,
        domain: str,
        *,
        tick: Callable[[], bool] | None = None,
        demo: bool = False,
    ) -> None:
        if demo:
            from .chart_analysis import ChartAnalysisService
            from .chart_demo import OfflineChartRuntime

            if domain != "assistant-tasks" or tick is not None:
                raise PlatformError("DEMO_WORKER_DOMAIN_INVALID")
            tick = ChartAnalysisService(root, runtime=OfflineChartRuntime(root)).tick
        self.lease = WorkerLease(root, domain)
        directory = self.lease.root / domain
        directory.mkdir(exist_ok=True)
        self.fence = local_root(directory)
        self.stop = threading.Event()
        self.tick = tick
        if self.tick is None:
            if domain == "agent-tasks":
                from .agent_tasks import AgentTaskService

                self.tick = AgentTaskService(root).tick
            elif domain == "assistant-tasks":
                from .chart_analysis import ChartAnalysisService
                from .chart_forward import DailyForward
                from .personal_service import PersonalService
                from .tasks import TaskService

                offline = TaskService(root)
                charts = ChartAnalysisService(root)
                personal = PersonalService(root)
                forward = DailyForward(root)

                def chart_and_personal_tick() -> bool:
                    changed = charts.tick()
                    changed = personal.tick() or changed
                    return changed or forward.idle_tick()

                def assistant_tick() -> bool:
                    changed = offline.tick()
                    return chart_and_personal_tick() or changed

                self.tick = (
                    chart_and_personal_tick
                    if os.environ.get("QF_CHART_ONLY") == "1"
                    else assistant_tick
                )
            elif domain == "research-rooms":
                from .bindings import BindingRegistry
                from .research_room import RoomService

                room = RoomService(root, BindingRegistry(root), external_worker=True)
                self.tick = lambda: room.tick(self.stop)
            else:
                from .service import PlatformService

                self.tick = PlatformService(root).autonomous_tick

    def run_once(self) -> bool:
        # A live stale owner cannot execute concurrently with a lease replacement.
        with ExitStack() as stack:
            try:
                stack.enter_context(writer_lock(self.fence))
            except OSError:
                return False
            if self.stop.is_set() or not self.lease.renew():
                return False
            assert self.tick is not None
            self.lease.record_activity("STEP_RUNNING")
            changed = self.tick()
            self.lease.record_activity("STEP_COMMITTED" if changed else "IDLE")
            return changed

    def run(self) -> None:
        lease_failed = threading.Event()

        def heartbeat() -> None:
            while not self.stop.wait(self.lease.ttl / 3):
                try:
                    if not self.lease.renew():
                        lease_failed.set()
                        self.stop.set()
                except (ValueError, OSError, sqlite3.Error):
                    lease_failed.set()
                    self.stop.set()

        while not self.lease.renew():
            if self.stop.wait(0.5):
                return
        pulse = threading.Thread(target=heartbeat, daemon=True)
        pulse.start()
        failures = 0
        try:
            while not self.stop.is_set():
                try:
                    self.run_once()
                    failures = 0
                except (ValueError, OSError, sqlite3.Error):
                    # Original task/call journals retain effects and recovery authority.
                    failures += 1
                    self.lease.record_activity("STEP_FAILED", "STEP_REQUIRES_REVIEW")
                    if failures >= 3:
                        raise PlatformError("WORKER_REPEATED_STEP_FAILURE") from None
                self.stop.wait(0.5)
        finally:
            self.stop.set()
            pulse.join(timeout=6)
            if lease_failed.is_set():
                self.lease.record_activity("LEASE_LOST", "LEASE_RENEWAL_FAILED")
            elif failures:
                self.lease.record_activity("STEP_FAILED", "STEP_REQUIRES_REVIEW")
            else:
                self.lease.record_activity("STOPPED")
            self.lease.release()

    def watch_parent(self, descriptor: int) -> threading.Thread:
        def control() -> None:
            try:
                os.read(descriptor, 1)
            except OSError:
                pass
            finally:
                # EOF survives parent crashes and avoids PID-reuse heuristics.
                self.stop.set()

        thread = threading.Thread(target=control, daemon=True)
        thread.start()
        return thread


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--domain", choices=DOMAINS, required=True)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--managed-stdin", action="store_true")
    args = parser.parse_args()
    worker = Worker(args.root, args.domain, demo=args.demo)
    if args.managed_stdin:
        worker.watch_parent(sys.stdin.fileno())
    for kind in (signal.SIGINT, signal.SIGTERM):
        signal.signal(kind, lambda *_: worker.stop.set())
    if args.once:
        try:
            worker.run_once()
        finally:
            worker.lease.release()
    else:
        worker.run()


if __name__ == "__main__":
    main()
