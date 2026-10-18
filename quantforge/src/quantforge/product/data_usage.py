"""Append-only interval exposure and one-opening confirmation admission."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from quantforge.experiments import canonical_json
from quantforge.product.storage import local_root

Purpose = Literal["DEVELOPMENT", "SELECTION", "FINAL_CONFIRMATION"]
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9:_-]{1,80}\Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class DataWindow:
    instrument: str
    start: date
    end: date
    content_hash: str
    frequency: Literal["DAILY"] = "DAILY"
    cohort: str = "UNVERIFIED"

    def __post_init__(self) -> None:
        if (
            not _NAME.fullmatch(self.instrument)
            or type(self.start) is not date
            or type(self.end) is not date
            or self.start > self.end
            or not _SHA.fullmatch(self.content_hash)
            or self.frequency != "DAILY"
            or not _NAME.fullmatch(self.cohort)
        ):
            raise ValueError("canonical instrument interval and content hash required")

    def to_json(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


def _overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["instrument"] == right["instrument"]
        and left["frequency"] == right["frequency"]
        and left["start"] <= right["end"]
        and right["start"] <= left["end"]
    )


class DataUsageRegistry:
    def __init__(self, root: Path) -> None:
        self.root = local_root(root)
        self.path = self.root / "data-usage.sqlite"
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, "
                "payload TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL)"
            )
            for operation in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS no_{operation.lower()} "
                    f"BEFORE {operation} ON events BEGIN "
                    "SELECT RAISE(ABORT,'append-only'); END"
                )

    def connect(self) -> sqlite3.Connection:
        local_root(self.root)
        if self.path.is_symlink():
            raise ValueError("redirected usage registry")
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _history(db: sqlite3.Connection) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        before = "0" * 64
        for seq, raw, previous, checksum in db.execute(
            "SELECT seq,payload,previous,hash FROM events ORDER BY seq"
        ):
            value = json.loads(raw)
            if (
                seq != len(result) + 1
                or previous != before
                or raw != canonical_json(value)
                or _digest({"previous": previous, "payload": value}) != checksum
            ):
                raise ValueError("usage history integrity failed")
            result.append({**value, "seq": seq, "event_hash": checksum})
            before = checksum
        return result

    @classmethod
    def _append(cls, db: sqlite3.Connection, kind: str, **values: Any) -> None:
        rows = cls._history(db)
        previous = rows[-1]["event_hash"] if rows else "0" * 64
        value = {"kind": kind, "at": datetime.now(UTC).isoformat(), **values}
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?)",
            (
                len(rows) + 1,
                canonical_json(value),
                previous,
                _digest({"previous": previous, "payload": value}),
            ),
        )

    def history(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            return self._history(db)

    def record_use(
        self,
        window: DataWindow,
        *,
        purpose: Purpose,
        program: str,
        reference_hash: str,
    ) -> dict[str, Any]:
        if (
            purpose not in {"DEVELOPMENT", "SELECTION"}
            or not _NAME.fullmatch(program)
            or not _SHA.fullmatch(reference_hash)
        ):
            raise ValueError(
                "research use or separately reserved final access required"
            )
        payload: dict[str, Any] = {
            "window": window.to_json(),
            "purpose": purpose,
            "program": program,
            "reference_hash": reference_hash,
        }
        identity = _digest(payload)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._history(db)
            if any(row.get("identity") == identity for row in rows):
                return {"status": "ALREADY_RECORDED", "identity": identity}
            for row in rows:
                if row["kind"] == "FINAL_RESERVED" and _overlap(
                    row["window"], payload["window"]
                ):
                    raise ValueError("sealed final interval cannot enter development")
            self._append(db, "DATA_USED", identity=identity, **payload)
        return {"status": "RECORDED", "identity": identity}

    def reserve_final(
        self, window: DataWindow, *, program: str, protocol_hash: str
    ) -> str:
        if not _NAME.fullmatch(program) or not _SHA.fullmatch(protocol_hash):
            raise ValueError("frozen final protocol identity required")
        payload: dict[str, Any] = {
            "window": window.to_json(),
            "program": program,
            "protocol_hash": protocol_hash,
        }
        identity = _digest(payload)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._history(db)
            for row in rows:
                if row.get("identity") == identity:
                    return identity
                if row["kind"] in {
                    "DATA_USED",
                    "FINAL_RESERVED",
                    "FORWARD_OBSERVED",
                } and _overlap(row["window"], payload["window"]):
                    raise ValueError("prior exposure or final reservation overlaps")
            self._append(db, "FINAL_RESERVED", identity=identity, **payload)
        return identity

    def freeze_candidate(self, final_id: str, *, candidate_hash: str) -> None:
        if not _SHA.fullmatch(final_id) or not _SHA.fullmatch(candidate_hash):
            raise ValueError("candidate and final identity required")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._history(db)
            if not any(
                row["kind"] == "FINAL_RESERVED" and row["identity"] == final_id
                for row in rows
            ):
                raise ValueError("reserved final required")
            if any(
                row["kind"] == "FINAL_OPENED" and row["final_id"] == final_id
                for row in rows
            ):
                raise ValueError("final already spent")
            existing = [
                row
                for row in rows
                if row["kind"] == "CANDIDATE_FROZEN" and row["final_id"] == final_id
            ]
            if existing:
                if existing[0]["candidate_hash"] != candidate_hash:
                    raise ValueError("frozen candidate immutable")
                return
            self._append(
                db, "CANDIDATE_FROZEN", final_id=final_id, candidate_hash=candidate_hash
            )

    def open_final(self, final_id: str, *, candidate_hash: str) -> dict[str, Any]:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._history(db)
            if any(
                row["kind"] == "FINAL_OPENED" and row["final_id"] == final_id
                for row in rows
            ):
                raise ValueError("final confirmation opening already consumed")
            if not any(
                row["kind"] == "CANDIDATE_FROZEN"
                and row["final_id"] == final_id
                and row["candidate_hash"] == candidate_hash
                for row in rows
            ):
                raise ValueError("exact frozen candidate required")
            reservation = next(
                row
                for row in rows
                if row["kind"] == "FINAL_RESERVED" and row["identity"] == final_id
            )
            self._append(
                db, "FINAL_OPENED", final_id=final_id, candidate_hash=candidate_hash
            )
            return {
                "status": "ONE_OPENING_CONSUMED",
                "window": reservation["window"],
                "protocol_hash": reservation["protocol_hash"],
                "candidate_hash": candidate_hash,
                "independence_claim": "NOT_ESTABLISHED_BY_REGISTRY",
            }

    def declare_forward(self, *, program: str, protocol_hash: str) -> dict[str, Any]:
        if not _NAME.fullmatch(program) or not _SHA.fullmatch(protocol_hash):
            raise ValueError("frozen forward protocol required")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._history(db)
            existing = [
                row
                for row in rows
                if row["kind"] == "FORWARD_DECLARED" and row["program"] == program
            ]
            if existing:
                if existing[0]["protocol_hash"] != protocol_hash:
                    raise ValueError("forward protocol immutable")
                return dict(existing[0])
            starts_on = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
            self._append(
                db,
                "FORWARD_DECLARED",
                program=program,
                protocol_hash=protocol_hash,
                starts_on=starts_on,
            )
            return dict(self._history(db)[-1])

    def record_forward(
        self, window: DataWindow, *, program: str, source_captured_at: datetime
    ) -> dict[str, Any]:
        if source_captured_at.tzinfo is None or source_captured_at.utcoffset() is None:
            raise ValueError("aware observed source time required")
        now = datetime.now(UTC)
        if source_captured_at > now or window.end > now.date():
            raise ValueError("future observations cannot be fabricated")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._history(db)
            declaration = next(
                (
                    row
                    for row in rows
                    if row["kind"] == "FORWARD_DECLARED" and row["program"] == program
                ),
                None,
            )
            if (
                declaration is None
                or window.start.isoformat() < declaration["starts_on"]
            ):
                raise ValueError(
                    "historical rows are not declared forward observations"
                )
            if source_captured_at < datetime.fromisoformat(declaration["at"]):
                raise ValueError("source predates frozen forward declaration")
            if any(
                row["kind"] == "FORWARD_OBSERVED"
                and row["program"] == program
                and row["window"] == window.to_json()
                for row in rows
            ):
                return {"status": "ALREADY_OBSERVED", "elapsed_time_waived": False}
            if any(
                row["kind"] in {"DATA_USED", "FINAL_RESERVED", "FORWARD_OBSERVED"}
                and _overlap(row["window"], window.to_json())
                for row in rows
            ):
                raise ValueError("forward observation interval already exposed")
            self._append(
                db,
                "FORWARD_OBSERVED",
                program=program,
                window=window.to_json(),
                captured_at=source_captured_at.astimezone(UTC).isoformat(),
            )
        return {"status": "FORWARD_OBSERVED", "elapsed_time_waived": False}
