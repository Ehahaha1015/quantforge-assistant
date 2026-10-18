"""Bounded local child lifecycle; never retries model calls itself."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from quantforge.product.storage import local_root

from .processes import python_process
from .storage import PlatformError
from .worker import DOMAINS, WorkerLease


class WorkerSupervisor:
    def __init__(
        self,
        root: Path,
        *,
        domains: tuple[str, ...] = DOMAINS,
        restart_limit: int = 2,
        demo: bool = False,
    ) -> None:
        if (
            not domains
            or len(set(domains)) != len(domains)
            or any(domain not in DOMAINS for domain in domains)
            or type(restart_limit) is not int
            or not 0 <= restart_limit <= 3
        ):
            raise PlatformError("WORKER_SUPERVISOR_POLICY_INVALID")
        self.demo = demo
        if demo:
            from .chart_demo import check_demo_root

            check_demo_root(root)
            if domains != ("assistant-tasks",):
                raise PlatformError("DEMO_WORKER_DOMAIN_INVALID")
        self.root = local_root(root)
        directory = self.root / "worker-logs"
        directory.mkdir(exist_ok=True)
        self.logs = local_root(directory)
        self.domains, self.restart_limit = domains, restart_limit
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.restarts = dict.fromkeys(domains, 0)
        self.exits: dict[str, int | None] = dict.fromkeys(domains)
        self.retry_at = dict.fromkeys(domains, 0.0)
        self.observers = {domain: WorkerLease(root, domain) for domain in domains}
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None

    def _spawn(self, domain: str) -> None:
        path = self.logs / (domain + ".log")
        if path.is_symlink():
            raise PlatformError("WORKER_LOG_REDIRECTED")
        command, environment = python_process(
            [
                "-m",
                "qf_platform.worker",
                "--root",
                str(self.root),
                "--domain",
                domain,
                "--managed-stdin",
                *(["--demo"] if self.demo else []),
            ]
        )
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW
        with path.open("ab") as stream:
            self.children[domain] = subprocess.Popen(
                command,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=stream,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=creation_flags,
            )

    def start(self) -> None:
        with self.lock:
            if self.thread is not None or self.stop.is_set():
                raise PlatformError("WORKER_SUPERVISOR_ALREADY_STARTED")
            try:
                for domain in self.domains:
                    self._spawn(domain)
            except BaseException:
                self.close()
                raise
            self.thread = threading.Thread(target=self._monitor, daemon=True)
            self.thread.start()

    def _monitor(self) -> None:
        while not self.stop.wait(0.5):
            self.poll()

    def poll(self) -> None:
        with self.lock:
            if self.stop.is_set():
                return
            for domain, process in list(self.children.items()):
                code = process.poll()
                if code is None:
                    continue
                self.exits[domain] = code
                if self.restarts[domain] >= self.restart_limit:
                    continue
                if not self.retry_at[domain]:
                    self.retry_at[domain] = time.monotonic() + 0.5
                if time.monotonic() < self.retry_at[domain]:
                    continue
                if process.stdin is not None:
                    process.stdin.close()
                self.restarts[domain] += 1
                self.retry_at[domain] = 0.0
                try:
                    self._spawn(domain)
                except (OSError, ValueError):
                    self.exits[domain] = -1

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            rows = {}
            for domain in self.domains:
                process = self.children.get(domain)
                alive = process is not None and process.poll() is None
                observation_error = False
                try:
                    lease = self.observers[domain].status()
                except (ValueError, OSError, sqlite3.Error):
                    lease = None
                    observation_error = True
                owns = (
                    lease is not None
                    and process is not None
                    and lease["pid"] == process.pid
                )
                activity = lease.get("activity") if owns and lease else None
                rows[domain] = {
                    "pid": process.pid if process else None,
                    "alive": alive,
                    "state": "OBSERVATION_UNAVAILABLE"
                    if observation_error
                    else (
                        activity["state"]
                        if activity and lease and activity["owner"] == lease["owner"]
                        else "STARTING_OR_WAITING_FOR_LEASE"
                    )
                    if alive
                    else (
                        "RESTART_LIMIT_REACHED"
                        if self.restarts[domain] >= self.restart_limit
                        else "RESTART_PENDING"
                    ),
                    "restarts": self.restarts[domain],
                    "restart_limit": self.restart_limit,
                    "last_exit_code": self.exits[domain],
                    "lease": lease if owns else None,
                }
            return rows

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        with self.lock:
            for process in self.children.values():
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
            for process in self.children.values():
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
