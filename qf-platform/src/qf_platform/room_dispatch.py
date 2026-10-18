"""Durable research-room dispatch; Lab owns all research checkpoints and effects."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

from .storage import Ledger, PlatformError

ACTIVE = {"QUEUED", "RUNNING"}


class RoomDispatch:
    def __init__(self, root: Path) -> None:
        directory = root / "room-dispatch"
        directory.mkdir(exist_ok=True)
        self.ledger = Ledger(directory)

    @staticmethod
    def _states(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        states = {}
        for row in history:
            states[row["job_id"]] = row
        return states

    def rows(self) -> list[dict[str, Any]]:
        return list(self._states(self.ledger.read()).values())

    def active(self, program: str | None = None) -> bool:
        return any(
            row["status"] in ACTIVE
            and (program is None or row["program_id"] == program)
            for row in self.rows()
        )

    def latest(self, program: str) -> dict[str, Any] | None:
        rows = [row for row in self.rows() if row["program_id"] == program]
        return max(rows, key=lambda row: row["seq"]) if rows else None

    def enqueue(self, program: str, declaration_hash: str) -> dict[str, Any]:
        if not re.fullmatch(r"room-[a-f0-9]{20}", program) or not re.fullmatch(
            r"[a-f0-9]{64}", declaration_hash
        ):
            raise PlatformError("ROOM_DISPATCH_IDENTITY_INVALID")
        with self.ledger.transaction() as db:
            active = [
                row
                for row in self._states(self.ledger.history(db)).values()
                if row["status"] in ACTIVE
            ]
            if active:
                if (
                    len(active) == 1
                    and active[0]["program_id"] == program
                    and active[0]["declaration_hash"] == declaration_hash
                ):
                    return active[0]
                raise PlatformError("ROOM_ALREADY_RUNNING")
            return self.ledger.append(
                db,
                kind="ROOM_DISPATCH",
                job_id="room-job-" + uuid.uuid4().hex,
                program_id=program,
                declaration_hash=declaration_hash,
                status="QUEUED",
                worker_pid=None,
                live_authority="NONE",
            )

    def claim(self) -> dict[str, Any] | None:
        with self.ledger.transaction() as db:
            pending = [
                row
                for row in self._states(self.ledger.history(db)).values()
                if row["status"] in ACTIVE
            ]
            if not pending:
                return None
            row = min(pending, key=lambda value: value["seq"])
            return self.ledger.append(
                db,
                kind="ROOM_DISPATCH",
                job_id=row["job_id"],
                program_id=row["program_id"],
                declaration_hash=row["declaration_hash"],
                status="RUNNING",
                worker_pid=os.getpid(),
                live_authority="NONE",
            )

    def finish(self, job: dict[str, Any], status: str) -> None:
        if status not in {"QUEUED", "PAUSED", "STOPPED", "FAILED", "COMPLETE"}:
            raise PlatformError("ROOM_DISPATCH_STATUS_INVALID")
        with self.ledger.transaction() as db:
            current = self._states(self.ledger.history(db)).get(job["job_id"])
            if current is None or current["seq"] != job["seq"]:
                raise PlatformError("ROOM_DISPATCH_COMMIT_CONFLICT")
            self.ledger.append(
                db,
                kind="ROOM_DISPATCH",
                job_id=job["job_id"],
                program_id=job["program_id"],
                declaration_hash=job["declaration_hash"],
                status=status,
                worker_pid=job["worker_pid"],
                live_authority="NONE",
            )
