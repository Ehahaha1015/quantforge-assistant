"""Durable authenticated OPERATOR-001 control store.

The store is deliberately local, deterministic and transport-free.  It owns
one SQLite database whose append-only audit head is the authoritative control
truth and whose control_state table is a verified projection of that head.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, cast

from ..frontier import (
    ExecutionTargetBinding,
    OperatorExecutionEnableEvidence,
)
from .canonical import (
    canonical_instant,
    canonical_json,
    commitment,
    duration_microseconds,
    microseconds_duration,
    normalize_sql,
    opaque,
    parse_canonical_instant,
)
from .errors import (
    OperatorArgumentError,
    OperatorAuthenticationError,
    OperatorBusyError,
    OperatorClosedError,
    OperatorCommitUnknownError,
    OperatorConflictError,
    OperatorCorruptionError,
    OperatorError,
    OperatorNotFoundError,
    OperatorPathError,
    OperatorSchemaError,
    OperatorSequenceExhaustedError,
    OperatorTargetMismatchError,
    OperatorUnsupportedSchemaError,
)
from .identity import (
    action_id,
    action_payload,
    audit_id,
    evidence_id,
    request_commitment,
    scope_key,
    scope_payload,
    state_version_id,
)
from .models import (
    ControlAction,
    ControlState,
    OperatorAuthenticationProof,
    OperatorControlAuditEvent,
    OperatorControlOutcome,
    OperatorControlPolicy,
    OperatorControlRequest,
    OperatorControlScope,
    OperatorControlState,
    VerifiedOperatorIdentity,
)
from .verifier import OperatorVerifier, RejectingOperatorVerifier

_SCHEMA_NAME = "quantforge.operator.sqlite"
_SCHEMA_VERSION = "1"
_USER_VERSION = 1
_CONTROL_SEQUENCE_MAX = 9_223_372_036_854_775_807
_REQUEST_SCHEMA = "operator-request-v1"
_SCOPE_SCHEMA = "operator-scope-v1"
_PROOF_SCHEMA = "operator-proof-v1"
_TARGET_SCHEMA = "frontier-target-v1"
_DEFAULT_VERIFIER = RejectingOperatorVerifier()
_Environment = Literal["PAPER", "SANDBOX", "PRODUCTION"]
_MISSING = object()


def _environment(value: Any) -> _Environment:
    text = str(value)
    if text not in ("PAPER", "SANDBOX", "PRODUCTION"):
        raise OperatorCorruptionError("stored environment is malformed")
    return cast(_Environment, text)


def _stored_int(value: Any, *, name: str) -> int:
    """Section 20 durable INTEGER decode: an exact int, never a bool or float.

    SQLite INTEGER affinity still admits REAL values that cannot be losslessly
    converted (for example ``float('inf')``), so the raw column value must be
    type-checked before any arithmetic or comparison is attempted.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise OperatorCorruptionError(f"stored {name} is not an integer")
    return value


def _required_field(value: Any, name: str) -> Any:
    """Fetch a required boundary field without leaking ``AttributeError``."""

    field = getattr(value, name, _MISSING)
    if field is _MISSING:
        raise OperatorArgumentError(f"{name} is missing")
    return field


def _validate_scope_shape(scope: Any) -> None:
    """Section 9 scope grammars, total over forged or field-less objects."""

    if not isinstance(scope, OperatorControlScope):
        raise OperatorArgumentError("scope must be an OperatorControlScope")
    if getattr(scope, "schema", None) != _SCOPE_SCHEMA:
        raise OperatorArgumentError("scope schema is malformed")
    opaque(_required_field(scope, "broker"), name="broker")
    opaque(_required_field(scope, "account_reference"), name="account_reference")
    opaque(_required_field(scope, "adapter_id"), name="adapter_id")
    opaque(_required_field(scope, "adapter_version"), name="adapter_version")
    if _required_field(scope, "environment") not in (
        "PAPER",
        "SANDBOX",
        "PRODUCTION",
    ):
        raise OperatorArgumentError("environment is malformed")


def _validate_target_shape(target: Any) -> None:
    """Read-only validation of the frozen Frontier target at the OPERATOR seam."""

    if not isinstance(target, ExecutionTargetBinding):
        raise OperatorArgumentError("target must be an ExecutionTargetBinding")
    if getattr(target, "schema", None) != _TARGET_SCHEMA:
        raise OperatorArgumentError("target schema is malformed")
    opaque(_required_field(target, "broker"), name="broker")
    opaque(_required_field(target, "account_reference"), name="account_reference")
    opaque(_required_field(target, "adapter_id"), name="adapter_id")
    opaque(_required_field(target, "adapter_version"), name="adapter_version")
    if _required_field(target, "environment") not in (
        "PAPER",
        "SANDBOX",
        "PRODUCTION",
    ):
        raise OperatorArgumentError("environment is malformed")
    submission_id_value = _required_field(target, "submission_id")
    if not isinstance(submission_id_value, str) or not submission_id_value:
        raise OperatorArgumentError("submission_id is malformed")

_CREATE_METADATA = """CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)"""

_CREATE_CONTROL_STATE = """CREATE TABLE control_state (
    scope_key TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('PAPER','SANDBOX','PRODUCTION')),
    account_reference TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ENABLED','DISABLED')),
    control_sequence INTEGER NOT NULL UNIQUE CHECK (control_sequence >= 1),
    action_id TEXT NOT NULL,
    state_version_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    maximum_age_microseconds INTEGER NOT NULL CHECK (maximum_age_microseconds >= 1)
)"""

