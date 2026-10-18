"""Product composition of protected model bindings and the research-only Lab."""

from __future__ import annotations

import threading
import uuid
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any

from qf_lab.gateway import GatewayError
from qf_lab.room import ResearchRoom
from qf_lab.room_gateway import FakeRoomProvider, StructuredProvider
from quantforge.product.storage import writer_lock

from .bindings import BindingRegistry
from .room_dispatch import RoomDispatch
from .storage import PlatformError, digest


class RoomService:
    def __init__(
        self, root: Path, bindings: BindingRegistry, *, external_worker: bool = False
    ) -> None:
        self.root, self.bindings = root, bindings
        self.rooms = root / "research-rooms"
        self.core_root = root / "research-core"
        self.rooms.mkdir(exist_ok=True)
        self.core_root.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.current: ResearchRoom | None = None
        self.external_worker = external_worker
        self.dispatch = RoomDispatch(root)
        programs = self._programs()
        if programs:
            self.select(max(programs, key=lambda p: p.stat().st_mtime).name)

    def _programs(self) -> list[Path]:
        programs = []
        for directory in self.rooms.iterdir():
            if (
                not directory.is_dir()
                or directory.is_symlink()
                or not (directory / "room-history.sqlite").is_file()
            ):
                continue
            candidate = ResearchRoom(directory, self.core_root, self._routes(True))
            history = candidate.history()
            if history and history[0].get("kind") == "DECLARATION":
                programs.append(directory)
        return programs

    @staticmethod
    def _execute(room: ResearchRoom) -> None:
        try:
            room.run()
        except OSError:
            # A competing process may own the writer lock. Only its owner may
            # write terminal history; contention is never research failure.
            return
        except Exception:
            # No raw provider exception reaches the thread hook or service log.
            if room.snapshot()["status"] not in {"FAILED", "COMPLETE", "STOPPED"}:
                room.event("FAILED", classification="RESEARCH_WORKER_UNAVAILABLE")

    def select(self, program_id: str) -> dict[str, Any]:
        if (
            not program_id.startswith("room-")
            or len(program_id) != 25
            or any(c not in "0123456789abcdef" for c in program_id[5:])
        ):
            raise PlatformError("ROOM_PROGRAM_INVALID")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise PlatformError("ROOM_ALREADY_RUNNING")
            if self.external_worker and any(
                row["status"] in {"QUEUED", "RUNNING"}
                and row["program_id"] != program_id
                for row in self.dispatch.rows()
            ):
                raise PlatformError("ROOM_ALREADY_RUNNING")
            directory = self.rooms / program_id
            if not (directory / "room-history.sqlite").is_file():
                raise PlatformError("ROOM_PROGRAM_NOT_FOUND")
            candidate = ResearchRoom(directory, self.core_root, self._routes(True))
            history = candidate.history()
            if not history or history[0].get("kind") != "DECLARATION":
                raise PlatformError("ROOM_NOT_DECLARED")
            self.current = candidate
        return self.snapshot()

    def _routes(self, synthetic: bool) -> dict[str, list[StructuredProvider]]:
        if synthetic:
            return {
                role: [FakeRoomProvider(role)]
                for role in ("researcher", "critic", "director", "validator")
            }
        assignments = self.bindings.roles()
        if set(assignments) != {"researcher", "critic", "director", "validator"}:
            raise PlatformError("ALL_RESEARCH_ROLES_REQUIRED")
        routes: dict[str, list[StructuredProvider]] = {}
        for role, items in assignments.items():
            routes[role] = []
            for item in items:
                meta = self.bindings.get(item["profile_id"])
                if meta["health"] != "HEALTHY":
                    raise PlatformError("MODEL_CONNECTION_TEST_REQUIRED")
                if Decimal(meta["output_price_per_million"]) <= 0:
                    raise PlatformError("DECLARED_MODEL_PRICING_REQUIRED")
                routes[role].append(
                    self.bindings.model_provider(item["profile_id"], item["model"])
                )
        return routes

    def datasets(self) -> list[dict[str, Any]]:
        from quantforge.data import ParquetDataCatalog

        catalog = ParquetDataCatalog(self.core_root / "catalog")
        return [
            catalog.verify(p.name).model_dump(mode="json")
            for p in sorted(catalog.root.iterdir())
            if p.is_dir() and not p.name.startswith(".")
        ]

    def _fixture(self) -> str:
        """An explicit synthetic demo, isolated from accepted or real datasets."""
        from datetime import UTC, datetime, timedelta

        import polars as pl
        from quantforge.data import BAR_SCHEMA, ParquetDataCatalog

        catalog = ParquetDataCatalog(self.core_root / "catalog")
        dataset = "room-synthetic-100"
        if any(v["dataset_id"] == dataset for v in self.datasets()):
            return dataset
        start = datetime(2020, 1, 1, tzinfo=UTC)
        prices = [100.0 + i * 0.1 + (i % 17) for i in range(100)]
        catalog.write(
            dataset,
            pl.DataFrame(
                {
                    "symbol": ["FIXTURE"] * 100,
                    "timestamp": [start + timedelta(days=i) for i in range(100)],
                    "open": prices,
                    "close": [v + 1 for v in prices],
                    "high": [v + 2 for v in prices],
                    "low": [v - 2 for v in prices],
                    "volume": [1000.0] * 100,
                },
                schema=BAR_SCHEMA,
            ),
            source="SYNTHETIC_PLATFORM_FIXTURE",
            schema_version="bars-v1",
            as_of=start + timedelta(days=100),
        )
        return dataset

    def acquire_demo(self) -> dict[str, Any]:
        from datetime import UTC, datetime

        from qf_lab.historical import fetch_public_demo
        from quantforge.historical.alphavantage import download_snapshot

        with self.lock:
            if self.worker and self.worker.is_alive():
                raise PlatformError("ROOM_ALREADY_RUNNING")
            if self.external_worker and self.dispatch.active():
                raise PlatformError("ROOM_ALREADY_RUNNING")
            download_snapshot(
                self.core_root,
                retrieved_at=datetime.now(UTC),
                fetcher=fetch_public_demo,
            )
        return self.snapshot()

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        queued_job = None
        if set(payload) - {
            "mode",
            "goal",
            "dataset_id",
            "rounds",
            "calls",
            "tokens",
            "cost",
            "review_run_id",
            "wall_seconds",
        }:
            raise PlatformError("ROOM_INPUT_INVALID")
        synthetic = payload.get("mode") == "SYNTHETIC"
        if not synthetic and payload.get("mode") != "HISTORICAL":
            raise PlatformError("RESEARCH_MODE_REQUIRED")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise PlatformError("ROOM_ALREADY_RUNNING")
            if self.external_worker and self.dispatch.active():
                raise PlatformError("ROOM_ALREADY_RUNNING")
            routes = self._routes(synthetic)
            rounds = payload.get("rounds", 3)
            minimum_calls = (
                3
                if payload.get("review_run_id") is not None
                else rounds * (len(routes["researcher"]) + len(routes["critic"]) + 4)
                if type(rounds) is int
                else 0
            )
            dataset = self._fixture() if synthetic else payload.get("dataset_id")
            if not isinstance(dataset, str) or not dataset:
                raise PlatformError("QUALIFIED_DATASET_REQUIRED")
            if not synthetic and any(
                v["dataset_id"] == dataset and "synthetic" in v["source"].lower()
                for v in self.datasets()
            ):
                raise PlatformError("SYNTHETIC_DATASET_IS_NOT_HISTORICAL")
            identity = "room-" + uuid.uuid4().hex[:20]
            directory = self.rooms / identity
            directory.mkdir()
            candidate = ResearchRoom(directory, self.core_root, routes)
            try:
                candidate.declare(
                    program_id=identity,
                    goal=payload.get(
                        "goal",
                        "Test causal families with fixed costs and temporal validation",
                    ),
                    dataset_id=dataset,
                    rounds=rounds,
                    calls=payload.get("calls", minimum_calls),
                    tokens=payload.get("tokens", 250000),
                    cost=Decimal(payload.get("cost", "1")),
                    review_run_id=payload.get("review_run_id"),
                    wall_seconds=payload.get("wall_seconds", 600),
                )
            except (GatewayError, ValueError, TypeError, DecimalException) as exc:
                code = "DATASET_ALREADY_BOUND_OR_UNRESOLVED"
                raise PlatformError(
                    code if str(exc) == code else "ROOM_DECLARATION_INVALID"
                ) from None
            self.current = candidate
            if self.external_worker:
                queued_job = self.dispatch.enqueue(
                    identity, digest(candidate.history()[0]["declaration"])
                )
            else:
                self.worker = threading.Thread(
                    target=self._execute, args=(candidate,), daemon=True
                )
                self.worker.start()
        value = self.snapshot()
        if queued_job is not None:
            value["dispatch_receipt"] = {
                key: queued_job[key]
                for key in ("job_id", "declaration_hash", "event_hash", "status")
            }
        return value

    def evidence(self, run_id: str) -> dict[str, Any]:
        from quantforge.product.service import ResearchService

        if self.current is None or not any(
            row["kind"] in {"EVIDENCE_REFERENCE", "RETAINED_EVIDENCE_REVIEW"}
            and row["run_id"] == run_id
            for row in self.current.history()
        ):
            raise PlatformError("ROOM_EVIDENCE_NOT_FOUND")
        core = ResearchService(self.core_root)
        try:
            return {
                "mode": self.current.snapshot()["mode"],
                "live_authority": "NONE",
                "evidence": core.model_context(run_id),
            }
        finally:
            core.close()

    def control(self, action: str) -> dict[str, Any]:
        if self.current is None:
            raise PlatformError("ROOM_NOT_RUNNING")
        if self.external_worker:
            declaration = self.current.snapshot()["declaration"]
            if action == "resume" and self.dispatch.active(declaration["program_id"]):
                raise PlatformError("ROOM_WORKER_TRANSITION_PENDING")
            self.current.control(action)
            if action == "resume":
                self.dispatch.enqueue(declaration["program_id"], digest(declaration))
            return self.snapshot()
        if action == "resume":
            with self.lock:
                if self.worker and self.worker.is_alive():
                    # Wait for the paused worker to relinquish its writer lock.
                    # No misleading resume CONTROL when no successor can start.
                    raise PlatformError("ROOM_WORKER_TRANSITION_PENDING")
                # Loading history uses inspection-only fake routes. Execution
                # must reconstruct the declared mode from protected bindings.
                self.current.routes = self._routes(
                    self.current.snapshot()["declaration"].get(
                        "provider_mode",
                        "DETERMINISTIC_FAKE"
                        if self.current.snapshot()["mode"] == "SYNTHETIC"
                        else "CONFIGURED_PROVIDER",
                    )
                    == "DETERMINISTIC_FAKE"
                )
                self.current.control(action)
                if not self.worker or not self.worker.is_alive():
                    self.worker = threading.Thread(
                        target=self._execute, args=(self.current,), daemon=True
                    )
                    self.worker.start()
        else:
            self.current.control(action)
        return self.snapshot()

    def tick(self, stop: threading.Event | None = None) -> bool:
        if stop is not None and stop.is_set():
            return False
        try:
            with writer_lock(self.dispatch.ledger.root):
                return self._tick(stop)
        except OSError:
            return False

    def _tick(self, stop: threading.Event | None = None) -> bool:
        job = self.dispatch.claim()
        if job is None:
            return False
        try:
            self.select(job["program_id"])
            assert self.current is not None
            candidate = self.current
            state = candidate.snapshot()
            declaration = state["declaration"]
            if digest(declaration) != job["declaration_hash"]:
                raise PlatformError("ROOM_DECLARATION_REPLAY_CONFLICT")
            if state["status"] in {"PAUSED", "STOPPED", "COMPLETE", "FAILED"}:
                self.dispatch.finish(job, state["status"])
                return True
            candidate.routes = self._routes(
                declaration.get(
                    "provider_mode",
                    "DETERMINISTIC_FAKE"
                    if state["mode"] == "SYNTHETIC"
                    else "CONFIGURED_PROVIDER",
                )
                == "DETERMINISTIC_FAKE"
            )
            finished = threading.Event()

            def boundary() -> None:
                while not finished.wait(0.1):
                    if stop is not None and stop.is_set():
                        try:
                            if candidate.snapshot()["status"] == "READY_OR_RUNNING":
                                candidate.control("pause")
                        except (ValueError, OSError):
                            pass
                        return

            watcher = threading.Thread(target=boundary, daemon=True)
            watcher.start()
            try:
                self._execute(candidate)
            finally:
                finished.set()
                watcher.join(timeout=2)
            status = candidate.snapshot()["status"]
            self.dispatch.finish(
                job, "QUEUED" if status == "READY_OR_RUNNING" else status
            )
        except (ValueError, OSError):
            self.dispatch.finish(job, "FAILED")
        return True

    def snapshot(self) -> dict[str, Any]:
        # Worker liveness and the durable room state live in separate stores, so
        # a finishing worker can land between the two reads. Read liveness
        # first: the worst pairing is then "still dispatching" beside an already
        # terminal state, which only makes a waiting caller poll once more.
        # Reading liveness last paired a finished dispatch with a stale
        # non-terminal state, so /api/room could answer worker_running=false
        # alongside READY_OR_RUNNING and the offline console closure flaked.
        with self.lock:
            current = self.current
            running = bool(self.worker and self.worker.is_alive())
        # The room directory name is the program id it was declared with.
        dispatched = (
            self.dispatch.latest(current.root.name) if current is not None else None
        )
        value: dict[str, Any] = (
            current.snapshot()
            if current
            else {
                "status": "UNDECLARED",
                "history": [],
                "calls": [],
                "conclusion": "NO VALID ALPHA",
                "mode": "UNBOUND",
                "live_authority": "NONE",
            }
        )
        if self.external_worker:
            running = bool(dispatched and dispatched["status"] in {"QUEUED", "RUNNING"})
        return {
            **value,
            "memory": current.memory() if current else None,
            "worker_running": running,
            "worker_state": dispatched["status"] if dispatched else "NOT_DISPATCHED",
            "worker_mode": "EXTERNAL_DURABLE"
            if self.external_worker
            else "EMBEDDED_COMPATIBILITY",
            "datasets": self.datasets(),
            "programs": [p.name for p in sorted(self._programs())],
        }
