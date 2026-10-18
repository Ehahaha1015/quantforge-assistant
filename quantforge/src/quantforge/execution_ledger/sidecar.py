"""Append-only SQLite evidence store for the broker-neutral execution ledger.

The sidecar persists canonical external observation evidence only. It owns no
attempt, phase, assessment, cache, or derived table, and it never touches the
EXECSTATE database. Schema damage and row corruption fail closed without
repair or migration.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, Self

from ._canonical import canonical_instant
from .errors import (
    ExecutionLedgerBusyError,
    ExecutionLedgerCommitUnknownError,
    ExecutionLedgerCorruptionError,
    ExecutionLedgerDeniedError,
    ExecutionLedgerError,
    ExecutionLedgerPathError,
    ExecutionLedgerSchemaError,
    ReconciliationConflict,
)
from .identity import derive_attempt_id, observation_id_for, validate_attempt_id
from .models import (
    BrokerOrderObservation,
    ReconciliationAssessment,
    ReconciliationResult,
    observation_envelope_fields,
    observation_envelope_json,
    observation_from_envelope_json,
    observation_semantic_key,
)

__all__ = ["SQLiteObservationStore"]

#: Contract 9.2 extracted observation columns. Every one of them must be
#: verified against the canonical stored payload on open and before reads.
_EXTRACTED_COLUMN_NAMES: tuple[str, ...] = (
    "observation_id",
    "attempt_id",
    "submission_id",
    "target_json",
    "generation",
    "recovery_epoch",
    "observation_kind",
    "status",
    "broker_reference",
    "broker_message",
    "observed_at",
    "recorded_at",
)


def _extracted_column_matches(expected: object, actual: object) -> bool:
    if expected is None:
        return actual is None
    if isinstance(expected, int) and not isinstance(expected, bool):
        try:
            return int(str(actual)) == expected
        except (TypeError, ValueError):
            return False
    return isinstance(actual, str) and actual == expected


class _ObservationAssessor(Protocol):
    """Pure assessment callback evaluated inside the writer transaction."""

    def __call__(
        self, recorded: tuple[BrokerOrderObservation, ...]
    ) -> ReconciliationAssessment: ...

_SCHEMA_NAME = "quantforge.execution-ledger.sqlite"
_SCHEMA_VERSION = 1
_EXECSTATE_SCHEMA_NAME = "quantforge.execstate.sqlite"
_EXECSTATE_SCHEMA_VERSION = "2"
_EXECSTATE_SCHEMA_FINGERPRINT = (
    "21925c0fde36219ad76d2e18fbaae74beac8ad5aacf3694dcb34c884833875d1"
)
_MAX_PATH_CHARS = 4096

_SQLITE_HEADER = b"SQLite format 3\x00"

_RESERVED_WINDOWS_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

_SQL_MULTI_CHAR_TOKENS = (
    ">=",
    "<=",
    "<>",
    "!=",
    "==",
    "||",
    "<<",
    ">>",
)

_METADATA_DDL = """
    CREATE TABLE metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """

_METADATA_INSERT = """
    INSERT INTO metadata(key, value) VALUES
        ('schema_name', 'quantforge.execution-ledger.sqlite'),
        ('schema_version', '1'),
        ('binding_execstate_schema_name', 'quantforge.execstate.sqlite'),
        ('binding_execstate_schema_version', '2'),
        ('binding_execstate_schema_fingerprint',
         '21925c0fde36219ad76d2e18fbaae74beac8ad5aacf3694dcb34c884833875d1')
    """

_OBSERVATIONS_DDL = """
    CREATE TABLE observations (
        observation_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        observation_id TEXT NOT NULL UNIQUE,
        attempt_id TEXT NOT NULL,
        submission_id TEXT NOT NULL,
        target_json TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        recovery_epoch INTEGER NOT NULL CHECK (recovery_epoch >= 1),
        observation_kind TEXT NOT NULL CHECK (
            observation_kind IN ('BROKER_OBSERVATION', 'OBSERVATION_REPLAY')
        ),
        status TEXT NOT NULL CHECK (
            status IN ('ACCEPTED', 'REJECTED', 'UNKNOWN')
        ),
        broker_reference TEXT,
        broker_message TEXT,
        observed_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """

_INDEX_DDL = """
    CREATE INDEX observations_attempt_lookup
    ON observations(attempt_id, observation_sequence)
    """

_TRIGGER_UPDATE_DDL = """
    CREATE TRIGGER observations_no_update
    BEFORE UPDATE ON observations
    BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END
    """

_TRIGGER_DELETE_DDL = """
    CREATE TRIGGER observations_no_delete
    BEFORE DELETE ON observations
    BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END
    """

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    _METADATA_DDL,
    _METADATA_INSERT,
    _OBSERVATIONS_DDL,
    _INDEX_DDL,
    _TRIGGER_UPDATE_DDL,
    _TRIGGER_DELETE_DDL,
)

_EXPECTED_METADATA = {
    "schema_name": _SCHEMA_NAME,
    "schema_version": str(_SCHEMA_VERSION),
    "binding_execstate_schema_name": _EXECSTATE_SCHEMA_NAME,
    "binding_execstate_schema_version": _EXECSTATE_SCHEMA_VERSION,
    "binding_execstate_schema_fingerprint": _EXECSTATE_SCHEMA_FINGERPRINT,
}

_INDEX_NAME = "observations_attempt_lookup"
_TRIGGER_NAMES = ("observations_no_update", "observations_no_delete")


def _schema_sql_tokens(sql: Any) -> tuple[str, ...]:
    """Tokenize generated schema SQL without weakening quoted literals.

    Whitespace is not semantic. Quoted strings, identifiers, and operators
    are preserved exactly so a formatting relaxation cannot weaken
    verification.
    """

    if not isinstance(sql, str):
        return ()
    tokens: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if char in "'\"`":
            quote = char
            start = index
            index += 1
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise ValueError("unterminated quoted SQL token")
            tokens.append(sql[start:index])
            continue
        if char == "[":
            start = index
            closing = sql.find("]", index + 1)
            if closing < 0:
                raise ValueError("unterminated bracketed SQL identifier")
            tokens.append(sql[start : closing + 1])
            index = closing + 1
            continue
        if char.isalpha() or char == "_":
            start = index
            index += 1
            while index < length and (
                sql[index].isalnum() or sql[index] in "_$"
            ):
                index += 1
            tokens.append(sql[start:index].lower())
            continue
        if char.isdigit() or (
            char == "." and index + 1 < length and sql[index + 1].isdigit()
        ):
            start = index
            index += 1
            while index < length and (
                sql[index].isalnum() or sql[index] in ".+-"
            ):
                index += 1
            tokens.append(sql[start:index])
            continue
        matched = next(
            (
                token
                for token in _SQL_MULTI_CHAR_TOKENS
                if sql.startswith(token, index)
            ),
            None,
        )
        if matched is not None:
            tokens.append(matched)
            index += len(matched)
            continue
        tokens.append(char)
        index += 1
    return tuple(tokens)


def _expected_table_tokens() -> dict[str, tuple[str, ...]]:
    expected: dict[str, tuple[str, ...]] = {}
    for statement in _SCHEMA_STATEMENTS:
        tokens = _schema_sql_tokens(statement)
        if len(tokens) >= 3 and tokens[:2] == ("create", "table"):
            expected[tokens[2]] = tokens
    return expected


_EXPECTED_TABLE_TOKENS = _expected_table_tokens()
_EXPECTED_INDEX_TOKENS = _schema_sql_tokens(_INDEX_DDL)
_EXPECTED_TRIGGER_TOKENS = {
    "observations_no_update": _schema_sql_tokens(_TRIGGER_UPDATE_DDL),
    "observations_no_delete": _schema_sql_tokens(_TRIGGER_DELETE_DDL),
}


def _validate_database_path(raw_path: Any) -> Path:
    if isinstance(raw_path, bool) or not isinstance(
        raw_path, (str, os.PathLike)
    ):
        raise TypeError("path must be a str or pathlib.Path")
    raw = os.fspath(raw_path)
    if not isinstance(raw, str):
        raise TypeError("path must be a str or pathlib.Path")
    if "\x00" in raw:
        raise ExecutionLedgerPathError("ledger path contains NUL")
    if not raw or raw == ":memory:" or raw.lower().startswith("file:"):
        raise ExecutionLedgerPathError(
            "ledger path must be an explicit local file"
        )
    if len(raw) > _MAX_PATH_CHARS:
        raise ExecutionLedgerPathError("ledger path is too long")
    original = Path(raw)
    if not original.is_absolute():
        raise ExecutionLedgerPathError("ledger path must be absolute")
    if original.is_symlink():
        raise ExecutionLedgerPathError("ledger path must not be a symlink")
    for parent in original.parents:
        if parent.is_symlink() or (
            hasattr(parent, "is_junction") and parent.is_junction()
        ):
            raise ExecutionLedgerPathError(
                "ledger path must not traverse a symlink or junction"
            )
    try:
        resolved = original.resolve(strict=False)
    except OSError as exc:
        raise ExecutionLedgerPathError(
            "ledger path cannot be resolved"
        ) from exc
    if not resolved.is_absolute():
        raise ExecutionLedgerPathError("ledger path must be absolute")
    if str(resolved).startswith("\\\\") or str(resolved).startswith("//"):
        raise ExecutionLedgerPathError(
            "UNC/network ledger paths are not supported"
        )
    parent = resolved.parent
    if not parent.exists() or not parent.is_dir():
        raise ExecutionLedgerPathError(
            "ledger parent directory must already exist and be a directory"
        )
    if hasattr(original, "is_junction") and original.is_junction():
        raise ExecutionLedgerPathError("ledger path must not be a junction")
    drive, tail = os.path.splitdrive(str(resolved))
    if os.name == "nt":
        if not drive:
            raise ExecutionLedgerPathError(
                "ledger path must name a local drive"
            )
        if ":" in tail:
            raise ExecutionLedgerPathError(
                "alternate data stream paths are not supported"
            )
    for part in resolved.parts:
        stem = part.split(".", 1)[0].upper()
        if stem in _RESERVED_WINDOWS_NAMES:
            raise ExecutionLedgerPathError(
                "Windows reserved device names are not supported"
            )
    if resolved.exists():
        if not resolved.is_file():
            raise ExecutionLedgerPathError(
                "ledger path is not a regular file"
            )
        size = resolved.stat().st_size
        if size == 0:
            raise ExecutionLedgerCorruptionError(
                "existing zero-byte ledger fails closed"
            )
        with resolved.open("rb") as stream:
            header = stream.read(len(_SQLITE_HEADER))
        if header != _SQLITE_HEADER:
            raise ExecutionLedgerSchemaError(
                "existing ledger file is not a SQLite database"
            )
    return resolved


def _is_busy(exc: sqlite3.Error) -> bool:
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


class SQLiteObservationStore:
    """Append-only sidecar evidence store bound to one ledger database."""

    def __init__(self, path: str | Path) -> None:
        self._path = _validate_database_path(path)
        file_existed = self._path.exists()
        self._closed = False
        self._commit_outcome_unknown = False
        try:
            self._connection = sqlite3.connect(
                str(self._path),
                timeout=0.0,
                detect_types=0,
                isolation_level=None,
                check_same_thread=True,
                uri=False,
            )
        except sqlite3.Error as exc:
            raise ExecutionLedgerPathError(
                "ledger database could not be opened"
            ) from exc
        self._connection.row_factory = sqlite3.Row
        try:
            self._configure_connection()
            self._initialize_or_verify_schema(file_existed=file_existed)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()

    def append(
        self, observation: BrokerOrderObservation
    ) -> BrokerOrderObservation:
        """Append one observation inside an explicit write transaction."""

        stored, _assessment = self._write_observation(
            observation, assess=None
        )
        return stored

    def _append_assessed(
        self,
        observation: BrokerOrderObservation,
        assess: _ObservationAssessor,
    ) -> tuple[BrokerOrderObservation, ReconciliationAssessment]:
        """Assess and append one observation inside one writer transaction.

        The assessment that authorizes the append is computed from the
        authoritative sidecar rows read inside the same `BEGIN IMMEDIATE`
        transaction that performs the INSERT, so a concurrent writer can
        never slip opposing evidence between the assessment and the append.
        A conflicting assessment rolls back and appends nothing.

        Private: the contracted public store surface stays exactly
        `append` / `read_observations` / `close`.
        """

        if not callable(assess):
            raise TypeError("assess must be callable")
        stored, assessment = self._write_observation(
            observation, assess=assess
        )
        if assessment is None:  # pragma: no cover - defensive
            raise ExecutionLedgerCorruptionError(
                "assessed append produced no assessment"
            )
        return stored, assessment

    def _write_observation(
        self,
        observation: BrokerOrderObservation,
        *,
        assess: _ObservationAssessor | None,
    ) -> tuple[BrokerOrderObservation, ReconciliationAssessment | None]:
        self._require_open()
        if not isinstance(observation, BrokerOrderObservation):
            raise TypeError(
                "observation must be a BrokerOrderObservation"
            )
        try:
            self._begin_immediate()
            self._verify_schema()
            attempt_id = derive_attempt_id(
                observation.submission_id,
                observation.target_json,
                observation.generation,
                observation.recovery_epoch,
            )
            recorded = tuple(
                observation_from_envelope_json(str(row["payload_json"]))
                for row in self._connection.execute(
                    """
                    SELECT payload_json FROM observations
                    WHERE attempt_id = ?
                    ORDER BY observation_sequence
                    """,
                    (attempt_id,),
                ).fetchall()
            )
            semantic_key = observation_semantic_key(observation)
            observation_kind = "BROKER_OBSERVATION"
            for stored in recorded:
                if observation_semantic_key(stored) == semantic_key:
                    observation_kind = "OBSERVATION_REPLAY"
                    break
            assessment = None if assess is None else assess(recorded)
            if (
                assessment is not None
                and assessment.result is ReconciliationResult.CONFLICT
            ):
                self._rollback()
                raise ReconciliationConflict(
                    "observation conflicts with durable execution truth",
                    assessment=assessment,
                )
            sequence_row = self._connection.execute(
                "SELECT COALESCE(MAX(observation_sequence), 0) + 1 "
                "FROM observations"
            ).fetchone()
            sequence = int(sequence_row[0])
            observation_id = observation_id_for(sequence)
            fresh = replace(observation, observation_id=None)
            payload_json = observation_envelope_json(
                fresh,
                observation_id=observation_id,
                observation_kind=observation_kind,
            )
            observed_at = canonical_instant(observation.observed_at)
            cursor = self._connection.execute(
                """
                INSERT INTO observations(
                    observation_sequence, observation_id, attempt_id,
                    submission_id, target_json, generation, recovery_epoch,
                    observation_kind, status, broker_reference,
                    broker_message, observed_at, payload_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    observation_id,
                    attempt_id,
                    observation.submission_id,
                    observation.target_json,
                    observation.generation,
                    observation.recovery_epoch,
                    observation_kind,
                    observation.status.value,
                    observation.broker_reference,
                    observation.broker_message,
                    observed_at,
                    payload_json,
                    observed_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutionLedgerCorruptionError(
                    "observation append did not change exactly one row"
                )
            self._commit()
            return (
                replace(observation, observation_id=observation_id),
                assessment,
            )
        except ExecutionLedgerError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            if _is_busy(exc):
                raise ExecutionLedgerBusyError(
                    "ledger database is busy"
                ) from exc
            raise ExecutionLedgerCorruptionError(
                "observation append failed"
            ) from exc
        except BaseException:
            # Every path that leaves the transactional body before a
            # successful COMMIT must release the writer transaction, including
            # exceptions outside the ledger and sqlite families (for example
            # an EXECSTATE error raised by the assessment callback). Commit
            # uncertainty is a separate boundary: it is reported as
            # ExecutionLedgerCommitUnknownError and poisons the store, so no
            # rollback may pretend an uncertain COMMIT did not land.
            if not self._commit_outcome_unknown:
                self._rollback()
            raise

    def read_observations(
        self, attempt_id: str
    ) -> tuple[BrokerOrderObservation, ...]:
        """Read one attempt's evidence in deterministic sequence order."""

        self._require_open()
        validate_attempt_id(attempt_id)
        self._verify_schema()
        try:
            self._begin_read()
            rows = self._connection.execute(
                """
                SELECT payload_json FROM observations
                WHERE attempt_id = ?
                ORDER BY observation_sequence
                """,
                (attempt_id,),
            ).fetchall()
            result = tuple(
                observation_from_envelope_json(str(row["payload_json"]))
                for row in rows
            )
            self._commit()
            return result
        except ExecutionLedgerError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            if _is_busy(exc):
                raise ExecutionLedgerBusyError(
                    "ledger database is busy"
                ) from exc
            raise ExecutionLedgerCorruptionError(
                "observation read failed"
            ) from exc

    def _require_open(self) -> None:
        if self._closed:
            raise ExecutionLedgerDeniedError(
                "execution ledger store is closed"
            )
        if self._commit_outcome_unknown:
            raise ExecutionLedgerCommitUnknownError(
                "commit outcome is unknown; reopen the database"
            )

    def _configure_connection(self) -> None:
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            try:
                mode = self._connection.execute(
                    "PRAGMA journal_mode = WAL"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if _is_busy(exc):
                    raise ExecutionLedgerBusyError(
                        "database is busy during WAL configuration"
                    ) from exc
                raise
            if mode is None or str(mode[0]).lower() != "wal":
                raise ExecutionLedgerPathError(
                    "SQLite WAL mode is unavailable"
                )
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA busy_timeout = 0")
            self._connection.execute("PRAGMA locking_mode = NORMAL")
            journal_mode = self._connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()
            foreign_keys = self._connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()
            synchronous = self._connection.execute(
                "PRAGMA synchronous"
            ).fetchone()
            busy_timeout = self._connection.execute(
                "PRAGMA busy_timeout"
            ).fetchone()
            locking_mode = self._connection.execute(
                "PRAGMA locking_mode"
            ).fetchone()
            if journal_mode is None or str(journal_mode[0]).lower() != "wal":
                raise ExecutionLedgerPathError(
                    "SQLite WAL mode is unavailable"
                )
            if foreign_keys is None or int(foreign_keys[0]) != 1:
                raise ExecutionLedgerPathError(
                    "SQLite foreign keys are unavailable"
                )
            if synchronous is None or int(synchronous[0]) != 2:
                raise ExecutionLedgerPathError(
                    "SQLite synchronous=FULL is unavailable"
                )
            if busy_timeout is None or int(busy_timeout[0]) != 0:
                raise ExecutionLedgerPathError(
                    "SQLite busy_timeout is not zero"
                )
            if locking_mode is None or str(locking_mode[0]).lower() != "normal":
                raise ExecutionLedgerPathError(
                    "SQLite locking_mode is not NORMAL"
                )
        except ExecutionLedgerError:
            raise
        except sqlite3.Error as exc:
            raise ExecutionLedgerPathError(
                "SQLite connection configuration failed"
            ) from exc

    def _initialize_or_verify_schema(self, *, file_existed: bool) -> None:
        try:
            version = int(
                self._connection.execute("PRAGMA user_version").fetchone()[0]
            )
        except sqlite3.Error as exc:
            raise ExecutionLedgerSchemaError(
                "schema version cannot be read"
            ) from exc
        if version == 0:
            if file_existed:
                raise ExecutionLedgerSchemaError(
                    "existing database has no execution-ledger schema"
                )
            self._create_schema()
            return
        if version != _SCHEMA_VERSION:
            raise ExecutionLedgerSchemaError(
                f"unsupported execution-ledger schema version {version}"
            )
        self._verify_schema()

    def _create_schema(self) -> None:
        try:
            self._connection.execute("BEGIN EXCLUSIVE")
            for statement in _SCHEMA_STATEMENTS:
                self._connection.execute(statement)
            self._connection.execute(
                f"PRAGMA user_version = {_SCHEMA_VERSION}"
            )
            self._commit()
        except ExecutionLedgerCommitUnknownError:
            raise
        except sqlite3.Error as exc:
            self._rollback()
            raise ExecutionLedgerSchemaError(
                "execution-ledger schema initialization failed"
            ) from exc
        self._verify_schema()

    def _verify_schema(self) -> None:
        try:
            version_row = self._connection.execute(
                "PRAGMA user_version"
            ).fetchone()
            if version_row is None or int(version_row[0]) != _SCHEMA_VERSION:
                raise ExecutionLedgerSchemaError(
                    "unsupported execution-ledger schema version"
                )
            objects = self._connection.execute(
                "SELECT type, name, sql FROM sqlite_master"
            ).fetchall()
            tables = {
                str(row["name"])
                for row in objects
                if str(row["type"]) == "table"
                and str(row["name"]) != "sqlite_sequence"
            }
            if tables != set(_EXPECTED_TABLE_TOKENS):
                raise ExecutionLedgerSchemaError(
                    "execution-ledger table set does not match the contract"
                )
            table_sql = {
                str(row["name"]): _schema_sql_tokens(row["sql"])
                for row in objects
                if str(row["type"]) == "table"
                and str(row["name"]) != "sqlite_sequence"
            }
            for name, expected in _EXPECTED_TABLE_TOKENS.items():
                if table_sql.get(name) != expected:
                    raise ExecutionLedgerSchemaError(
                        f"invalid execution-ledger table definition: {name}"
                    )
            indexes = {
                str(row["name"]): _schema_sql_tokens(row["sql"])
                for row in objects
                if str(row["type"]) == "index" and row["sql"] is not None
            }
            if set(indexes) != {_INDEX_NAME}:
                raise ExecutionLedgerSchemaError(
                    "execution-ledger index set does not match the contract"
                )
            if indexes[_INDEX_NAME] != _EXPECTED_INDEX_TOKENS:
                raise ExecutionLedgerSchemaError(
                    "invalid execution-ledger index definition"
                )
            triggers = {
                str(row["name"]): _schema_sql_tokens(row["sql"])
                for row in objects
                if str(row["type"]) == "trigger"
            }
            if set(triggers) != set(_TRIGGER_NAMES):
                raise ExecutionLedgerSchemaError(
                    "execution-ledger trigger set does not match the contract"
                )
            for name, expected in _EXPECTED_TRIGGER_TOKENS.items():
                if triggers.get(name) != expected:
                    raise ExecutionLedgerSchemaError(
                        f"invalid execution-ledger trigger: {name}"
                    )
            metadata = {
                str(row["key"]): str(row["value"])
                for row in self._connection.execute(
                    "SELECT key, value FROM metadata"
                ).fetchall()
            }
            if metadata != _EXPECTED_METADATA:
                raise ExecutionLedgerSchemaError(
                    "execution-ledger metadata mismatch"
                )
            integrity = self._connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise ExecutionLedgerCorruptionError(
                    "execution-ledger integrity check failed"
                )
            if self._connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall():
                raise ExecutionLedgerCorruptionError(
                    "execution-ledger foreign key check failed"
                )
            self._verify_rows()
        except (
            ExecutionLedgerSchemaError,
            ExecutionLedgerCorruptionError,
        ):
            raise
        except sqlite3.Error as exc:
            raise ExecutionLedgerSchemaError(
                "execution-ledger schema cannot be verified"
            ) from exc

    def _verify_rows(self) -> None:
        try:
            rows = self._connection.execute(
                "SELECT * FROM observations ORDER BY observation_sequence"
            ).fetchall()
            semantic_keys: dict[str, set[str]] = {}
            for row in rows:
                sequence = int(row["observation_sequence"])
                if sequence < 1:
                    raise ExecutionLedgerCorruptionError(
                        "stored observation sequence is not positive"
                    )
                observation_id = observation_id_for(sequence)
                if str(row["observation_id"]) != observation_id:
                    raise ExecutionLedgerCorruptionError(
                        "stored observation_id contradicts its sequence"
                    )
                observation, observation_kind = (
                    observation_envelope_fields(str(row["payload_json"]))
                )
                if observation.observation_id != observation_id:
                    raise ExecutionLedgerCorruptionError(
                        "stored observation_id contradicts its payload"
                    )
                attempt_id = derive_attempt_id(
                    observation.submission_id,
                    observation.target_json,
                    observation.generation,
                    observation.recovery_epoch,
                )
                observed_at = canonical_instant(observation.observed_at)
                expected_columns: dict[str, object] = {
                    "observation_id": observation_id,
                    "attempt_id": attempt_id,
                    "submission_id": observation.submission_id,
                    "target_json": observation.target_json,
                    "generation": observation.generation,
                    "recovery_epoch": observation.recovery_epoch,
                    "observation_kind": observation_kind,
                    "status": observation.status.value,
                    "broker_reference": observation.broker_reference,
                    "broker_message": observation.broker_message,
                    "observed_at": observed_at,
                    "recorded_at": observed_at,
                }
                if set(expected_columns) != set(_EXTRACTED_COLUMN_NAMES):
                    raise ExecutionLedgerCorruptionError(
                        "extracted-column verification matrix is incomplete"
                    )
                for column in _EXTRACTED_COLUMN_NAMES:
                    if not _extracted_column_matches(
                        expected_columns[column], row[column]
                    ):
                        raise ExecutionLedgerCorruptionError(
                            f"stored {column} contradicts its payload"
                        )
                semantic_key = observation_semantic_key(observation)
                seen = semantic_keys.setdefault(attempt_id, set())
                if (
                    observation_kind == "OBSERVATION_REPLAY"
                    and semantic_key not in seen
                ):
                    raise ExecutionLedgerCorruptionError(
                        "stored replay has no original observation"
                    )
                seen.add(semantic_key)
            sequence_row = self._connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'observations'"
            ).fetchone()
            maximum = max(
                (int(row["observation_sequence"]) for row in rows),
                default=0,
            )
            if sequence_row is not None and int(sequence_row[0]) < maximum:
                raise ExecutionLedgerCorruptionError(
                    "sqlite_sequence is behind the stored observations"
                )
        except (
            ExecutionLedgerSchemaError,
            ExecutionLedgerCorruptionError,
        ):
            raise
        except (TypeError, ValueError, ExecutionLedgerDeniedError) as exc:
            raise ExecutionLedgerCorruptionError(
                "stored observation is invalid"
            ) from exc

    def _begin_immediate(self) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise ExecutionLedgerBusyError(
                    "database is busy"
                ) from exc
            raise ExecutionLedgerCorruptionError(
                "could not begin a durable transaction"
            ) from exc

    def _begin_read(self) -> None:
        try:
            self._connection.execute("BEGIN")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise ExecutionLedgerBusyError(
                    "database is busy"
                ) from exc
            raise ExecutionLedgerCorruptionError(
                "could not begin a read transaction"
            ) from exc

    def _commit(self) -> None:
        try:
            self._connection.commit()
        except BaseException as exc:
            self._commit_outcome_unknown = True
            raise ExecutionLedgerCommitUnknownError(
                "commit outcome is unknown"
            ) from exc
        else:
            self._commit_outcome_unknown = False

    def _rollback(self) -> None:
        if self._commit_outcome_unknown:
            return
        try:
            self._connection.rollback()
        except sqlite3.Error:
            pass