_CREATE_CONTROL_AUDIT = """CREATE TABLE control_audit (
    control_sequence INTEGER PRIMARY KEY CHECK (control_sequence >= 1),
    audit_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL UNIQUE,
    action_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    broker TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('PAPER','SANDBOX','PRODUCTION')),
    account_reference TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    requested_action TEXT NOT NULL CHECK (requested_action IN ('ENABLE','DISABLE')),
    resulting_state TEXT NOT NULL CHECK (resulting_state IN ('ENABLED','DISABLED')),
    state_version_id TEXT NOT NULL,
    operator_reference TEXT NOT NULL,
    control_source TEXT NOT NULL,
    verifier_id TEXT NOT NULL,
    proof_id TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    maximum_age_microseconds INTEGER NOT NULL CHECK (maximum_age_microseconds >= 1),
    committed_at TEXT NOT NULL,
    request_commitment TEXT NOT NULL
)"""

_CREATE_CONTROL_AUDIT_INDEX = """CREATE INDEX control_audit_scope_sequence
    ON control_audit (scope_key, control_sequence)"""

_CREATE_CONTROL_AUDIT_NO_UPDATE = """CREATE TRIGGER control_audit_no_update
BEFORE UPDATE ON control_audit
BEGIN
    SELECT RAISE(ABORT, 'control_audit is append-only');
END"""

_CREATE_CONTROL_AUDIT_NO_DELETE = """CREATE TRIGGER control_audit_no_delete
BEFORE DELETE ON control_audit
BEGIN
    SELECT RAISE(ABORT, 'control_audit is append-only');
END"""

_DDL = (
    _CREATE_METADATA,
    _CREATE_CONTROL_STATE,
    _CREATE_CONTROL_AUDIT,
    _CREATE_CONTROL_AUDIT_INDEX,
    _CREATE_CONTROL_AUDIT_NO_UPDATE,
    _CREATE_CONTROL_AUDIT_NO_DELETE,
)

_EXPECTED_SQL_OBJECTS: dict[str, tuple[str, str, str]] = {
    "metadata": ("table", "metadata", _CREATE_METADATA),
    "control_state": ("table", "control_state", _CREATE_CONTROL_STATE),
    "control_audit": ("table", "control_audit", _CREATE_CONTROL_AUDIT),
    "control_audit_scope_sequence": (
        "index",
        "control_audit",
        _CREATE_CONTROL_AUDIT_INDEX,
    ),
    "control_audit_no_update": (
        "trigger",
        "control_audit",
        _CREATE_CONTROL_AUDIT_NO_UPDATE,
    ),
    "control_audit_no_delete": (
        "trigger",
        "control_audit",
        _CREATE_CONTROL_AUDIT_NO_DELETE,
    ),
}

_EXPECTED_AUTOINDEXES: dict[str, str] = {
    "sqlite_autoindex_metadata_1": "metadata",
    "sqlite_autoindex_control_state_1": "control_state",
    "sqlite_autoindex_control_state_2": "control_state",
    "sqlite_autoindex_control_audit_1": "control_audit",
    "sqlite_autoindex_control_audit_2": "control_audit",
}

_EXPECTED_METADATA = (
    ("schema_name", _SCHEMA_NAME),
    ("schema_version", _SCHEMA_VERSION),
)


class SQLiteOperatorControlStore:
    """One local SQLite-backed authenticated operator control source."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        policy: OperatorControlPolicy | None = None,
        verifier: OperatorVerifier = _DEFAULT_VERIFIER,
    ) -> None:
        if not isinstance(policy, OperatorControlPolicy):
            raise OperatorArgumentError("policy is required")
        if not hasattr(verifier, "verifier_id") or not callable(
            getattr(verifier, "verify", None)
        ):
            raise OperatorArgumentError("verifier must implement OperatorVerifier")

        self._policy = policy
        self._verifier = verifier
        self._connection: sqlite3.Connection | None = None
        self._poisoned = False
        self._closed = False
        self._resolved_path: Path | None = None

        resolved, fresh = self._validate_path(path)
        self._resolved_path = resolved
        try:
            self._open(resolved, fresh)
        except OperatorCommitUnknownError:
            raise
        except OperatorSchemaError:
            self._close_connection()
            raise
        except OperatorUnsupportedSchemaError:
            self._close_connection()
            raise
        except OperatorPathError:
            self._close_connection()
            raise
        except OperatorCorruptionError:
            self._close_connection()
            raise
        except sqlite3.Error as exc:
            self._close_connection()
            raise self._map_sqlite_error(exc) from exc
        except OSError as exc:
            self._close_connection()
            raise OperatorPathError("store path is not openable") from exc
        except BaseException:
            self._close_connection()
            raise

    # ------------------------------------------------------------------
    # Path and open
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_path(path: str | os.PathLike[str]) -> tuple[Path, bool]:
        try:
            raw = os.fspath(path)
        except TypeError as exc:
            raise OperatorPathError("store path is not a filesystem path") from exc
        if not isinstance(raw, str) or not raw:
            raise OperatorPathError("store path is not a nonempty string")
        if "://" in raw:
            raise OperatorPathError("store path must not be a URI")
        if raw.startswith(("\\\\", "//")):
            raise OperatorPathError("store path must not be a UNC path")
        if not os.path.isabs(raw):
            raise OperatorPathError("store path must be absolute")
        candidate = Path(raw)
        if candidate.is_symlink():
            raise OperatorPathError("store path must not be a symlink")
        is_junction = getattr(candidate, "is_junction", None)
        if callable(is_junction) and is_junction():
            raise OperatorPathError("store path must not be a junction")
        if candidate.exists():
            if candidate.is_dir():
                raise OperatorPathError("store path must not be a directory")
            try:
                metadata = candidate.stat()
            except OSError as exc:
                raise OperatorPathError("store path is not statable") from exc
            mode = metadata.st_mode
            if (
                stat.S_ISCHR(mode)
                or stat.S_ISBLK(mode)
                or stat.S_ISFIFO(mode)
                or stat.S_ISSOCK(mode)
            ):
                raise OperatorPathError("store path must not be a device path")
            if not stat.S_ISREG(mode):
                raise OperatorPathError("store path must be a regular file")
            if metadata.st_size == 0:
                raise OperatorPathError("existing zero-byte store is rejected")
            fresh = False
        else:
            parent = candidate.parent
            if not parent.exists() or not parent.is_dir():
                raise OperatorPathError("store parent directory must exist")
            fresh = True
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise OperatorPathError("store path cannot be resolved") from exc
        return resolved, fresh

    def _open(self, path: Path, fresh: bool) -> None:
        try:
            connection = sqlite3.connect(str(path), isolation_level=None)
        except sqlite3.Error as exc:
            raise OperatorPathError("store could not be opened") from exc
        connection.row_factory = sqlite3.Row
        self._connection = connection
        self._apply_pragmas()
        if fresh:
            self._initialize_fresh()
        else:
            self._verify_existing()

    def _apply_pragmas(self) -> None:
        connection = self._require_connection()
        try:
            journal = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 0")
            connection.execute("PRAGMA locking_mode = NORMAL")
            synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            locking = connection.execute("PRAGMA locking_mode").fetchone()[0]
        except sqlite3.Error as exc:
            raise OperatorPathError("store pragmas are not applicable") from exc
        if str(journal).lower() != "wal":
            raise OperatorSchemaError("journal_mode is not WAL")
        if int(synchronous) != 2:
            raise OperatorSchemaError("synchronous is not FULL")
        if int(foreign_keys) != 1:
            raise OperatorSchemaError("foreign_keys is not ON")
        if int(busy_timeout) != 0:
            raise OperatorSchemaError("busy_timeout is not zero")
        if str(locking).lower() != "normal":
            raise OperatorSchemaError("locking_mode is not NORMAL")

    def _initialize_fresh(self) -> None:
        connection = self._require_connection()
        self._begin_immediate()
        try:
            connection.execute(f"PRAGMA user_version = {_USER_VERSION}")
            for statement in _DDL:
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                _EXPECTED_METADATA,
            )
            self._verify_all(full=True)
            self._commit()
        except OperatorCommitUnknownError:
            raise
        except BaseException:
            self._rollback_if_open()
            raise

    def _verify_existing(self) -> None:
        connection = self._require_connection()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if int(version) != _USER_VERSION:
            raise OperatorUnsupportedSchemaError("unsupported operator user_version")
        self._verify_all(full=True)

    # ------------------------------------------------------------------
    # Schema and durable verification
    # ------------------------------------------------------------------

    def _verify_all(self, *, full: bool) -> None:
        # V4/V6/V7 are cheap relative to the durable truth they protect, and
        # section 20 allows the targeted read path to re-run them.  Verifying
        # unconditionally is the conservative reading: an object added, a
        # trigger dropped or an integrity fault introduced after open must
        # never be served as durable truth.
        self._verify_schema()
        self._verify_metadata()
        self._verify_integrity()
        self._verify_foreign_keys()
        self._verify_append_only_triggers()
        audit = self._load_audit_events()
        states = self._load_state_rows()
        self._verify_sequences(audit)
        self._verify_projection(audit, states)

    def _verify_append_only_triggers(self) -> None:
        """Contract V8: both append-only triggers are re-checked every time."""

        connection = self._require_connection()
        rows = connection.execute(
            "SELECT name, type, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name IN (?, ?)",
            ("control_audit_no_update", "control_audit_no_delete"),
        ).fetchall()
        found: set[str] = set()
        for row in rows:
            name = str(row["name"])
            expected = _EXPECTED_SQL_OBJECTS.get(name)
            if (
                expected is None
                or str(row["type"]) != "trigger"
                or str(row["tbl_name"]) != "control_audit"
                or not isinstance(row["sql"], str)
                or normalize_sql(str(row["sql"])) != normalize_sql(expected[2])
            ):
                raise OperatorCorruptionError(
                    "append-only trigger body is not intact"
                )
            found.add(name)
        if found != {"control_audit_no_update", "control_audit_no_delete"}:
            raise OperatorCorruptionError("append-only trigger is missing")

    def _verify_schema(self) -> None:
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
        ).fetchall()
        seen_sql: set[str] = set()
        seen_auto: set[str] = set()
        for row in rows:
            object_type = str(row["type"])
            name = str(row["name"])
            table_name = str(row["tbl_name"])
            sql = row["sql"]
            if object_type == "index" and sql is None:
                expected_table = _EXPECTED_AUTOINDEXES.get(name)
                if expected_table is None or table_name != expected_table:
                    raise OperatorSchemaError("unexpected automatic index")
                seen_auto.add(name)
                continue
            expected = _EXPECTED_SQL_OBJECTS.get(name)
            if expected is None:
                raise OperatorSchemaError("unexpected sqlite_master object")
            expected_type, expected_table, expected_sql = expected
            if (
                object_type != expected_type
                or table_name != expected_table
                or not isinstance(sql, str)
            ):
                raise OperatorSchemaError("sqlite_master identity mismatch")
            if normalize_sql(sql) != normalize_sql(expected_sql):
                raise OperatorSchemaError("sqlite_master SQL mismatch")
            seen_sql.add(name)
        if seen_sql != set(_EXPECTED_SQL_OBJECTS):
            raise OperatorSchemaError("missing sqlite_master object")
        if seen_auto != set(_EXPECTED_AUTOINDEXES):
            raise OperatorSchemaError("missing automatic index")

    def _verify_metadata(self) -> None:
        connection = self._require_connection()
        rows = tuple(
            (str(row["key"]), str(row["value"]))
            for row in connection.execute(
                "SELECT key, value FROM metadata ORDER BY key"
            ).fetchall()
        )
        if rows != _EXPECTED_METADATA:
            raise OperatorSchemaError("metadata rows do not match the schema")

    def _verify_integrity(self) -> None:
        connection = self._require_connection()
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        if len(rows) != 1 or str(rows[0][0]).lower() != "ok":
            raise OperatorCorruptionError("database integrity check failed")

    def _verify_foreign_keys(self) -> None:
        connection = self._require_connection()
        rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        if rows:
            raise OperatorCorruptionError("foreign key check failed")

    def _load_audit_events(self) -> tuple[OperatorControlAuditEvent, ...]:
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT * FROM control_audit ORDER BY control_sequence"
        ).fetchall()
        events: list[OperatorControlAuditEvent] = []
        for row in rows:
            events.append(self._audit_event_from_row(row))
        return tuple(events)

    def _load_state_rows(self) -> tuple[sqlite3.Row, ...]:
        connection = self._require_connection()
        return tuple(
            connection.execute(
                "SELECT * FROM control_state ORDER BY scope_key"
            ).fetchall()
        )

    def _audit_event_from_row(
        self, row: sqlite3.Row
    ) -> OperatorControlAuditEvent:
        try:
            return self._decode_audit_event(row)
        except OperatorCorruptionError:
            raise
        except (OperatorArgumentError, ValueError, TypeError) as exc:
            raise OperatorCorruptionError(
                "stored control_audit row is malformed"
            ) from exc

    @staticmethod
    def _decode_audit_event(
        row: sqlite3.Row,
    ) -> OperatorControlAuditEvent:
        scope = OperatorControlScope(
            schema="operator-scope-v1",
            broker=str(row["broker"]),
            environment=_environment(row["environment"]),
            account_reference=str(row["account_reference"]),
            adapter_id=str(row["adapter_id"]),
            adapter_version=str(row["adapter_version"]),
        )
        maximum_age = microseconds_duration(
            row["maximum_age_microseconds"], name="maximum_age_microseconds"
        )
        event = OperatorControlAuditEvent(
            control_sequence=_stored_int(
                row["control_sequence"], name="control_sequence"
            ),
            audit_id=str(row["audit_id"]),
            request_id=str(row["request_id"]),
            action_id=str(row["action_id"]),
            scope_key=str(row["scope_key"]),
            broker=scope.broker,
            environment=scope.environment,
            account_reference=scope.account_reference,
            adapter_id=scope.adapter_id,
            adapter_version=scope.adapter_version,
            requested_action=ControlAction(str(row["requested_action"])),
            resulting_state=ControlState(str(row["resulting_state"])),
            state_version_id=str(row["state_version_id"]),
            operator_reference=str(row["operator_reference"]),
            control_source=str(row["control_source"]),
            verifier_id=str(row["verifier_id"]),
            proof_id=str(row["proof_id"]),
            verified_at=parse_canonical_instant(
                row["verified_at"], name="verified_at"
            ),
            observed_at=parse_canonical_instant(
                row["observed_at"], name="observed_at"
            ),
            maximum_age=maximum_age,
            committed_at=parse_canonical_instant(
                row["committed_at"], name="committed_at"
            ),
            request_commitment=commitment(row["request_commitment"]),
        )
        expected_scope_key = scope_key(scope)
        if event.scope_key != expected_scope_key:
            raise OperatorCorruptionError("audit scope_key does not re-derive")
        expected_action_id = action_id(
            OperatorControlRequest(
                schema="operator-request-v1",
                request_id=event.request_id,
                action=event.requested_action,
                scope=scope,
                operator_reference=event.operator_reference,
                control_source=event.control_source,
                observed_at=event.observed_at,
                maximum_age=event.maximum_age,
            )
        )
        if event.action_id != expected_action_id:
            raise OperatorCorruptionError("audit action_id does not re-derive")
        expected_state_version_id = state_version_id(
            action_id_value=event.action_id,
            scope=scope,
            state=event.resulting_state,
            observed_at=event.observed_at,
            maximum_age=event.maximum_age,
        )
        if event.state_version_id != expected_state_version_id:
            raise OperatorCorruptionError(
                "audit state_version_id does not re-derive"
            )
        expected_commitment = request_commitment(
            OperatorControlRequest(
                schema="operator-request-v1",
                request_id=event.request_id,
                action=event.requested_action,
                scope=scope,
                operator_reference=event.operator_reference,
                control_source=event.control_source,
                observed_at=event.observed_at,
                maximum_age=event.maximum_age,
            )
        )
        if event.request_commitment != expected_commitment:
            raise OperatorCorruptionError(
                "audit request_commitment does not re-derive"
            )
        expected_audit_id = audit_id(
            control_sequence=event.control_sequence,
            action_id_value=event.action_id,
            request_id=event.request_id,
            scope=scope,
            requested_action=event.requested_action,
            resulting_state=event.resulting_state,
            state_version_id_value=event.state_version_id,
            operator_reference=event.operator_reference,
            control_source=event.control_source,
            verifier_id=event.verifier_id,
            proof_id=event.proof_id,
            verified_at=event.verified_at,
            observed_at=event.observed_at,
            maximum_age=event.maximum_age,
            committed_at=event.committed_at,
            request_commitment=event.request_commitment,
        )
        if event.audit_id != expected_audit_id:
            raise OperatorCorruptionError("audit_id does not re-derive")
        if event.observed_at > event.verified_at:
            raise OperatorCorruptionError(
                "audit observed_at is later than verified_at"
            )
        return event

    @staticmethod
    def _verify_sequences(events: tuple[OperatorControlAuditEvent, ...]) -> None:
        expected = list(range(1, len(events) + 1))
        actual = [event.control_sequence for event in events]
        if actual != expected:
            raise OperatorCorruptionError("control sequence is not contiguous")
        if actual and actual[-1] > _CONTROL_SEQUENCE_MAX:
            raise OperatorCorruptionError("control sequence exceeds INT64_MAX")

    def _verify_projection(
        self,
        events: tuple[OperatorControlAuditEvent, ...],
        state_rows: tuple[sqlite3.Row, ...],
    ) -> None:
        heads: dict[str, OperatorControlAuditEvent] = {}
        for event in events:
            current = heads.get(event.scope_key)
            if current is None or event.control_sequence > current.control_sequence:
                heads[event.scope_key] = event
        projection: dict[str, sqlite3.Row] = {}
        for row in state_rows:
            scope_key_value = str(row["scope_key"])
            if scope_key_value in projection:
                raise OperatorCorruptionError("duplicate control_state scope")
            projection[scope_key_value] = row
        if set(projection) != set(heads):
            raise OperatorCorruptionError("control_state/audit scope set mismatch")
        for scope_key_value, head in heads.items():
            row = projection[scope_key_value]
            try:
                mismatch = (
                    str(row["state"]) != head.resulting_state.value
                    or _stored_int(
                        row["control_sequence"], name="control_sequence"
                    )
                    != head.control_sequence
                    or str(row["action_id"]) != head.action_id
                    or str(row["state_version_id"]) != head.state_version_id
                    or str(row["observed_at"])
                    != canonical_instant(head.observed_at, name="observed_at")
                    or _stored_int(
                        row["maximum_age_microseconds"],
                        name="maximum_age_microseconds",
                    )
                    != duration_microseconds(head.maximum_age, name="maximum_age")
                    or str(row["broker"]) != head.broker
                    or str(row["environment"]) != head.environment
                    or str(row["account_reference"]) != head.account_reference
                    or str(row["adapter_id"]) != head.adapter_id
                    or str(row["adapter_version"]) != head.adapter_version
                )
            except (
                ArithmeticError,
                ValueError,
                TypeError,
                OperatorArgumentError,
            ) as exc:
                raise OperatorCorruptionError(
                    "stored control_state row is malformed"
                ) from exc
            if mismatch:
                raise OperatorCorruptionError(
                    "control_state is not the audit head projection"
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_control_change(
        self,
        request: OperatorControlRequest,
        proof: OperatorAuthenticationProof,
        *,
        committed_at: datetime,
    ) -> OperatorControlOutcome:
        self._check_usable()
        self._validate_request_and_proof(request, proof, committed_at)
        try:
            self._begin_immediate()
            self._verify_all(full=False)
            existing = self._find_request(str(request.request_id))
            verified = self._verify_request(request, proof)
            if existing is not None:
                return self._resolve_existing(
                    existing=existing,
                    request=request,
                    proof=proof,
                )
            same_action = self._resolve_same_action_replay(request)
            if same_action is not None:
                return same_action
            self._validate_policy_bound(request)
            sequence = self._next_sequence()
            new_action_id = action_id(request)
            resulting_state = (
                ControlState.ENABLED
                if request.action is ControlAction.ENABLE
                else ControlState.DISABLED
            )
            new_state_version_id = state_version_id(
                action_id_value=new_action_id,
                scope=request.scope,
                state=resulting_state,
                observed_at=request.observed_at,
                maximum_age=request.maximum_age,
            )
            new_request_commitment = request_commitment(request)
            new_audit_id = audit_id(
                control_sequence=sequence,
                action_id_value=new_action_id,
                request_id=request.request_id,
                scope=request.scope,
                requested_action=request.action,
                resulting_state=resulting_state,
                state_version_id_value=new_state_version_id,
                operator_reference=request.operator_reference,
                control_source=request.control_source,
                verifier_id=verified.verifier_id,
                proof_id=proof.proof_id,
                verified_at=verified.verified_at,
                observed_at=request.observed_at,
                maximum_age=request.maximum_age,
                committed_at=committed_at,
                request_commitment=new_request_commitment,
            )
            self._insert_audit(
                control_sequence=sequence,
                audit_id_value=new_audit_id,
                request=request,
                resulting_state=resulting_state,
                state_version_id_value=new_state_version_id,
                verifier=verified,
                proof=proof,
                committed_at=committed_at,
                request_commitment_value=new_request_commitment,
            )
            self._upsert_state(
                request=request,
                resulting_state=resulting_state,
                sequence=sequence,
                action_id_value=new_action_id,
                state_version_id_value=new_state_version_id,
            )
            self._verify_all(full=False)
            self._commit()
            return OperatorControlOutcome(
                replay=False,
                action_id=new_action_id,
                original_control_sequence=None,
                original_state_version_id=None,
                original_audit_id=None,
                original_resulting_state=None,
                current_effective_state=resulting_state,
            )
        except OperatorCommitUnknownError:
            raise
        except OperatorError:
            self._rollback_if_open()
            raise
        except sqlite3.OperationalError as exc:
            self._rollback_if_open()
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise OperatorBusyError("operator store is busy") from exc
            raise OperatorCorruptionError("operator store failed") from exc
        except sqlite3.IntegrityError as exc:
            self._rollback_if_open()
            raise OperatorCorruptionError(
                "operator store integrity failure"
            ) from exc
        except sqlite3.DatabaseError as exc:
            self._rollback_if_open()
            self._close_connection()
            raise OperatorCorruptionError("operator store is corrupt") from exc
        except OSError as exc:
            self._rollback_if_open()
            raise OperatorPathError("operator store filesystem failure") from exc
        except BaseException:
            self._rollback_if_open()
            raise

    def read_control_state(
        self, scope: OperatorControlScope
    ) -> OperatorControlState | None:
        self._check_usable()
        _validate_scope_shape(scope)
        self._verify_all(full=False)
        row = self._require_connection().execute(
            "SELECT * FROM control_state WHERE scope_key = ?",
            (scope_key(scope),),
        ).fetchone()
        if row is None:
            return None
        return self._state_from_row(row)

    def read_control_history(
        self,
        scope: OperatorControlScope | None = None,
        *,
        limit: int | None = None,
    ) -> tuple[OperatorControlAuditEvent, ...]:
        self._check_usable()
        if scope is not None:
            _validate_scope_shape(scope)
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise OperatorArgumentError("limit must be a nonnegative integer")
        self._verify_all(full=False)
        connection = self._require_connection()
        if scope is None:
            query = "SELECT * FROM control_audit ORDER BY control_sequence"
            params: tuple[Any, ...] = ()
        else:
            query = (
                "SELECT * FROM control_audit WHERE scope_key = ? "
                "ORDER BY control_sequence"
            )
            params = (scope_key(scope),)
        if limit is not None:
            query += " LIMIT ?"
            params += (limit,)
        rows = connection.execute(query, params).fetchall()
        return tuple(self._audit_event_from_row(row) for row in rows)

    def produce_operator_evidence(
        self, target: ExecutionTargetBinding
    ) -> OperatorExecutionEnableEvidence:
        self._check_usable()
        _validate_target_shape(target)
        self._verify_all(full=False)
        scope = OperatorControlScope.from_target(target)
        row = self._require_connection().execute(
            "SELECT * FROM control_state WHERE scope_key = ?",
            (scope_key(scope),),
        ).fetchone()
        if row is None:
            committed_scopes = self._require_connection().execute(
                "SELECT scope_key FROM control_state"
            ).fetchall()
            if committed_scopes:
                raise OperatorTargetMismatchError(
                    "target scope does not match committed operator state"
                )
            raise OperatorNotFoundError("no committed operator state")
        state = self._state_from_row(row)
        enabled = state.state is ControlState.ENABLED
        new_evidence_id = evidence_id(
            state_version_id_value=state.state_version_id,
            target=target,
            action_id_value=state.action_id,
            enabled=enabled,
            observed_at=state.observed_at,
            maximum_age=state.maximum_age,
        )
        return OperatorExecutionEnableEvidence(
            schema="frontier-operator-v1",
            evidence_id=new_evidence_id,
            target=target,
            action_id=state.action_id,
            enabled=enabled,
            observed_at=state.observed_at,
            maximum_age=state.maximum_age,
        )

    def close(self) -> None:
        self._closed = True
        self._close_connection()

    def __enter__(self) -> Self:
        self._check_usable()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Request help
    # ------------------------------------------------------------------

    def _check_usable(self) -> None:
        if self._closed:
            raise OperatorClosedError("operator store is closed")
        if self._poisoned:
            raise OperatorCommitUnknownError("operator store is poisoned")

    def _validate_request_and_proof(
        self,
        request: OperatorControlRequest,
        proof: OperatorAuthenticationProof,
        committed_at: datetime,
    ) -> None:
        if not isinstance(request, OperatorControlRequest):
            raise OperatorArgumentError(
                "request must be an OperatorControlRequest"
            )
        if not isinstance(proof, OperatorAuthenticationProof):
            raise OperatorAuthenticationError(
                "proof must be an OperatorAuthenticationProof"
            )
        self._validate_request_shape(request)
        self._validate_proof_shape(proof)
        canonical_instant(committed_at, name="committed_at")
        self._validate_policy_bound(request)
        if proof.action is not request.action:
            raise OperatorAuthenticationError("proof action does not match request")
        if proof.scope != request.scope:
            raise OperatorAuthenticationError("proof scope does not match request")
        if proof.operator_reference != request.operator_reference:
            raise OperatorAuthenticationError(
                "proof operator does not match request"
            )
        if proof.control_source != request.control_source:
            raise OperatorAuthenticationError(
                "proof control source does not match request"
            )
        if proof.request_commitment != request_commitment(request):
            raise OperatorAuthenticationError(
                "proof request commitment does not match request"
            )

    @staticmethod
    def _validate_request_shape(request: OperatorControlRequest) -> None:
        """Section 12 grammars, re-applied to untrusted caller values."""

        if getattr(request, "schema", None) != _REQUEST_SCHEMA:
            raise OperatorArgumentError("request schema is malformed")
        if not isinstance(getattr(request, "action", None), ControlAction):
            raise OperatorArgumentError("request action is malformed")
        opaque(_required_field(request, "request_id"), name="request_id")
        opaque(
            _required_field(request, "operator_reference"),
            name="operator_reference",
        )
        opaque(
            _required_field(request, "control_source"), name="control_source"
        )
        _validate_scope_shape(_required_field(request, "scope"))
        canonical_instant(
            _required_field(request, "observed_at"), name="observed_at"
        )
        duration_microseconds(
            _required_field(request, "maximum_age"), name="maximum_age"
        )

    @staticmethod
    def _validate_proof_shape(proof: OperatorAuthenticationProof) -> None:
        """Section 7 proof grammars; any failure is authentication-side."""

        try:
            if getattr(proof, "schema", None) != _PROOF_SCHEMA:
                raise OperatorArgumentError("proof schema is malformed")
            if not isinstance(getattr(proof, "action", None), ControlAction):
                raise OperatorArgumentError("proof action is malformed")
            opaque(_required_field(proof, "proof_id"), name="proof_id")
            opaque(
                _required_field(proof, "operator_reference"),
                name="operator_reference",
            )
            opaque(
                _required_field(proof, "control_source"),
                name="control_source",
            )
            commitment(_required_field(proof, "request_commitment"))
            _validate_scope_shape(_required_field(proof, "scope"))
            if _required_field(proof, "method") != "LOCAL_VERIFIER":
                raise OperatorArgumentError("proof method is malformed")
        except (OperatorArgumentError, ValueError, TypeError) as exc:
            raise OperatorAuthenticationError("operator proof is malformed") from exc

    def _validate_policy_bound(self, request: OperatorControlRequest) -> None:
        if request.maximum_age > self._policy.maximum_age_upper_bound:
            raise OperatorArgumentError(
                "maximum_age exceeds the operator policy bound"
            )

    def _verify_request(
        self,
        request: OperatorControlRequest,
        proof: OperatorAuthenticationProof,
    ) -> VerifiedOperatorIdentity:
        try:
            verified = self._verifier.verify(request, proof)
        except OperatorAuthenticationError as exc:
            raise OperatorAuthenticationError(
                "operator verifier rejected the proof"
            ) from exc
        if not isinstance(verified, VerifiedOperatorIdentity):
            raise OperatorAuthenticationError(
                "verifier did not return VerifiedOperatorIdentity"
            )
        if verified.operator_reference != request.operator_reference:
            raise OperatorAuthenticationError(
                "verified operator does not match request"
            )
        if verified.control_source != request.control_source:
            raise OperatorAuthenticationError(
                "verified control source does not match request"
            )
        if verified.proof_id != proof.proof_id:
            raise OperatorAuthenticationError(
                "verified proof_id does not match proof"
            )
        verifier_id = getattr(self._verifier, "verifier_id", None)
        if not isinstance(verifier_id, str) or verified.verifier_id != verifier_id:
            raise OperatorAuthenticationError(
                "verified verifier_id does not match verifier"
            )
        try:
            opaque(verified.verifier_id, name="verifier_id")
        except OperatorArgumentError as exc:
            raise OperatorAuthenticationError(
                "verified verifier_id is malformed"
            ) from exc
        try:
            canonical_instant(verified.verified_at, name="verified_at")
        except (OperatorArgumentError, ValueError, TypeError) as exc:
            raise OperatorAuthenticationError(
                "verified_at is not a canonical UTC instant"
            ) from exc
        if request.observed_at > verified.verified_at:
            raise OperatorAuthenticationError(
                "observed_at is later than verified_at"
            )
        if request.action is ControlAction.ENABLE and (
            verified.verified_at - request.observed_at > request.maximum_age
        ):
            raise OperatorAuthenticationError(
                "verified_at is outside the enable freshness bound"
            )
        return verified

    def _find_request(self, request_id: str) -> OperatorControlAuditEvent | None:
        row = self._require_connection().execute(
            "SELECT * FROM control_audit WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return self._audit_event_from_row(row)

    def _resolve_existing(
        self,
        *,
        existing: OperatorControlAuditEvent,
        request: OperatorControlRequest,
        proof: OperatorAuthenticationProof,
    ) -> OperatorControlOutcome:
        stored_request = {
            "action": existing.requested_action.value,
            "request_id": existing.request_id,
            "scope": scope_payload(existing.scope),
            "operator_reference": existing.operator_reference,
            "control_source": existing.control_source,
            "observed_at": canonical_instant(
                existing.observed_at, name="observed_at"
            ),
            "maximum_age_microseconds": duration_microseconds(
                existing.maximum_age, name="maximum_age"
            ),
        }
        submitted_request = action_payload(request)
        if canonical_json(stored_request) != canonical_json(submitted_request):
            raise OperatorConflictError(
                "request_id was reused with a different canonical request"
            )
        current_row = self._require_connection().execute(
            "SELECT state FROM control_state WHERE scope_key = ?",
            (existing.scope_key,),
        ).fetchone()
        current_state = (
            ControlState(str(current_row["state"]))
            if current_row is not None
            else None
        )
        self._rollback_if_open()
        return OperatorControlOutcome(
            replay=True,
            action_id=existing.action_id,
            original_control_sequence=existing.control_sequence,
            original_state_version_id=existing.state_version_id,
            original_audit_id=existing.audit_id,
                original_resulting_state=existing.resulting_state,
                current_effective_state=current_state,
            )

    def _resolve_same_action_replay(
        self,
        request: OperatorControlRequest,
    ) -> OperatorControlOutcome | None:
        """Contract section 10: same action as the durable state is REPLAY."""

        row = self._require_connection().execute(
            "SELECT state FROM control_state WHERE scope_key = ?",
            (scope_key(request.scope),),
        ).fetchone()
        if row is None:
            return None
        current_state = ControlState(str(row["state"]))
        submitted_state = ControlState(
            "ENABLED"
            if request.action is ControlAction.ENABLE
            else "DISABLED"
        )
        if current_state is not submitted_state:
            return None
        head_row = self._require_connection().execute(
            "SELECT * FROM control_audit WHERE scope_key = ? "
            "ORDER BY control_sequence DESC LIMIT 1",
            (scope_key(request.scope),),
        ).fetchone()
        if head_row is None:
            raise OperatorCorruptionError(
                "control_state has no matching audit history"
            )
        head = self._audit_event_from_row(head_row)
        if head.scope != request.scope:
            raise OperatorCorruptionError(
                "control_state audit head scope mismatch"
            )
        self._rollback_if_open()
        return OperatorControlOutcome(
            replay=True,
            action_id=action_id(request),
            original_control_sequence=head.control_sequence,
            original_state_version_id=head.state_version_id,
            original_audit_id=head.audit_id,
            original_resulting_state=head.resulting_state,
            current_effective_state=current_state,
        )

    def _next_sequence(self) -> int:
        row = self._require_connection().execute(
            "SELECT COALESCE(MAX(control_sequence), 0) + 1 FROM control_audit"
        ).fetchone()
        sequence = int(row[0])
        if sequence > _CONTROL_SEQUENCE_MAX:
            raise OperatorSequenceExhaustedError(
                "control sequence domain is exhausted"
            )
        return sequence

    def _insert_audit(
        self,
        *,
        control_sequence: int,
        audit_id_value: str,
        request: OperatorControlRequest,
        resulting_state: ControlState,
        state_version_id_value: str,
        verifier: VerifiedOperatorIdentity,
        proof: OperatorAuthenticationProof,
        committed_at: datetime,
        request_commitment_value: str,
    ) -> None:
        connection = self._require_connection()
        connection.execute(
            """
            INSERT INTO control_audit(
                control_sequence, audit_id, request_id, action_id, scope_key,
                broker, environment, account_reference, adapter_id,
                adapter_version, requested_action, resulting_state,
                state_version_id, operator_reference, control_source,
                verifier_id, proof_id, verified_at, observed_at,
                maximum_age_microseconds, committed_at, request_commitment
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                control_sequence,
                audit_id_value,
                request.request_id,
                action_id(request),
                scope_key(request.scope),
                request.scope.broker,
                request.scope.environment,
                request.scope.account_reference,
                request.scope.adapter_id,
                request.scope.adapter_version,
                request.action.value,
                resulting_state.value,
                state_version_id_value,
                request.operator_reference,
                request.control_source,
                verifier.verifier_id,
                proof.proof_id,
                canonical_instant(verifier.verified_at, name="verified_at"),
                canonical_instant(request.observed_at, name="observed_at"),
                duration_microseconds(request.maximum_age, name="maximum_age"),
                canonical_instant(committed_at, name="committed_at"),
                request_commitment_value,
            ),
        )

    def _upsert_state(
        self,
        *,
        request: OperatorControlRequest,
        resulting_state: ControlState,
        sequence: int,
        action_id_value: str,
        state_version_id_value: str,
    ) -> None:
        connection = self._require_connection()
        connection.execute(
            """
            INSERT OR REPLACE INTO control_state(
                scope_key, broker, environment, account_reference,
                adapter_id, adapter_version, state, control_sequence,
                action_id, state_version_id, observed_at,
                maximum_age_microseconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope_key(request.scope),
                request.scope.broker,
                request.scope.environment,
                request.scope.account_reference,
                request.scope.adapter_id,
                request.scope.adapter_version,
                resulting_state.value,
                sequence,
                action_id_value,
                state_version_id_value,
                canonical_instant(request.observed_at, name="observed_at"),
                duration_microseconds(request.maximum_age, name="maximum_age"),
            ),
        )

    @staticmethod
    def _state_from_row(row: sqlite3.Row) -> OperatorControlState:
        try:
            scope = OperatorControlScope(
                schema="operator-scope-v1",
                broker=str(row["broker"]),
                environment=_environment(row["environment"]),
                account_reference=str(row["account_reference"]),
                adapter_id=str(row["adapter_id"]),
                adapter_version=str(row["adapter_version"]),
            )
            return OperatorControlState(
                scope=scope,
                scope_key=str(row["scope_key"]),
                state=ControlState(str(row["state"])),
                control_sequence=_stored_int(
                    row["control_sequence"], name="control_sequence"
                ),
                action_id=str(row["action_id"]),
                state_version_id=str(row["state_version_id"]),
                observed_at=parse_canonical_instant(
                    row["observed_at"], name="observed_at"
                ),
                maximum_age=microseconds_duration(
                    row["maximum_age_microseconds"],
                    name="maximum_age_microseconds",
                ),
            )
        except OperatorCorruptionError:
            raise
        except (OperatorArgumentError, ValueError, TypeError) as exc:
            raise OperatorCorruptionError(
                "stored control_state row is malformed"
            ) from exc

    # ------------------------------------------------------------------
    # Transaction and connection helpers
    # ------------------------------------------------------------------

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise OperatorClosedError("operator store has no connection")
        return self._connection

    def _begin_immediate(self) -> None:
        try:
            self._require_connection().execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise OperatorBusyError("operator store is busy") from exc
            raise

    def _before_commit(self) -> None:
        """Test-only definite-pre-commit failure seam."""

    def _commit_connection(self) -> None:
        """The single COMMIT seam shared by production and tests."""

        self._require_connection().commit()

    def _commit(self) -> None:
        self._before_commit()
        try:
            self._commit_connection()
        except BaseException as exc:
            self._poisoned = True
            raise OperatorCommitUnknownError(
                "operator commit outcome is unknown"
            ) from exc

    def _rollback_if_open(self) -> None:
        if self._connection is None or self._poisoned:
            return
        try:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _close_connection(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _map_sqlite_error(exc: sqlite3.Error) -> Exception:
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            return OperatorBusyError("operator store is busy")
        if "malformed" in message or "corrupt" in message or "not a database" in message:
            return OperatorCorruptionError("operator store is corrupt")
        return OperatorCorruptionError("operator store failure")


def operator_evidence_for_frontier(
    store: SQLiteOperatorControlStore,
    target: ExecutionTargetBinding,
) -> OperatorExecutionEnableEvidence | None:
    """Canonical fresh-evidence composition helper."""

    if type(store) is not SQLiteOperatorControlStore:
        raise OperatorArgumentError(
            "store must be exactly SQLiteOperatorControlStore"
        )
    _validate_target_shape(target)
    try:
        return SQLiteOperatorControlStore.produce_operator_evidence(store, target)
    except OperatorNotFoundError:
        return None


__all__ = ["SQLiteOperatorControlStore", "operator_evidence_for_frontier"]
