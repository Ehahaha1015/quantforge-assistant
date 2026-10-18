"""Durable SQLite-backed execution state for QuantForge.

This module implements the EXECSTATE-001 contract.  It records intent,
atomic claims, durable outcomes, and startup recovery.  It performs no
network, broker, credential, market-data, or portfolio action.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple, Self

from ..broker import (
    BrokerPreDispatchError,
    BrokerSubmissionEnvelope,
    BrokerSubmissionOutcome,
)
from ..frontier import (
    AtomicDispatchClaimEvidence,
    DurableCommitReceipt,
    DurableSubmissionRecord,
    ExecutionTargetBinding,
    FrontierEligibilityDecision,
    RecoveryCompleteEvidence,
)
from ..provenance import FreshRiskProvenance
from .errors import (
    ExecutionStateArgumentError,
    ExecutionStateBusyError,
    ExecutionStateCommitUnknownError,
    ExecutionStateConflictError,
    ExecutionStateCorruptionError,
    ExecutionStateDeniedError,
    ExecutionStateError,
    ExecutionStateNotFoundError,
    ExecutionStatePathError,
    ExecutionStateRecoveryRequiredError,
    ExecutionStateSchemaError,
)

__all__ = ["SQLiteExecutionStateStore"]

StateLiteral = Literal[
    "PREPARED",
    "DISPATCHING",
    "ACKNOWLEDGED",
    "REJECTED",
    "OUTCOME_UNKNOWN",
    "NO_EFFECT_CONFIRMED",
]

_SCHEMA_NAME = "quantforge.execstate.sqlite"
_SCHEMA_VERSION = 2
_MIN_SQLITE_VERSION = (3, 35, 0)
_STATES: tuple[StateLiteral, ...] = (
    "PREPARED",
    "DISPATCHING",
    "ACKNOWLEDGED",
    "REJECTED",
    "OUTCOME_UNKNOWN",
    "NO_EFFECT_CONFIRMED",
)
_ROW_SCOPE_SUBMISSION = "SUBMISSION"
_ROW_SCOPE_BOUNDARY = "RECOVERY_BOUNDARY"
_CUT_BEGIN_KIND = "RECOVERY_CUT_BEGIN"
_CUT_END_KIND = "RECOVERY_CUT_END"
_CUT_PHASES: tuple[str, str] = ("BEGIN", "END")
_EVENT_KINDS = (
    "PREPARED",
    "CLAIM_ACQUIRED",
    "CLAIM_CONFLICT",
    "CLAIM_REVOKED",
    "DISPATCHING",
    "TRANSPORT_OUTCOME",
    "PRE_DISPATCH_NO_EFFECT",
    "RECONCILIATION",
    "RECOVERY_EPOCH_REBIND",
    "RECOVERY_UNKNOWN_CONVERSION",
    "RECOVERY_CUT_BEGIN",
    "RECOVERY_CUT_END",
)
_REASON_PAIRS = frozenset(
    {
        ("PREPARED", "NEW_PREPARE"),
        ("PREPARED", "DUPLICATE_PREPARE_REPLAY"),
        ("CLAIM_ACQUIRED", "CLAIM_ACQUIRED"),
        ("CLAIM_ACQUIRED", "CLAIM_REPLAY"),
        ("CLAIM_CONFLICT", "CLAIM_CONFLICT"),
        ("CLAIM_REVOKED", "CLAIM_REVOKED"),
        ("DISPATCHING", "MARK_DISPATCHING"),
        ("DISPATCHING", "DISPATCHING_REPLAY"),
        ("TRANSPORT_OUTCOME", "TRANSPORT_ACCEPTED"),
        ("TRANSPORT_OUTCOME", "TRANSPORT_REJECTED"),
        ("TRANSPORT_OUTCOME", "TRANSPORT_OUTCOME_UNKNOWN"),
        ("TRANSPORT_OUTCOME", "TRANSPORT_OUTCOME_REPLAY"),
        ("PRE_DISPATCH_NO_EFFECT", "PRE_DISPATCH_NO_EFFECT"),
        ("PRE_DISPATCH_NO_EFFECT", "PRE_DISPATCH_NO_EFFECT_REPLAY"),
        ("RECONCILIATION", "RECONCILIATION_ACCEPTED"),
        ("RECONCILIATION", "RECONCILIATION_REJECTED"),
        ("RECONCILIATION", "RECONCILIATION_NO_EFFECT"),
        ("RECONCILIATION", "RECONCILIATION_REPLAY"),
        ("RECOVERY_EPOCH_REBIND", "RECOVERY_EPOCH_REBIND"),
        ("RECOVERY_UNKNOWN_CONVERSION", "RECOVERY_UNKNOWN_CONVERSION"),
        ("RECOVERY_CUT_BEGIN", "RECOVERY_CUT_BEGIN"),
        ("RECOVERY_CUT_END", "RECOVERY_CUT_END"),
    }
)
_OPAQUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_REASON_PATTERN = re.compile(r"^[A-Z0-9_]{1,64}$")
_PAYLOAD_KIND_BY_EVENT_KIND = {
    "TRANSPORT_OUTCOME": "TRANSPORT_OUTCOME",
    "PRE_DISPATCH_NO_EFFECT": "PRE_DISPATCH_NO_EFFECT",
    "RECONCILIATION": "RECONCILIATION",
}
_GOVERNING_TRANSITIONS: dict[str, frozenset[tuple[str, str]]] = {
    "PREPARED": frozenset({("PREPARED", "NEW_PREPARE")}),
    "DISPATCHING": frozenset({("DISPATCHING", "MARK_DISPATCHING")}),
    "ACKNOWLEDGED": frozenset(
        {
            ("TRANSPORT_OUTCOME", "TRANSPORT_ACCEPTED"),
            ("RECONCILIATION", "RECONCILIATION_ACCEPTED"),
        }
    ),
    "REJECTED": frozenset(
        {
            ("TRANSPORT_OUTCOME", "TRANSPORT_REJECTED"),
            ("RECONCILIATION", "RECONCILIATION_REJECTED"),
        }
    ),
    "OUTCOME_UNKNOWN": frozenset(
        {
            ("TRANSPORT_OUTCOME", "TRANSPORT_OUTCOME_UNKNOWN"),
            ("RECOVERY_UNKNOWN_CONVERSION", "RECOVERY_UNKNOWN_CONVERSION"),
        }
    ),
    "NO_EFFECT_CONFIRMED": frozenset(
        {
            ("PRE_DISPATCH_NO_EFFECT", "PRE_DISPATCH_NO_EFFECT"),
            ("RECONCILIATION", "RECONCILIATION_NO_EFFECT"),
        }
    ),
}
_CLOSED_STATES = frozenset({"PREPARED", "DISPATCHING"})
_PAYLOAD_BEARING_TERMINAL_STATES = frozenset(
    {"ACKNOWLEDGED", "REJECTED", "NO_EFFECT_CONFIRMED"}
)
_RECONCILIATION_STATUSES = ("ACCEPTED", "REJECTED", "NO_EFFECT_CONFIRMED")
_RECONCILIATION_FIELDS = frozenset(
    {
        "evidence_reference",
        "observed_at",
        "status",
        "submission_id",
        "target_json",
    }
)
_NO_EFFECT_FIELDS = frozenset(
    {
        "classification",
        "observed_at",
        "proof_reference",
        "submission_id",
        "target_json",
    }
)
_STATE_BY_RECORDED_STATUS: dict[str, str] = {
    "ACCEPTED": "ACKNOWLEDGED",
    "REJECTED": "REJECTED",
    "OUTCOME_UNKNOWN": "OUTCOME_UNKNOWN",
    "NO_EFFECT_CONFIRMED": "NO_EFFECT_CONFIRMED",
}
_REASON_BY_EVENT_KIND_AND_STATUS: dict[tuple[str, str], str] = {
    ("TRANSPORT_OUTCOME", "ACCEPTED"): "TRANSPORT_ACCEPTED",
    ("TRANSPORT_OUTCOME", "REJECTED"): "TRANSPORT_REJECTED",
    ("TRANSPORT_OUTCOME", "OUTCOME_UNKNOWN"): "TRANSPORT_OUTCOME_UNKNOWN",
    ("RECONCILIATION", "ACCEPTED"): "RECONCILIATION_ACCEPTED",
    ("RECONCILIATION", "REJECTED"): "RECONCILIATION_REJECTED",
    ("RECONCILIATION", "NO_EFFECT_CONFIRMED"): "RECONCILIATION_NO_EFFECT",
    ("PRE_DISPATCH_NO_EFFECT", "NO_EFFECT_CONFIRMED"): "PRE_DISPATCH_NO_EFFECT",
}
_REPLAY_REASONS = frozenset(
    {
        "TRANSPORT_OUTCOME_REPLAY",
        "PRE_DISPATCH_NO_EFFECT_REPLAY",
        "RECONCILIATION_REPLAY",
    }
)

_STATE_AUTHORITATIVE_PAIRS = frozenset(
    {
        ("PREPARED", "NEW_PREPARE"),
        ("CLAIM_ACQUIRED", "CLAIM_ACQUIRED"),
        ("CLAIM_REVOKED", "CLAIM_REVOKED"),
        ("DISPATCHING", "MARK_DISPATCHING"),
        ("TRANSPORT_OUTCOME", "TRANSPORT_ACCEPTED"),
        ("TRANSPORT_OUTCOME", "TRANSPORT_REJECTED"),
        ("TRANSPORT_OUTCOME", "TRANSPORT_OUTCOME_UNKNOWN"),
        ("PRE_DISPATCH_NO_EFFECT", "PRE_DISPATCH_NO_EFFECT"),
        ("RECONCILIATION", "RECONCILIATION_ACCEPTED"),
        ("RECONCILIATION", "RECONCILIATION_REJECTED"),
        ("RECONCILIATION", "RECONCILIATION_NO_EFFECT"),
        ("RECOVERY_EPOCH_REBIND", "RECOVERY_EPOCH_REBIND"),
        ("RECOVERY_UNKNOWN_CONVERSION", "RECOVERY_UNKNOWN_CONVERSION"),
    }
)

_OBSERVATIONAL_PAIRS = frozenset(
    {
        ("PREPARED", "DUPLICATE_PREPARE_REPLAY"),
        ("CLAIM_ACQUIRED", "CLAIM_REPLAY"),
        ("CLAIM_CONFLICT", "CLAIM_CONFLICT"),
        ("DISPATCHING", "DISPATCHING_REPLAY"),
        ("TRANSPORT_OUTCOME", "TRANSPORT_OUTCOME_REPLAY"),
        ("PRE_DISPATCH_NO_EFFECT", "PRE_DISPATCH_NO_EFFECT_REPLAY"),
        ("RECONCILIATION", "RECONCILIATION_REPLAY"),
    }
)

_RECOVERY_MUTATION_KINDS = frozenset(
    {
        "RECOVERY_EPOCH_REBIND",
        "CLAIM_REVOKED",
        "RECOVERY_UNKNOWN_CONVERSION",
    }
)

_AuditFieldProfile = Literal[
    "empty",
    "claim",
    "claim_conflict",
    "payload_claim",
    "payload_owner",
    "payload_empty",
]


class _AuditSemanticsSpec(NamedTuple):
    states: frozenset[tuple[str | None, str]]
    fields: _AuditFieldProfile
    receipt_required: bool = False
    recovery_event: bool = False


_SAME_STATE_PAIRS = frozenset((state, state) for state in _STATES)
_AUDIT_SEMANTICS: dict[tuple[str, str], _AuditSemanticsSpec] = {
    ("PREPARED", "NEW_PREPARE"): _AuditSemanticsSpec(
        frozenset({(None, "PREPARED")}), "empty", receipt_required=True
    ),
    ("PREPARED", "DUPLICATE_PREPARE_REPLAY"): _AuditSemanticsSpec(
        frozenset({("PREPARED", "PREPARED")}), "empty"
    ),
    ("CLAIM_ACQUIRED", "CLAIM_ACQUIRED"): _AuditSemanticsSpec(
        frozenset({("PREPARED", "PREPARED")}), "claim"
    ),
    ("CLAIM_ACQUIRED", "CLAIM_REPLAY"): _AuditSemanticsSpec(
        frozenset({("PREPARED", "PREPARED")}), "claim"
    ),
    ("CLAIM_CONFLICT", "CLAIM_CONFLICT"): _AuditSemanticsSpec(
        frozenset({("PREPARED", "PREPARED")}), "claim_conflict"
    ),
    ("CLAIM_REVOKED", "CLAIM_REVOKED"): _AuditSemanticsSpec(
        _SAME_STATE_PAIRS, "claim", recovery_event=True
    ),
    ("DISPATCHING", "MARK_DISPATCHING"): _AuditSemanticsSpec(
        frozenset({("PREPARED", "DISPATCHING")}),
        "claim",
        receipt_required=True,
    ),
    ("DISPATCHING", "DISPATCHING_REPLAY"): _AuditSemanticsSpec(
        frozenset({("DISPATCHING", "DISPATCHING")}), "claim"
    ),
    ("TRANSPORT_OUTCOME", "TRANSPORT_ACCEPTED"): _AuditSemanticsSpec(
        frozenset({("DISPATCHING", "ACKNOWLEDGED")}),
        "payload_claim",
        receipt_required=True,
    ),
    ("TRANSPORT_OUTCOME", "TRANSPORT_REJECTED"): _AuditSemanticsSpec(
        frozenset({("DISPATCHING", "REJECTED")}),
        "payload_claim",
        receipt_required=True,
    ),
    ("TRANSPORT_OUTCOME", "TRANSPORT_OUTCOME_UNKNOWN"): _AuditSemanticsSpec(
        frozenset({("DISPATCHING", "OUTCOME_UNKNOWN")}),
        "payload_claim",
        receipt_required=True,
    ),
    ("TRANSPORT_OUTCOME", "TRANSPORT_OUTCOME_REPLAY"): _AuditSemanticsSpec(
        frozenset(
            {
                ("ACKNOWLEDGED", "ACKNOWLEDGED"),
                ("REJECTED", "REJECTED"),
                ("OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN"),
            }
        ),
        "payload_owner",
    ),
    ("PRE_DISPATCH_NO_EFFECT", "PRE_DISPATCH_NO_EFFECT"): _AuditSemanticsSpec(
        frozenset({("DISPATCHING", "NO_EFFECT_CONFIRMED")}),
        "payload_claim",
        receipt_required=True,
    ),
    (
        "PRE_DISPATCH_NO_EFFECT",
        "PRE_DISPATCH_NO_EFFECT_REPLAY",
    ): _AuditSemanticsSpec(
        frozenset({("NO_EFFECT_CONFIRMED", "NO_EFFECT_CONFIRMED")}),
        "payload_owner",
    ),
    ("RECONCILIATION", "RECONCILIATION_ACCEPTED"): _AuditSemanticsSpec(
        frozenset({("OUTCOME_UNKNOWN", "ACKNOWLEDGED")}),
        "payload_empty",
        receipt_required=True,
    ),
    ("RECONCILIATION", "RECONCILIATION_REJECTED"): _AuditSemanticsSpec(
        frozenset({("OUTCOME_UNKNOWN", "REJECTED")}),
        "payload_empty",
        receipt_required=True,
    ),
    ("RECONCILIATION", "RECONCILIATION_NO_EFFECT"): _AuditSemanticsSpec(
        frozenset({("OUTCOME_UNKNOWN", "NO_EFFECT_CONFIRMED")}),
        "payload_empty",
        receipt_required=True,
    ),
    ("RECONCILIATION", "RECONCILIATION_REPLAY"): _AuditSemanticsSpec(
        frozenset(
            {
                ("ACKNOWLEDGED", "ACKNOWLEDGED"),
                ("REJECTED", "REJECTED"),
                ("NO_EFFECT_CONFIRMED", "NO_EFFECT_CONFIRMED"),
            }
        ),
        "payload_empty",
    ),
    ("RECOVERY_EPOCH_REBIND", "RECOVERY_EPOCH_REBIND"): _AuditSemanticsSpec(
        _SAME_STATE_PAIRS,
        "empty",
        receipt_required=True,
        recovery_event=True,
    ),
    (
        "RECOVERY_UNKNOWN_CONVERSION",
        "RECOVERY_UNKNOWN_CONVERSION",
    ): _AuditSemanticsSpec(
        frozenset({("DISPATCHING", "OUTCOME_UNKNOWN")}),
        "empty",
        receipt_required=True,
        recovery_event=True,
    ),
}

_ForeignKeySpec = tuple[
    str,
    tuple[tuple[str, str], ...],
    str,
    str,
    str,
]
_EXPECTED_FOREIGN_KEYS: dict[str, frozenset[_ForeignKeySpec]] = {
    "metadata": frozenset(),
    "submissions": frozenset(
        {
            (
                "commit_receipts",
                (("current_receipt_id", "receipt_id"),),
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
            (
                "claims",
                (("active_claim_id", "claim_id"),),
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
        }
    ),
    "commit_receipts": frozenset(
        {
            (
                "submissions",
                (("record_id", "record_id"),),
                "NO ACTION",
                "NO ACTION",
                "NONE",
            )
        }
    ),
    "claims": frozenset(
        {
            (
                "submissions",
                (("record_id", "record_id"),),
                "NO ACTION",
                "NO ACTION",
                "NONE",
            )
        }
    ),
    "operational_payloads": frozenset(
        {
            (
                "submissions",
                (("record_id", "record_id"),),
                "NO ACTION",
                "NO ACTION",
                "NONE",
            )
        }
    ),
    "recovery_runs": frozenset(),
    "audit_events": frozenset(
        {
            (
                "submissions",
                (
                    ("submission_id", "submission_id"),
                    ("target_json", "target_json"),
                ),
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
            (
                "recovery_runs",
                (("boundary_run_sequence", "run_sequence"),),
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
        }
    ),
}
_CANONICAL_INSTANT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
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
_MAX_PATH_CHARS = 4096
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_PREPARE_BYTES = 128 * 1024 * 1024
_INT64_MAX = 9_223_372_036_854_775_807

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    INSERT INTO metadata(key, value) VALUES
        ('schema_name', 'quantforge.execstate.sqlite'),
        ('schema_version', '2'),
        ('next_generation', '1'),
        ('next_recovery_epoch', '1')
    """,
    """
    CREATE TABLE submissions (
        submission_id TEXT NOT NULL,
        target_json TEXT NOT NULL,
        record_id TEXT NOT NULL UNIQUE,
        generation INTEGER NOT NULL UNIQUE CHECK (generation >= 1),
        recovery_epoch INTEGER NOT NULL CHECK (recovery_epoch >= 1),
        state TEXT NOT NULL CHECK (
            state IN (
                'PREPARED', 'DISPATCHING', 'ACKNOWLEDGED',
                'REJECTED', 'OUTCOME_UNKNOWN', 'NO_EFFECT_CONFIRMED'
            )
        ),
        record_json TEXT NOT NULL,
        envelope_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        provenance_id TEXT NOT NULL,
        current_receipt_id TEXT REFERENCES commit_receipts(receipt_id)
            DEFERRABLE INITIALLY DEFERRED,
        active_claim_id TEXT REFERENCES claims(claim_id)
            DEFERRABLE INITIALLY DEFERRED,
        dispatch_decision_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (submission_id, target_json)
    )
    """,
    """
    CREATE TABLE commit_receipts (
        receipt_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        receipt_id TEXT NOT NULL UNIQUE,
        record_id TEXT NOT NULL REFERENCES submissions(record_id),
        submission_id TEXT NOT NULL,
        target_json TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        recovery_epoch INTEGER NOT NULL CHECK (recovery_epoch >= 1),
        receipt_json TEXT NOT NULL,
        committed INTEGER NOT NULL CHECK (committed = 1),
        committed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE claims (
        claim_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id TEXT NOT NULL UNIQUE,
        record_id TEXT NOT NULL REFERENCES submissions(record_id),
        submission_id TEXT NOT NULL,
        target_json TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        recovery_epoch INTEGER NOT NULL CHECK (recovery_epoch >= 1),
        claim_json TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        token TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status = 'ACQUIRED'),
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE operational_payloads (
        payload_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id TEXT NOT NULL REFERENCES submissions(record_id),
        submission_id TEXT NOT NULL,
        target_json TEXT NOT NULL,
        payload_kind TEXT NOT NULL CHECK (
            payload_kind IN (
                'TRANSPORT_OUTCOME', 'RECONCILIATION',
                'PRE_DISPATCH_NO_EFFECT'
            )
        ),
        payload_json TEXT NOT NULL,
        observed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE recovery_runs (
        run_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        evidence_id TEXT NOT NULL UNIQUE,
        target_json TEXT NOT NULL,
        recovery_epoch INTEGER NOT NULL CHECK (recovery_epoch >= 1),
        scanned_through_generation INTEGER NOT NULL
            CHECK (scanned_through_generation >= 0),
        status TEXT NOT NULL CHECK (status IN ('COMPLETE', 'INCOMPLETE')),
        evidence_json TEXT NOT NULL,
        observed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE audit_events (
        event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        row_scope TEXT NOT NULL CHECK (
            row_scope IN ('SUBMISSION', 'RECOVERY_BOUNDARY')
        ),
        submission_id TEXT,
        target_json TEXT NOT NULL,
        prior_state TEXT,
        new_state TEXT,
        generation INTEGER,
        recovery_epoch INTEGER NOT NULL CHECK (recovery_epoch >= 1),
        event_kind TEXT NOT NULL CHECK (
            event_kind IN (
                'PREPARED', 'CLAIM_ACQUIRED', 'CLAIM_CONFLICT',
                'CLAIM_REVOKED', 'DISPATCHING', 'TRANSPORT_OUTCOME',
                'PRE_DISPATCH_NO_EFFECT', 'RECONCILIATION',
                'RECOVERY_EPOCH_REBIND', 'RECOVERY_UNKNOWN_CONVERSION',
                'RECOVERY_CUT_BEGIN', 'RECOVERY_CUT_END'
            )
        ),
        reason TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        owner_id TEXT,
        token_reference TEXT,
        claim_id TEXT,
        payload_sequence INTEGER,
        status TEXT,
        boundary_run_sequence INTEGER
            REFERENCES recovery_runs(run_sequence) DEFERRABLE INITIALLY DEFERRED,
        boundary_phase TEXT CHECK (boundary_phase IN ('BEGIN', 'END')),
        event_json TEXT NOT NULL,
        CHECK (
            (
                row_scope = 'SUBMISSION'
                AND submission_id IS NOT NULL
                AND generation IS NOT NULL
                AND generation >= 1
                AND new_state IS NOT NULL
                AND new_state IN (
                    'PREPARED', 'DISPATCHING', 'ACKNOWLEDGED',
                    'REJECTED', 'OUTCOME_UNKNOWN', 'NO_EFFECT_CONFIRMED'
                )
                AND event_kind IN (
                    'PREPARED', 'CLAIM_ACQUIRED', 'CLAIM_CONFLICT',
                    'CLAIM_REVOKED', 'DISPATCHING', 'TRANSPORT_OUTCOME',
                    'PRE_DISPATCH_NO_EFFECT', 'RECONCILIATION',
                    'RECOVERY_EPOCH_REBIND', 'RECOVERY_UNKNOWN_CONVERSION'
                )
                AND boundary_run_sequence IS NULL
                AND boundary_phase IS NULL
            )
            OR
            (
                row_scope = 'RECOVERY_BOUNDARY'
                AND submission_id IS NULL
                AND prior_state IS NULL
                AND new_state IS NULL
                AND generation IS NULL
                AND owner_id IS NULL
                AND token_reference IS NULL
                AND claim_id IS NULL
                AND payload_sequence IS NULL
                AND status IS NULL
                AND boundary_run_sequence IS NOT NULL
                AND boundary_run_sequence >= 1
                AND boundary_phase IS NOT NULL
                AND (
                    (event_kind = 'RECOVERY_CUT_BEGIN'
                        AND boundary_phase = 'BEGIN')
                    OR (event_kind = 'RECOVERY_CUT_END'
                        AND boundary_phase = 'END')
                )
                AND reason = event_kind
            )
        ),
        FOREIGN KEY (submission_id, target_json)
            REFERENCES submissions(submission_id, target_json)
    )
    """,
    """
    CREATE UNIQUE INDEX submissions_one_active_claim
    ON submissions(active_claim_id)
    WHERE active_claim_id IS NOT NULL
    """,
    """
    CREATE INDEX commit_receipts_lookup
    ON commit_receipts(record_id, recovery_epoch)
    """,
    """
    CREATE INDEX claims_lookup
    ON claims(record_id, recovery_epoch)
    """,
    """
    CREATE INDEX audit_events_lookup
    ON audit_events(submission_id, target_json, event_sequence)
    """,
    """
    CREATE UNIQUE INDEX audit_events_boundary_run_phase
    ON audit_events(boundary_run_sequence, boundary_phase)
    WHERE boundary_run_sequence IS NOT NULL
    """,
    """
    CREATE TRIGGER commit_receipts_no_update
    BEFORE UPDATE ON commit_receipts
    BEGIN SELECT RAISE(ABORT, 'commit receipts are immutable'); END
    """,
    """
    CREATE TRIGGER commit_receipts_no_delete
    BEFORE DELETE ON commit_receipts
    BEGIN SELECT RAISE(ABORT, 'commit receipts are immutable'); END
    """,
    """
    CREATE TRIGGER claims_no_update
    BEFORE UPDATE ON claims
    BEGIN SELECT RAISE(ABORT, 'claims are immutable'); END
    """,
    """
    CREATE TRIGGER claims_no_delete
    BEFORE DELETE ON claims
    BEGIN SELECT RAISE(ABORT, 'claims are immutable'); END
    """,
    """
    CREATE TRIGGER operational_payloads_no_update
    BEFORE UPDATE ON operational_payloads
    BEGIN SELECT RAISE(ABORT, 'operational payloads are append-only'); END
    """,
    """
    CREATE TRIGGER operational_payloads_no_delete
    BEFORE DELETE ON operational_payloads
    BEGIN SELECT RAISE(ABORT, 'operational payloads are append-only'); END
    """,
    """
    CREATE TRIGGER recovery_runs_no_update
    BEFORE UPDATE ON recovery_runs
    BEGIN SELECT RAISE(ABORT, 'recovery runs are append-only'); END
    """,
    """
    CREATE TRIGGER recovery_runs_no_delete
    BEFORE DELETE ON recovery_runs
    BEGIN SELECT RAISE(ABORT, 'recovery runs are append-only'); END
    """,
    """
    CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END
    """,
    """
    CREATE TRIGGER audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END
    """,
    """
    CREATE TRIGGER recovery_runs_require_cut
    BEFORE INSERT ON recovery_runs
    FOR EACH ROW
    BEGIN
        SELECT RAISE(
            ABORT,
            'recovery run requires exactly one BEGIN and one END cut anchor'
        )
        WHERE
            (SELECT COUNT(*) FROM audit_events
              WHERE boundary_run_sequence = NEW.run_sequence) <> 2
            OR (SELECT COUNT(*) FROM audit_events
                 WHERE boundary_run_sequence = NEW.run_sequence
                   AND boundary_phase = 'BEGIN'
                   AND event_kind = 'RECOVERY_CUT_BEGIN'
                   AND reason = 'RECOVERY_CUT_BEGIN'
                   AND target_json = NEW.target_json
                   AND recovery_epoch = NEW.recovery_epoch
                   AND observed_at = NEW.observed_at) <> 1
            OR (SELECT COUNT(*) FROM audit_events
                 WHERE boundary_run_sequence = NEW.run_sequence
                   AND boundary_phase = 'END'
                   AND event_kind = 'RECOVERY_CUT_END'
                   AND reason = 'RECOVERY_CUT_END'
                   AND target_json = NEW.target_json
                   AND recovery_epoch = NEW.recovery_epoch
                   AND observed_at = NEW.observed_at) <> 1
            OR (SELECT event_sequence FROM audit_events
                 WHERE boundary_run_sequence = NEW.run_sequence
                   AND boundary_phase = 'BEGIN')
               >=
               (SELECT event_sequence FROM audit_events
                 WHERE boundary_run_sequence = NEW.run_sequence
                   AND boundary_phase = 'END');
    END
    """,
    """
    CREATE TRIGGER submissions_no_delete
    BEFORE DELETE ON submissions
    BEGIN SELECT RAISE(ABORT, 'submissions are immutable roots'); END
    """,
)


def _utc(value: Any, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ExecutionStateArgumentError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionStateArgumentError(f"{name} must be timezone-aware")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ExecutionStateArgumentError(
            f"{name} is outside the representable UTC range"
        ) from exc


def _canonical_instant(value: datetime) -> str:
    moment = _utc(value, name="timestamp")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_instant(value: Any, *, name: str) -> datetime:
    if not isinstance(value, str) or _CANONICAL_INSTANT.fullmatch(value) is None:
        raise ExecutionStateCorruptionError(f"{name} is not a canonical UTC instant")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ExecutionStateCorruptionError(
            f"{name} is not a valid canonical UTC instant"
        ) from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _opaque(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or _OPAQUE_PATTERN.fullmatch(value) is None:
        raise ExecutionStateArgumentError(f"{name} is malformed")
    return value


def _reason(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or _REASON_PATTERN.fullmatch(value) is None:
        raise ExecutionStateArgumentError(f"{name} is malformed")
    return value


def _check_size(value: str, *, name: str, limit: int = _MAX_ARTIFACT_BYTES) -> None:
    if len(value.encode("utf-8")) > limit:
        raise ExecutionStateArgumentError(f"{name} exceeds the contracted bound")


def _is_busy(exc: sqlite3.Error) -> bool:
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


def _sqlite_runtime_version() -> tuple[int, int, int]:
    """Return the runtime SQLite version or fail closed."""
    raw = getattr(sqlite3, "sqlite_version_info", None)
    if not isinstance(raw, tuple) or len(raw) < 3:
        raise ExecutionStatePathError("SQLite runtime version is unavailable")
    try:
        version = tuple(int(part) for part in raw[:3])
    except (TypeError, ValueError) as exc:
        raise ExecutionStatePathError(
            "SQLite runtime version is malformed"
        ) from exc
    if len(version) != 3 or version < _MIN_SQLITE_VERSION:
        raise ExecutionStatePathError(
            "SQLite runtime is older than the contracted minimum"
        )
    return version


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


def _schema_sql_tokens(sql: Any) -> tuple[str, ...]:
    """Tokenize the small, generated EXECSTATE schema SQL grammar.

    Whitespace is not semantic, but quoted strings/identifiers and operators
    are preserved exactly so a formatting relaxation cannot weaken schema
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
            while index < length and (sql[index].isalnum() or sql[index] in "_$"):
                index += 1
            tokens.append(sql[start:index].lower())
            continue
        if char.isdigit() or (
            char == "." and index + 1 < length and sql[index + 1].isdigit()
        ):
            start = index
            index += 1
            while index < length and (sql[index].isalnum() or sql[index] in ".+-"):
                index += 1
            tokens.append(sql[start:index])
            continue
        matched = next(
            (token for token in _SQL_MULTI_CHAR_TOKENS if sql.startswith(token, index)),
            None,
        )
        if matched is not None:
            tokens.append(matched)
            index += len(matched)
            continue
        tokens.append(char)
        index += 1
    return tuple(tokens)


def _expected_table_sql() -> dict[str, tuple[str, ...]]:
    expected: dict[str, tuple[str, ...]] = {}
    for statement in _SCHEMA_STATEMENTS:
        tokens = _schema_sql_tokens(statement)
        if len(tokens) >= 3 and tokens[:2] == ("create", "table"):
            expected[tokens[2]] = tokens
    return expected


_EXPECTED_TABLE_SQL = _expected_table_sql()


def _foreign_key_topology(
    connection: sqlite3.Connection, table: str
) -> frozenset[_ForeignKeySpec]:
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        grouped.setdefault(int(row["id"]), []).append(row)
    topology: set[_ForeignKeySpec] = set()
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: int(row["seq"]))
        first = ordered[0]
        topology.add(
            (
                str(first["table"]),
                tuple((str(row["from"]), str(row["to"])) for row in ordered),
                str(first["on_update"]),
                str(first["on_delete"]),
                str(first["match"]),
            )
        )
    return frozenset(topology)


def _next_sequence(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
    ).fetchone()
    return 1 if row is None else int(row[0]) + 1


def _next_audit_sequence(connection: sqlite3.Connection) -> int:
    """Contract allocation: one shared append-only audit ordering domain."""
    row = connection.execute(
        "SELECT COALESCE(MAX(event_sequence), 0) + 1 FROM audit_events"
    ).fetchone()
    return int(row[0])


def _next_run_sequence(connection: sqlite3.Connection) -> int:
    """Contract allocation: exact committed run key before anchor insertion."""
    row = connection.execute(
        "SELECT COALESCE(MAX(run_sequence), 0) + 1 FROM recovery_runs"
    ).fetchone()
    return int(row[0])


def _metadata_int(
    connection: sqlite3.Connection, key: str, *, minimum: int
) -> int:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = ?", (key,)
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise ExecutionStateSchemaError(f"missing metadata counter {key!r}")
    try:
        value = int(row[0])
    except ValueError as exc:
        raise ExecutionStateCorruptionError(
            f"metadata counter {key!r} is not an integer"
        ) from exc
    if value < minimum or value > _INT64_MAX:
        raise ExecutionStateCorruptionError(
            f"metadata counter {key!r} is outside the contracted range"
        )
    return value


def _set_metadata_int(
    connection: sqlite3.Connection, key: str, value: int
) -> None:
    connection.execute(
        "UPDATE metadata SET value = ? WHERE key = ?", (str(value), key)
    )


def _validate_database_path(raw_path: Any) -> Path:
    if isinstance(raw_path, bool) or not isinstance(raw_path, (str, os.PathLike)):
        raise TypeError("path must be a str or pathlib.Path")
    raw = os.fspath(raw_path)
    if not isinstance(raw, str):
        raise TypeError("path must be a str or pathlib.Path")
    if "\x00" in raw:
        raise ExecutionStatePathError("database path contains NUL")
    if not raw or raw == ":memory:" or raw.lower().startswith("file:"):
        raise ExecutionStatePathError("database path must be an explicit local file")
    if len(raw) > _MAX_PATH_CHARS:
        raise ExecutionStatePathError("database path is too long")
    original = Path(raw)
    if not original.is_absolute():
        raise ExecutionStatePathError("database path must be absolute")
    if original.is_symlink():
        raise ExecutionStatePathError("database path must not be a symlink")
    try:
        resolved = original.resolve(strict=False)
    except OSError as exc:
        raise ExecutionStatePathError("database path cannot be resolved") from exc
    if not resolved.is_absolute():
        raise ExecutionStatePathError("database path must be absolute")
    if str(resolved).startswith("\\\\") or str(resolved).startswith("//"):
        raise ExecutionStatePathError("UNC/network database paths are not supported")
    parent = resolved.parent
    if not parent.exists() or not parent.is_dir():
        raise ExecutionStatePathError(
            "database parent directory must already exist and be a directory"
        )
    if hasattr(original, "is_junction") and original.is_junction():
        raise ExecutionStatePathError("database path must not be a junction")
    drive, tail = os.path.splitdrive(str(resolved))
    if os.name == "nt":
        if not drive:
            raise ExecutionStatePathError("database path must name a local drive")
        if ":" in tail:
            raise ExecutionStatePathError(
                "alternate data stream paths are not supported"
            )
    for part in resolved.parts:
        stem = part.split(".", 1)[0].upper()
        if stem in _RESERVED_WINDOWS_NAMES:
            raise ExecutionStatePathError(
                "Windows reserved device names are not supported"
            )
    if resolved.exists():
        if not resolved.is_file():
            raise ExecutionStatePathError("database path is not a regular file")
        if resolved.stat().st_size == 0:
            raise ExecutionStateCorruptionError(
                "existing zero-byte database fails closed"
            )
    return resolved


class SQLiteExecutionStateStore:
    """SQLite-backed durable execution-state ledger."""

    def __init__(self, path: str | Path) -> None:
        self._path = _validate_database_path(path)
        _sqlite_runtime_version()
        file_existed = self._path.exists()
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
            raise ExecutionStatePathError("database could not be opened") from exc
        self._connection.row_factory = sqlite3.Row
        self._closed = False
        self._recovered = False
        self._current_epoch: int | None = None
        self._commit_outcome_unknown = False
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

    def _require_open(self) -> None:
        if self._closed:
            raise ExecutionStateArgumentError("execution state store is closed")
        if self._commit_outcome_unknown:
            raise ExecutionStateCommitUnknownError(
                "commit outcome is unknown; reopen the database to inspect it"
            )

    def _require_recovered(self) -> None:
        self._require_open()
        if not self._recovered:
            raise ExecutionStateRecoveryRequiredError(
                "startup recovery must complete before this operation"
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
                    raise ExecutionStateBusyError(
                        "database is busy during WAL configuration"
                    ) from exc
                raise
            if mode is None or str(mode[0]).lower() != "wal":
                raise ExecutionStatePathError("SQLite WAL mode is unavailable")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA busy_timeout = 0")
            self._connection.execute("PRAGMA locking_mode = NORMAL")
            journal_mode = self._connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()
            foreign_keys = self._connection.execute("PRAGMA foreign_keys").fetchone()
            synchronous = self._connection.execute("PRAGMA synchronous").fetchone()
            busy_timeout = self._connection.execute("PRAGMA busy_timeout").fetchone()
            locking_mode = self._connection.execute("PRAGMA locking_mode").fetchone()
            if journal_mode is None or str(journal_mode[0]).lower() != "wal":
                raise ExecutionStatePathError("SQLite WAL mode is unavailable")
            if foreign_keys is None or int(foreign_keys[0]) != 1:
                raise ExecutionStatePathError("SQLite foreign keys are unavailable")
            if synchronous is None or int(synchronous[0]) != 2:
                raise ExecutionStatePathError("SQLite synchronous=FULL is unavailable")
            if busy_timeout is None or int(busy_timeout[0]) != 0:
                raise ExecutionStatePathError("SQLite busy_timeout is not zero")
            if locking_mode is None or str(locking_mode[0]).lower() != "normal":
                raise ExecutionStatePathError("SQLite locking_mode is not NORMAL")
        except ExecutionStateError:
            raise
        except sqlite3.Error as exc:
            raise ExecutionStatePathError(
                "SQLite connection configuration failed"
            ) from exc

    def _initialize_or_verify_schema(self, *, file_existed: bool) -> None:
        try:
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error as exc:
            raise ExecutionStateSchemaError("schema version cannot be read") from exc
        if version == 0:
            if file_existed:
                raise ExecutionStateSchemaError(
                    "existing database has no EXECSTATE schema"
                )
            self._create_schema()
            return
        if version != _SCHEMA_VERSION:
            raise ExecutionStateSchemaError(
                f"unsupported EXECSTATE schema version {version}"
            )
        self._verify_schema()

    def _create_schema(self) -> None:
        try:
            self._connection.execute("BEGIN EXCLUSIVE")
            for statement in _SCHEMA_STATEMENTS:
                self._connection.execute(statement)
            self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            try:
                self._connection.commit()
            except BaseException as exc:
                self._commit_outcome_unknown = True
                raise ExecutionStateCommitUnknownError(
                    "schema initialization commit outcome is unknown"
                ) from exc
        except ExecutionStateCommitUnknownError:
            raise
        except sqlite3.Error as exc:
            self._rollback()
            raise ExecutionStateSchemaError(
                "EXECSTATE schema initialization failed"
            ) from exc
        self._verify_schema()

    def _verify_schema(self) -> None:
        try:
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version != _SCHEMA_VERSION:
                raise ExecutionStateSchemaError(
                    f"unsupported EXECSTATE schema version {version}"
                )
            rows = self._connection.execute(
                "SELECT key, value FROM metadata"
            ).fetchall()
            metadata = {str(row[0]): str(row[1]) for row in rows}
            if metadata.get("schema_name") != _SCHEMA_NAME:
                raise ExecutionStateSchemaError("EXECSTATE schema name mismatch")
            if metadata.get("schema_version") != str(_SCHEMA_VERSION):
                raise ExecutionStateSchemaError("EXECSTATE schema version mismatch")
            expected_tables = {
                "metadata",
                "submissions",
                "commit_receipts",
                "claims",
                "operational_payloads",
                "recovery_runs",
                "audit_events",
            }
            actual_tables = {
                str(row[0])
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing = expected_tables - actual_tables
            if missing:
                raise ExecutionStateSchemaError(
                    f"missing EXECSTATE tables: {sorted(missing)!r}"
                )
            table_sql = {
                str(row["name"]): _schema_sql_tokens(row["sql"])
                for row in self._connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for table, expected_table_sql in _EXPECTED_TABLE_SQL.items():
                if table_sql.get(table) != expected_table_sql:
                    raise ExecutionStateSchemaError(
                        f"invalid EXECSTATE table definition: {table}"
                    )
            required_objects = {
                "index": {
                    "submissions_one_active_claim": (
                        "create unique index submissions_one_active_claim on "
                        "submissions(active_claim_id) where active_claim_id is not null"
                    ),
                    "commit_receipts_lookup": (
                        "create index commit_receipts_lookup on "
                        "commit_receipts(record_id, recovery_epoch)"
                    ),
                    "claims_lookup": (
                        "create index claims_lookup on claims(record_id, recovery_epoch)"
                    ),
                    "audit_events_lookup": (
                        "create index audit_events_lookup on "
                        "audit_events(submission_id, target_json, event_sequence)"
                    ),
                    "audit_events_boundary_run_phase": (
                        "create unique index audit_events_boundary_run_phase on "
                        "audit_events(boundary_run_sequence, boundary_phase) where "
                        "boundary_run_sequence is not null"
                    ),
                },
                "trigger": {
                    "commit_receipts_no_update": (
                        "create trigger commit_receipts_no_update before update on "
                        "commit_receipts begin select raise(abort, 'commit receipts are immutable'); end"
                    ),
                    "commit_receipts_no_delete": (
                        "create trigger commit_receipts_no_delete before delete on "
                        "commit_receipts begin select raise(abort, 'commit receipts are immutable'); end"
                    ),
                    "claims_no_update": (
                        "create trigger claims_no_update before update on claims begin "
                        "select raise(abort, 'claims are immutable'); end"
                    ),
                    "claims_no_delete": (
                        "create trigger claims_no_delete before delete on claims begin "
                        "select raise(abort, 'claims are immutable'); end"
                    ),
                    "operational_payloads_no_update": (
                        "create trigger operational_payloads_no_update before update on "
                        "operational_payloads begin select raise(abort, 'operational payloads are append-only'); end"
                    ),
                    "operational_payloads_no_delete": (
                        "create trigger operational_payloads_no_delete before delete on "
                        "operational_payloads begin select raise(abort, 'operational payloads are append-only'); end"
                    ),
                    "recovery_runs_no_update": (
                        "create trigger recovery_runs_no_update before update on recovery_runs begin "
                        "select raise(abort, 'recovery runs are append-only'); end"
                    ),
                    "recovery_runs_no_delete": (
                        "create trigger recovery_runs_no_delete before delete on recovery_runs begin "
                        "select raise(abort, 'recovery runs are append-only'); end"
                    ),
                    "audit_events_no_update": (
                        "create trigger audit_events_no_update before update on audit_events begin "
                        "select raise(abort, 'audit events are append-only'); end"
                    ),
                    "audit_events_no_delete": (
                        "create trigger audit_events_no_delete before delete on audit_events begin "
                        "select raise(abort, 'audit events are append-only'); end"
                    ),
                    "submissions_no_delete": (
                        "create trigger submissions_no_delete before delete on submissions begin "
                        "select raise(abort, 'submissions are immutable roots'); end"
                    ),
                    "recovery_runs_require_cut": (
                        "create trigger recovery_runs_require_cut before insert on "
                        "recovery_runs for each row begin select raise(abort, "
                        "'recovery run requires exactly one BEGIN and one END cut anchor') "
                        "where (select count(*) from audit_events where "
                        "boundary_run_sequence = new.run_sequence) <> 2 or (select "
                        "count(*) from audit_events where boundary_run_sequence = "
                        "new.run_sequence and boundary_phase = 'BEGIN' and event_kind = "
                        "'RECOVERY_CUT_BEGIN' and reason = 'RECOVERY_CUT_BEGIN' and "
                        "target_json = new.target_json and recovery_epoch = "
                        "new.recovery_epoch and observed_at = new.observed_at) <> 1 or "
                        "(select count(*) from audit_events where boundary_run_sequence = "
                        "new.run_sequence and boundary_phase = 'END' and event_kind = "
                        "'RECOVERY_CUT_END' and reason = 'RECOVERY_CUT_END' and "
                        "target_json = new.target_json and recovery_epoch = "
                        "new.recovery_epoch and observed_at = new.observed_at) <> 1 or "
                        "(select event_sequence from audit_events where "
                        "boundary_run_sequence = new.run_sequence and boundary_phase = "
                        "'BEGIN') >= (select event_sequence from audit_events where "
                        "boundary_run_sequence = new.run_sequence and boundary_phase = "
                        "'END'); end"
                    ),
                },
            }
            objects = {
                (str(row["type"]), str(row["name"])): _schema_sql_tokens(row["sql"])
                for row in self._connection.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE type IN ('index', 'trigger')"
                ).fetchall()
            }
            for object_type, expected in required_objects.items():
                for name, expected_sql in expected.items():
                    actual_object_sql = objects.get((object_type, name))
                    if actual_object_sql != _schema_sql_tokens(expected_sql):
                        raise ExecutionStateSchemaError(
                            f"missing or invalid EXECSTATE {object_type}: {name}"
                        )
            for table, expected_topology in _EXPECTED_FOREIGN_KEYS.items():
                if (
                    _foreign_key_topology(self._connection, table)
                    != expected_topology
                ):
                    raise ExecutionStateSchemaError(
                        f"invalid EXECSTATE foreign-key topology: {table}"
                    )
            submissions_row = self._connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'submissions'"
            ).fetchone()
            submissions_sql = (
                () if submissions_row is None else _schema_sql_tokens(submissions_row[0])
            )
            audit_events_row = self._connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'audit_events'"
            ).fetchone()
            audit_events_sql = (
                () if audit_events_row is None else _schema_sql_tokens(audit_events_row[0])
            )
            deferred_references = (
                _schema_sql_tokens(
                    "current_receipt_id text references "
                    "commit_receipts(receipt_id) deferrable initially deferred"
                ),
                _schema_sql_tokens(
                    "active_claim_id text references claims(claim_id) "
                    "deferrable initially deferred"
                ),
            )

            def contains_tokens(
                haystack: tuple[str, ...], needle: tuple[str, ...]
            ) -> bool:
                width = len(needle)
                return any(
                    haystack[index : index + width] == needle
                    for index in range(len(haystack) - width + 1)
                )

            if any(
                not contains_tokens(submissions_sql, fragment)
                for fragment in deferred_references
            ):
                raise ExecutionStateSchemaError(
                    "invalid EXECSTATE deferred foreign-key topology"
                )
            boundary_fk = _schema_sql_tokens(
                "boundary_run_sequence integer references "
                "recovery_runs(run_sequence) deferrable initially deferred"
            )
            if not contains_tokens(audit_events_sql, boundary_fk):
                raise ExecutionStateSchemaError(
                    "invalid EXECSTATE boundary deferred foreign-key topology"
                )
            integrity = self._connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise ExecutionStateCorruptionError("SQLite integrity_check failed")
            if self._connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ExecutionStateCorruptionError("SQLite foreign_key_check failed")
        except ExecutionStateError:
            raise
        except sqlite3.Error as exc:
            raise ExecutionStateSchemaError("EXECSTATE schema verification failed") from exc

    def _begin_immediate(self) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError(
                "could not begin a durable transaction"
            ) from exc

    def _begin_read(self) -> None:
        try:
            self._connection.execute("BEGIN")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError(
                "could not begin a read transaction"
            ) from exc

    def _commit(self) -> None:
        try:
            self._commit_connection()
        except BaseException as exc:
            self._commit_outcome_unknown = True
            raise ExecutionStateCommitUnknownError(
                "commit outcome is unknown"
            ) from exc
        else:
            self._commit_outcome_unknown = False

    def _commit_connection(self) -> None:
        """Commit seam kept private so tests can deterministically fault it."""
        self._connection.commit()

    def _rollback(self) -> None:
        if self._commit_outcome_unknown:
            return
        try:
            self._connection.rollback()
        except sqlite3.Error:
            pass

    def _allocate_generation(self) -> int:
        value = _metadata_int(self._connection, "next_generation", minimum=1)
        if value >= _INT64_MAX:
            raise ExecutionStateArgumentError("generation space is exhausted")
        _set_metadata_int(self._connection, "next_generation", value + 1)
        return value

    def _require_current_recovery_epoch(self) -> int:
        epoch = self._current_epoch
        if epoch is None:
            raise ExecutionStateRecoveryRequiredError(
                "startup recovery must complete before this operation"
            )
        next_epoch = _metadata_int(
            self._connection, "next_recovery_epoch", minimum=1
        )
        durable_epoch = next_epoch - 1
        if durable_epoch < 1 or epoch != durable_epoch:
            raise ExecutionStateDeniedError("store recovery epoch is stale")
        allocating_run = self._connection.execute(
            "SELECT 1 FROM recovery_runs WHERE recovery_epoch = ? LIMIT 1",
            (epoch,),
        ).fetchone()
        if allocating_run is None:
            raise ExecutionStateRecoveryRequiredError(
                "current recovery epoch has no allocating run"
            )
        return epoch

    def _insert_receipt(
        self,
        *,
        record_id: str,
        submission_id: str,
        target: ExecutionTargetBinding,
        generation: int,
        recovery_epoch: int,
        committed_at: datetime,
    ) -> DurableCommitReceipt:
        sequence = _next_sequence(self._connection, "commit_receipts")
        receipt_id = f"execstate-receipt-v1-{sequence}"
        receipt = DurableCommitReceipt(
            schema="frontier-commit-receipt-v1",
            receipt_id=receipt_id,
            record_id=record_id,
            submission_id=submission_id,
            target=target,
            generation=generation,
            recovery_epoch=recovery_epoch,
            committed=True,
            committed_at=committed_at,
        )
        receipt_json = receipt.model_dump_json()
        _check_size(receipt_json, name="receipt_json")
        self._connection.execute(
            """
            INSERT INTO commit_receipts(
                receipt_sequence, receipt_id, record_id, submission_id,
                target_json, generation, recovery_epoch, receipt_json,
                committed, committed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                sequence,
                receipt_id,
                receipt.record_id,
                receipt.submission_id,
                receipt.target.model_dump_json(),
                receipt.generation,
                receipt.recovery_epoch,
                receipt_json,
                _canonical_instant(receipt.committed_at),
            ),
        )
        return receipt

    def _write_audit(
        self,
        *,
        submission_id: str,
        target_json: str,
        prior_state: StateLiteral | None,
        new_state: StateLiteral,
        generation: int,
        recovery_epoch: int,
        event_kind: str,
        reason: str,
        observed_at: datetime,
        owner_id: str | None = None,
        token_reference: str | None = None,
        claim_id: str | None = None,
        payload_sequence: int | None = None,
        status: str | None = None,
    ) -> int:
        if event_kind not in _EVENT_KINDS:
            raise ExecutionStateArgumentError("unsupported event_kind")
        if (event_kind, reason) not in _REASON_PAIRS:
            raise ExecutionStateArgumentError(
                "unsupported event_kind/reason combination"
            )
        if new_state not in _STATES:
            raise ExecutionStateArgumentError("unsupported new state")
        if prior_state is not None and prior_state not in _STATES:
            raise ExecutionStateArgumentError("unsupported prior state")
        if owner_id is not None:
            _opaque(owner_id, name="owner_id")
        if status is not None:
            _reason(status, name="status")
        sequence = _next_audit_sequence(self._connection)
        event = {
            "event_sequence": sequence,
            "row_scope": _ROW_SCOPE_SUBMISSION,
            "submission_id": submission_id,
            "target_json": target_json,
            "prior_state": prior_state,
            "new_state": new_state,
            "generation": generation,
            "recovery_epoch": recovery_epoch,
            "event_kind": event_kind,
            "observed_at": _canonical_instant(observed_at),
            "owner_id": owner_id,
            "token_reference": token_reference,
            "claim_id": claim_id,
            "payload_sequence": payload_sequence,
            "status": status,
            "reason": reason,
        }
        event_json = _canonical_json(event)
        self._connection.execute(
            """
            INSERT INTO audit_events(
                event_sequence, row_scope, submission_id, target_json,
                prior_state, new_state, generation, recovery_epoch, event_kind,
                reason, observed_at, owner_id, token_reference, claim_id,
                payload_sequence, status, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                _ROW_SCOPE_SUBMISSION,
                submission_id,
                target_json,
                prior_state,
                new_state,
                generation,
                recovery_epoch,
                event_kind,
                reason,
                event["observed_at"],
                owner_id,
                token_reference,
                claim_id,
                payload_sequence,
                status,
                event_json,
            ),
        )
        return sequence

    def _write_boundary_anchor(
        self,
        *,
        run_sequence: int,
        target_json: str,
        recovery_epoch: int,
        observed_at: datetime,
        phase: str,
    ) -> int:
        if phase not in _CUT_PHASES:
            raise ExecutionStateArgumentError("unsupported boundary phase")
        if run_sequence < 1 or run_sequence > _INT64_MAX:
            raise ExecutionStateArgumentError("boundary run sequence is invalid")
        if recovery_epoch < 1 or recovery_epoch > _INT64_MAX:
            raise ExecutionStateArgumentError("boundary recovery epoch is invalid")
        event_kind = _CUT_BEGIN_KIND if phase == "BEGIN" else _CUT_END_KIND
        sequence = _next_audit_sequence(self._connection)
        event = {
            "event_sequence": sequence,
            "row_scope": _ROW_SCOPE_BOUNDARY,
            "target_json": target_json,
            "recovery_epoch": recovery_epoch,
            "event_kind": event_kind,
            "boundary_phase": phase,
            "boundary_run_sequence": run_sequence,
            "observed_at": _canonical_instant(observed_at),
            "reason": event_kind,
        }
        self._connection.execute(
            """
            INSERT INTO audit_events(
                event_sequence, row_scope, submission_id, target_json,
                prior_state, new_state, generation, recovery_epoch, event_kind,
                reason, observed_at, owner_id, token_reference, claim_id,
                payload_sequence, status, boundary_run_sequence, boundary_phase,
                event_json
            ) VALUES (?, ?, NULL, ?, NULL, NULL, NULL, ?, ?, ?, ?, NULL, NULL,
                      NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                sequence,
                _ROW_SCOPE_BOUNDARY,
                target_json,
                recovery_epoch,
                event_kind,
                event_kind,
                event["observed_at"],
                run_sequence,
                phase,
                _canonical_json(event),
            ),
        )
        return sequence

    def _load_submission_row(
        self, submission_id: str, target_json: str
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT * FROM submissions
            WHERE submission_id = ? AND target_json = ?
            """,
            (submission_id, target_json),
        ).fetchone()

    def _decode_record(self, row: sqlite3.Row) -> DurableSubmissionRecord:
        try:
            record = DurableSubmissionRecord.from_json(str(row["record_json"]))
        except (ValueError, TypeError) as exc:
            raise ExecutionStateCorruptionError(
                "stored durable record is malformed"
            ) from exc
        if (
            record.record_id != row["record_id"]
            or record.submission_id != row["submission_id"]
            or record.provenance_id != row["provenance_id"]
            or record.target.model_dump_json() != row["target_json"]
            or record.generation != row["generation"]
            or record.recovery_epoch != row["recovery_epoch"]
            or record.state != row["state"]
            or _canonical_instant(record.created_at) != row["created_at"]
            or _canonical_instant(record.updated_at) != row["updated_at"]
        ):
            raise ExecutionStateCorruptionError(
                "stored record contradicts its extracted columns"
            )
        return record

    def _decode_envelope_provenance(
        self, row: sqlite3.Row
    ) -> tuple[BrokerSubmissionEnvelope, FreshRiskProvenance]:
        try:
            envelope = BrokerSubmissionEnvelope.from_json(str(row["envelope_json"]))
            provenance = FreshRiskProvenance.from_json(str(row["provenance_json"]))
        except (TypeError, ValueError) as exc:
            raise ExecutionStateCorruptionError(
                "stored envelope/provenance is malformed"
            ) from exc
        if (
            envelope.model_dump_json() != row["envelope_json"]
            or provenance.model_dump_json() != row["provenance_json"]
            or envelope.submission_id != row["submission_id"]
            or provenance.provenance_id != row["provenance_id"]
            or provenance.pre_dispatch_decision.envelope != envelope
        ):
            raise ExecutionStateCorruptionError(
                "stored envelope/provenance contradicts the submission"
            )
        return envelope, provenance

    def _decode_receipt(
        self, receipt_id: str, *, record: DurableSubmissionRecord
    ) -> DurableCommitReceipt:
        row = self._connection.execute(
            "SELECT * FROM commit_receipts WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise ExecutionStateCorruptionError("current commit receipt is missing")
        return self._validate_receipt_row(row, record, current=True)

    def _validate_receipt_row(
        self,
        row: sqlite3.Row,
        record: DurableSubmissionRecord,
        *,
        current: bool,
    ) -> DurableCommitReceipt:
        try:
            receipt = DurableCommitReceipt.from_json(str(row["receipt_json"]))
        except (ValueError, TypeError) as exc:
            raise ExecutionStateCorruptionError(
                "stored commit receipt is malformed"
            ) from exc
        if (
            receipt.model_dump_json() != row["receipt_json"]
            or receipt.receipt_id != row["receipt_id"]
            or receipt.receipt_id
            != f"execstate-receipt-v1-{row['receipt_sequence']}"
            or receipt.record_id != record.record_id
            or receipt.record_id != row["record_id"]
            or receipt.submission_id != record.submission_id
            or receipt.submission_id != row["submission_id"]
            or receipt.target != record.target
            or receipt.target.model_dump_json() != row["target_json"]
            or receipt.generation != record.generation
            or receipt.generation != row["generation"]
            or receipt.recovery_epoch != row["recovery_epoch"]
            or receipt.recovery_epoch > record.recovery_epoch
            or receipt.committed is not True
            or row["committed"] != 1
            or _canonical_instant(receipt.committed_at) != row["committed_at"]
            or receipt.committed_at < record.created_at
            or receipt.committed_at > record.updated_at
        ):
            raise ExecutionStateCorruptionError(
                "stored receipt contradicts the durable record"
            )
        if current and receipt.committed_at != record.updated_at:
            raise ExecutionStateCorruptionError(
                "current receipt timestamp contradicts the durable record"
            )
        if current and receipt.recovery_epoch != record.recovery_epoch:
            raise ExecutionStateCorruptionError(
                "current receipt epoch contradicts the durable record"
            )
        return receipt

    def _decode_claim(
        self, claim_id: str, *, record: DurableSubmissionRecord
    ) -> AtomicDispatchClaimEvidence:
        row = self._connection.execute(
            "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            raise ExecutionStateCorruptionError("active claim row is missing")
        return self._validate_claim_row(row, record)

    def _validate_claim_row(
        self, row: sqlite3.Row, record: DurableSubmissionRecord
    ) -> AtomicDispatchClaimEvidence:
        try:
            claim = AtomicDispatchClaimEvidence.from_json(str(row["claim_json"]))
        except (ValueError, TypeError) as exc:
            raise ExecutionStateCorruptionError("stored claim is malformed") from exc
        if claim.expires_at is None:
            raise ExecutionStateCorruptionError("stored claim has no expiry")
        if (
            claim.claim_id != row["claim_id"]
            or claim.claim_id != f"execstate-claim-v1-{row['claim_sequence']}"
            or claim.record_id != record.record_id
            or claim.record_id != row["record_id"]
            or claim.submission_id != record.submission_id
            or claim.submission_id != row["submission_id"]
            or claim.target != record.target
            or claim.target.model_dump_json() != row["target_json"]
            or claim.generation != record.generation
            or claim.generation != row["generation"]
            or claim.recovery_epoch != row["recovery_epoch"]
            or claim.recovery_epoch > record.recovery_epoch
            or claim.owner_id != row["owner_id"]
            or claim.token != row["token"]
            or claim.status != row["status"]
            or _canonical_instant(claim.acquired_at) != row["acquired_at"]
            or _canonical_instant(claim.expires_at) != row["expires_at"]
            or claim.expires_at <= claim.acquired_at
        ):
            raise ExecutionStateCorruptionError(
                "stored claim contradicts its extracted columns"
            )
        return claim

    def _load_recovery_evidence(self, evidence_id: str) -> RecoveryCompleteEvidence:
        row = self._connection.execute(
            "SELECT * FROM recovery_runs WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise ExecutionStateDeniedError(
                "recovery evidence is not committed in this database"
            )
        evidence = self._validate_recovery_row(row)
        self._validate_run_cut(row)
        return evidence

    def _validate_recovery_row(
        self, row: sqlite3.Row
    ) -> RecoveryCompleteEvidence:
        try:
            evidence = RecoveryCompleteEvidence.from_json(str(row["evidence_json"]))
        except (ValueError, TypeError) as exc:
            raise ExecutionStateCorruptionError(
                "stored recovery evidence is malformed"
            ) from exc
        if (
            evidence.evidence_id != row["evidence_id"]
            or evidence.evidence_id
            != f"execstate-recovery-v1-{row['run_sequence']}"
            or evidence.target.model_dump_json() != row["target_json"]
            or evidence.recovery_epoch != row["recovery_epoch"]
            or evidence.scanned_through_generation
            != row["scanned_through_generation"]
            or evidence.status != row["status"]
            or _canonical_instant(evidence.observed_at) != row["observed_at"]
        ):
            raise ExecutionStateCorruptionError(
                "stored recovery evidence contradicts its extracted columns"
            )
        return evidence

    def _decode_canonical_payload(
        self, payload_json: Any, *, name: str, fields: frozenset[str]
    ) -> dict[str, Any]:
        if not isinstance(payload_json, str):
            raise ExecutionStateCorruptionError(f"{name} is missing")
        try:
            document = json.loads(payload_json)
        except (TypeError, ValueError) as exc:
            raise ExecutionStateCorruptionError(f"{name} is malformed") from exc
        if (
            not isinstance(document, dict)
            or set(document) != fields
            or _canonical_json(document) != payload_json
        ):
            raise ExecutionStateCorruptionError(f"{name} is not canonical")
        return document

    def _validate_payload_row(
        self, row: sqlite3.Row, record: DurableSubmissionRecord
    ) -> dict[str, Any]:
        target_json = record.target.model_dump_json()
        if (
            row["record_id"] != record.record_id
            or row["submission_id"] != record.submission_id
            or row["target_json"] != target_json
        ):
            raise ExecutionStateCorruptionError(
                "operational payload contradicts its owning submission"
            )
        _parse_instant(row["observed_at"], name="payload observed_at")
        kind = row["payload_kind"]
        status: str
        if kind == "TRANSPORT_OUTCOME":
            try:
                outcome = BrokerSubmissionOutcome.from_json(str(row["payload_json"]))
            except (TypeError, ValueError) as exc:
                raise ExecutionStateCorruptionError(
                    "stored transport outcome is malformed"
                ) from exc
            if outcome.submission_id != record.submission_id:
                raise ExecutionStateCorruptionError(
                    "stored transport outcome has a foreign submission identity"
                )
            if outcome.model_dump_json() != row["payload_json"]:
                raise ExecutionStateCorruptionError(
                    "stored transport outcome is not canonical"
                )
            status = outcome.status
        elif kind == "RECONCILIATION":
            document = self._decode_canonical_payload(
                row["payload_json"],
                name="reconciliation payload",
                fields=_RECONCILIATION_FIELDS,
            )
            if (
                document["submission_id"] != record.submission_id
                or document["target_json"] != target_json
                or document["observed_at"] != row["observed_at"]
                or document["status"] not in _RECONCILIATION_STATUSES
            ):
                raise ExecutionStateCorruptionError(
                    "stored reconciliation payload contradicts its submission"
                )
            try:
                _opaque(document["evidence_reference"], name="evidence_reference")
            except ExecutionStateArgumentError as exc:
                raise ExecutionStateCorruptionError(
                    "stored reconciliation reference is malformed"
                ) from exc
            status = str(document["status"])
        elif kind == "PRE_DISPATCH_NO_EFFECT":
            document = self._decode_canonical_payload(
                row["payload_json"],
                name="pre-dispatch no-effect payload",
                fields=_NO_EFFECT_FIELDS,
            )
            if (
                document["classification"] != "BROKER_PRE_DISPATCH_NO_EFFECT"
                or document["submission_id"] != record.submission_id
                or document["target_json"] != target_json
                or document["observed_at"] != row["observed_at"]
            ):
                raise ExecutionStateCorruptionError(
                    "stored no-effect payload contradicts its submission"
                )
            try:
                _opaque(document["proof_reference"], name="proof_reference")
            except ExecutionStateArgumentError as exc:
                raise ExecutionStateCorruptionError(
                    "stored no-effect reference is malformed"
                ) from exc
            status = "NO_EFFECT_CONFIRMED"
        else:
            raise ExecutionStateCorruptionError(
                "operational payload kind is not contracted"
            )
        return {
            "sequence": int(row["payload_sequence"]),
            "kind": str(kind),
            "status": status,
            "observed_at": row["observed_at"],
        }

    def _validate_audit_rows(
        self,
        rows: list[sqlite3.Row],
        record: DurableSubmissionRecord,
        claims: dict[str, AtomicDispatchClaimEvidence],
        payloads: dict[int, dict[str, Any]],
        receipts: list[sqlite3.Row],
        active_claim_id: str | None,
    ) -> None:
        if not rows:
            raise ExecutionStateCorruptionError("submission has no audit history")
        target_json = record.target.model_dump_json()
        referenced: set[int] = set()
        acquired_claim_ids: set[str] = set()
        revoked_claim_ids: set[str] = set()
        active_claim_chain: str | None = None
        previous_state: str | None = None
        previous_epoch: int | None = None
        receipt_points = {
            (int(row["recovery_epoch"]), str(row["committed_at"]))
            for row in receipts
        }
        recovery_points = {
            (int(row["recovery_epoch"]), str(row["observed_at"]))
            for row in self._connection.execute(
                "SELECT recovery_epoch, observed_at FROM recovery_runs"
            ).fetchall()
        }
        for index, row in enumerate(rows):
            self._verify_audit_row(row)
            if (
                row["submission_id"] != record.submission_id
                or row["target_json"] != target_json
                or row["generation"] != record.generation
                or not 1 <= row["recovery_epoch"] <= record.recovery_epoch
            ):
                raise ExecutionStateCorruptionError(
                    "audit event contradicts its owning submission"
                )
            prior_state = row["prior_state"]
            if index == 0:
                if (
                    prior_state is not None
                    or row["new_state"] != "PREPARED"
                    or row["event_kind"] != "PREPARED"
                    or row["reason"] != "NEW_PREPARE"
                ):
                    raise ExecutionStateCorruptionError(
                        "audit history does not start with the prepare transition"
                    )
                if row["observed_at"] != _canonical_instant(record.created_at):
                    raise ExecutionStateCorruptionError(
                        "prepare audit timestamp contradicts the durable prepare"
                    )
            elif prior_state != previous_state:
                raise ExecutionStateCorruptionError(
                    "audit history is not a contiguous state chain"
                )
            previous_state = row["new_state"]
            pair = (str(row["event_kind"]), str(row["reason"]))
            semantics = _AUDIT_SEMANTICS[pair]
            if (prior_state, str(row["new_state"])) not in semantics.states:
                raise ExecutionStateCorruptionError(
                    "audit event contradicts its operation-specific state semantics"
                )
            event_epoch = int(row["recovery_epoch"])
            if previous_epoch is not None:
                if event_epoch < previous_epoch:
                    raise ExecutionStateCorruptionError(
                        "audit recovery epoch moves backwards"
                    )
                if pair == (
                    "RECOVERY_EPOCH_REBIND",
                    "RECOVERY_EPOCH_REBIND",
                ):
                    if event_epoch <= previous_epoch:
                        raise ExecutionStateCorruptionError(
                            "recovery rebind does not advance the audit epoch"
                        )
                elif event_epoch != previous_epoch:
                    raise ExecutionStateCorruptionError(
                        "audit epoch changes without a recovery rebind"
                    )
            previous_epoch = event_epoch

            owner_id = row["owner_id"]
            token_reference = row["token_reference"]
            claim_id = row["claim_id"]
            claim_profiles = frozenset(
                {"claim", "claim_conflict", "payload_claim"}
            )
            if semantics.fields in claim_profiles:
                if (
                    not isinstance(owner_id, str)
                    or not isinstance(claim_id, str)
                    or token_reference != claim_id
                ):
                    raise ExecutionStateCorruptionError(
                        "claim audit event lacks its exact claim references"
                    )
                try:
                    _opaque(owner_id, name="audit owner_id")
                    _opaque(claim_id, name="audit claim_id")
                except ExecutionStateArgumentError as exc:
                    raise ExecutionStateCorruptionError(
                        "claim audit references are malformed"
                    ) from exc
                claim = claims.get(str(claim_id))
                if claim is None:
                    raise ExecutionStateCorruptionError(
                        "audit event references a missing claim"
                    )
                if (
                    semantics.fields != "claim_conflict"
                    and owner_id != claim.owner_id
                ):
                    raise ExecutionStateCorruptionError(
                        "audit event owner contradicts its claim"
                    )
                if claim.generation != row["generation"]:
                    raise ExecutionStateCorruptionError(
                        "audit event generation contradicts its claim"
                    )
                if pair == ("CLAIM_REVOKED", "CLAIM_REVOKED"):
                    observed_at = _parse_instant(
                        row["observed_at"], name="claim revocation observed_at"
                    )
                    if (
                        claim.recovery_epoch > event_epoch
                        or (
                            claim.recovery_epoch == event_epoch
                            and claim.expires_at is not None
                            and claim.expires_at > observed_at
                        )
                    ):
                        raise ExecutionStateCorruptionError(
                            "claim revocation has no stale or expired claim"
                        )
                elif claim.recovery_epoch != event_epoch:
                    raise ExecutionStateCorruptionError(
                        "audit event epoch contradicts its claim"
                    )
                if pair == ("CLAIM_ACQUIRED", "CLAIM_ACQUIRED"):
                    if claim_id in acquired_claim_ids:
                        raise ExecutionStateCorruptionError(
                            "claim acquisition is recorded more than once"
                        )
                    acquired_claim_ids.add(claim_id)
                    if active_claim_chain is not None:
                        raise ExecutionStateCorruptionError(
                            "claim acquisition replaced an active claim"
                        )
                    active_claim_chain = claim_id
                elif pair == ("CLAIM_ACQUIRED", "CLAIM_REPLAY"):
                    if claim_id not in acquired_claim_ids:
                        raise ExecutionStateCorruptionError(
                            "claim replay precedes durable claim acquisition"
                        )
                    if active_claim_chain != claim_id:
                        raise ExecutionStateCorruptionError(
                            "claim replay does not reference the active claim"
                        )
                elif claim_id not in acquired_claim_ids:
                    raise ExecutionStateCorruptionError(
                        "claim audit event precedes durable claim acquisition"
                    )
                if (
                    pair == ("CLAIM_CONFLICT", "CLAIM_CONFLICT")
                    and active_claim_chain != claim_id
                ):
                    raise ExecutionStateCorruptionError(
                        "claim conflict does not reference the active claim"
                    )
                if pair == ("CLAIM_REVOKED", "CLAIM_REVOKED"):
                    if claim_id in revoked_claim_ids:
                        raise ExecutionStateCorruptionError(
                            "claim revocation is recorded more than once"
                        )
                    revoked_claim_ids.add(claim_id)
                    if active_claim_chain != claim_id:
                        raise ExecutionStateCorruptionError(
                            "claim revocation does not reference the active claim"
                        )
                    active_claim_chain = None
                if pair in {
                    ("DISPATCHING", "MARK_DISPATCHING"),
                    ("DISPATCHING", "DISPATCHING_REPLAY"),
                    ("TRANSPORT_OUTCOME", "TRANSPORT_ACCEPTED"),
                    ("TRANSPORT_OUTCOME", "TRANSPORT_REJECTED"),
                    ("TRANSPORT_OUTCOME", "TRANSPORT_OUTCOME_UNKNOWN"),
                    ("PRE_DISPATCH_NO_EFFECT", "PRE_DISPATCH_NO_EFFECT"),
                } and active_claim_chain != claim_id:
                    raise ExecutionStateCorruptionError(
                        "state transition does not reference the active claim"
                    )
                if pair[0] == "CLAIM_ACQUIRED" and row[
                    "observed_at"
                ] != _canonical_instant(claim.acquired_at):
                    raise ExecutionStateCorruptionError(
                        "claim audit timestamp contradicts its durable claim"
                    )
            elif semantics.fields == "payload_owner":
                if (
                    not isinstance(owner_id, str)
                    or token_reference is not None
                    or claim_id is not None
                ):
                    raise ExecutionStateCorruptionError(
                        "payload replay carries invalid claim references"
                    )
                try:
                    _opaque(owner_id, name="audit owner_id")
                except ExecutionStateArgumentError as exc:
                    raise ExecutionStateCorruptionError(
                        "payload replay owner is malformed"
                    ) from exc
            elif (
                owner_id is not None
                or token_reference is not None
                or claim_id is not None
            ):
                raise ExecutionStateCorruptionError(
                    "audit event carries unexpected claim references"
                )

            if semantics.receipt_required and (
                event_epoch,
                str(row["observed_at"]),
            ) not in receipt_points:
                raise ExecutionStateCorruptionError(
                    "audit transition has no corresponding durable receipt"
                )
            if semantics.recovery_event and (
                event_epoch,
                str(row["observed_at"]),
            ) not in recovery_points:
                raise ExecutionStateCorruptionError(
                    "recovery audit event has no corresponding recovery run"
                )
            if pair in {
                ("PREPARED", "NEW_PREPARE"),
                ("PREPARED", "DUPLICATE_PREPARE_REPLAY"),
            } and row["observed_at"] != _canonical_instant(record.created_at):
                raise ExecutionStateCorruptionError(
                    "prepare audit timestamp contradicts the durable prepare"
                )

            expected_kind = _PAYLOAD_KIND_BY_EVENT_KIND.get(str(row["event_kind"]))
            sequence = row["payload_sequence"]
            if expected_kind is None:
                if sequence is not None or row["status"] is not None:
                    raise ExecutionStateCorruptionError(
                        "audit event carries an unexpected payload reference"
                    )
                continue
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                raise ExecutionStateCorruptionError(
                    "audit event lacks its operational payload reference"
                )
            payload = payloads.get(sequence)
            if (
                payload is None
                or payload["kind"] != expected_kind
                or payload["observed_at"] != row["observed_at"]
                or payload["status"] != row["status"]
            ):
                raise ExecutionStateCorruptionError(
                    "audit event contradicts its operational payload"
                )
            reason = str(row["reason"])
            expected_state = _STATE_BY_RECORDED_STATUS.get(str(row["status"]))
            if expected_state is None:
                raise ExecutionStateCorruptionError(
                    "audit event status contradicts its resulting state"
                )
            if reason in _REPLAY_REASONS:
                if sequence not in referenced:
                    raise ExecutionStateCorruptionError(
                        "payload replay precedes its authoritative transition"
                    )
                if row["prior_state"] != row["new_state"]:
                    raise ExecutionStateCorruptionError(
                        "observational replay changes durable state"
                    )
            else:
                if row["new_state"] != expected_state:
                    raise ExecutionStateCorruptionError(
                        "audit event status contradicts its resulting state"
                    )
                if sequence in referenced:
                    raise ExecutionStateCorruptionError(
                        "authoritative transition reuses an operational payload"
                    )
                if reason != _REASON_BY_EVENT_KIND_AND_STATUS.get(
                    (str(row["event_kind"]), str(row["status"]))
                ):
                    raise ExecutionStateCorruptionError(
                        "audit event reason contradicts its recorded status"
                    )
            referenced.add(sequence)
        if previous_state != record.state:
            raise ExecutionStateCorruptionError(
                "current state contradicts append-only audit history"
            )
        if set(payloads) - referenced:
            raise ExecutionStateCorruptionError(
                "operational payload is not referenced by audit history"
            )
        if active_claim_chain != active_claim_id:
            raise ExecutionStateCorruptionError(
                "active claim pointer contradicts append-only claim history"
            )
        if set(claims) != acquired_claim_ids:
            raise ExecutionStateCorruptionError(
                "durable claim lacks its append-only acquisition history"
            )

    def _validate_state_semantics(
        self,
        row: sqlite3.Row,
        record: DurableSubmissionRecord,
        audit_rows: list[sqlite3.Row],
        payload_rows: list[sqlite3.Row],
        claims: dict[str, AtomicDispatchClaimEvidence],
    ) -> None:
        decision_json = row["dispatch_decision_json"]
        if record.state == "PREPARED":
            if decision_json is not None:
                raise ExecutionStateCorruptionError(
                    "PREPARED row unexpectedly stores a dispatch decision"
                )
        else:
            if not isinstance(decision_json, str):
                raise ExecutionStateCorruptionError(
                    "non-PREPARED row lacks its dispatch decision"
                )
            try:
                decision = FrontierEligibilityDecision.from_json(decision_json)
            except (TypeError, ValueError) as exc:
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision is malformed"
                ) from exc
            if (
                decision.submission_id != record.submission_id
                or decision.target != record.target
                or decision.provenance_id != record.provenance_id
                or decision.eligible is not True
                or decision.reasons != ()
            ):
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision contradicts its submission"
                )
            marks = [
                event for event in audit_rows if event["reason"] == "MARK_DISPATCHING"
            ]
            mark = marks[0] if len(marks) == 1 else None
            if (
                mark is None
                or _canonical_instant(decision.dispatch_timestamp)
                != mark["observed_at"]
            ):
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision is not bound to its transition"
                )
            if decision.claim is None or mark is None:
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision lacks its durable claim"
                )
            claim_id = mark["claim_id"]
            if not isinstance(claim_id, str):
                raise ExecutionStateCorruptionError(
                    "dispatch transition lacks its claim identity"
                )
            claim = claims.get(claim_id)
            if claim is None:
                raise ExecutionStateCorruptionError(
                    "dispatch transition references a missing claim"
                )
            if (
                decision.claim.model_dump_json() != claim.model_dump_json()
                or mark["owner_id"] != claim.owner_id
                or mark["token_reference"] != claim.claim_id
                or int(mark["generation"]) != claim.generation
                or int(mark["recovery_epoch"]) != claim.recovery_epoch
                or mark["submission_id"] != claim.submission_id
                or mark["target_json"] != claim.target.model_dump_json()
            ):
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision contradicts its durable claim"
                )

            # The decision is immutable historical evidence of the first
            # DISPATCHING transition.  Its receipt and recovery evidence must
            # therefore resolve to the exact canonical rows that authorized
            # that transition; merely embedding model-valid artifacts is not
            # sufficient.  These checks deliberately use the MARK event's
            # epoch rather than the record's current epoch, because recovery
            # rebinds preserve the pre-rebind decision bytes.
            if (
                decision.durable_record is None
                or decision.commit_receipt is None
                or decision.recovery_evidence is None
            ):
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision lacks durable evidence"
                )
            mark_target_json = str(mark["target_json"])
            mark_generation = int(mark["generation"])
            mark_epoch = int(mark["recovery_epoch"])
            mark_at = _parse_instant(
                mark["observed_at"], name="dispatch observed_at"
            )
            historical_record = decision.durable_record
            if (
                historical_record.state != "PREPARED"
                or historical_record.record_id != record.record_id
                or historical_record.submission_id != record.submission_id
                or historical_record.provenance_id != record.provenance_id
                or historical_record.target != record.target
                or historical_record.target.model_dump_json() != mark_target_json
                or historical_record.generation != mark_generation
                or historical_record.recovery_epoch != mark_epoch
                or historical_record.created_at != record.created_at
            ):
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision durable record is not bound to its transition"
                )

            receipt_row = self._connection.execute(
                "SELECT * FROM commit_receipts WHERE receipt_id = ?",
                (decision.commit_receipt.receipt_id,),
            ).fetchone()
            if receipt_row is None:
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision references a missing receipt"
                )
            receipt = self._validate_receipt_row(
                receipt_row, record, current=False
            )
            if receipt.model_dump_json() != decision.commit_receipt.model_dump_json():
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision receipt is not canonical"
                )
            if (
                receipt.record_id != historical_record.record_id
                or receipt.submission_id != historical_record.submission_id
                or receipt.target != historical_record.target
                or receipt.generation != mark_generation
                or receipt.recovery_epoch != historical_record.recovery_epoch
                or receipt.committed is not True
                or receipt.committed_at != historical_record.updated_at
                or receipt.committed_at > mark_at
            ):
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision receipt is not bound to its transition"
                )

            evidence_row = self._connection.execute(
                "SELECT * FROM recovery_runs WHERE evidence_id = ?",
                (decision.recovery_evidence.evidence_id,),
            ).fetchone()
            if evidence_row is None:
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision references missing recovery evidence"
                )
            recovery_evidence = self._validate_recovery_row(evidence_row)
            if (
                recovery_evidence.model_dump_json()
                != decision.recovery_evidence.model_dump_json()
            ):
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision recovery evidence is not canonical"
                )
            if (
                recovery_evidence.target.model_dump_json() != mark_target_json
                or recovery_evidence.recovery_epoch != mark_epoch
                or recovery_evidence.scanned_through_generation < mark_generation
                or recovery_evidence.status != "COMPLETE"
                or recovery_evidence.observed_at > mark_at
            ):
                raise ExecutionStateCorruptionError(
                    "stored dispatch decision recovery evidence is not bound to its transition"
                )
        if record.state in _CLOSED_STATES and payload_rows:
            raise ExecutionStateCorruptionError(
                "closed state carries an operational payload"
            )
        if record.state in _PAYLOAD_BEARING_TERMINAL_STATES and not payload_rows:
            raise ExecutionStateCorruptionError(
                "terminal state lacks its operational payload"
            )
        governing: sqlite3.Row | None = None
        for event in reversed(audit_rows):
            if event["prior_state"] != event["new_state"]:
                governing = event
                break
        if governing is None:
            raise ExecutionStateCorruptionError(
                "submission has no recorded state transition"
            )
        pair = (str(governing["event_kind"]), str(governing["reason"]))
        if pair not in _GOVERNING_TRANSITIONS[record.state]:
            raise ExecutionStateCorruptionError(
                "current state contradicts its governing transition"
            )

    def _validate_submission_row(
        self, row: sqlite3.Row
    ) -> tuple[DurableSubmissionRecord, DurableCommitReceipt]:
        record = self._decode_record(row)
        self._decode_envelope_provenance(row)
        if record.record_id != f"execstate-record-v1-{record.generation}":
            raise ExecutionStateCorruptionError(
                "durable record identifier contradicts its generation"
            )
        target_json = record.target.model_dump_json()
        receipts = self._connection.execute(
            """
            SELECT * FROM commit_receipts
            WHERE record_id = ?
            ORDER BY receipt_sequence
            """,
            (record.record_id,),
        ).fetchall()
        if not receipts:
            raise ExecutionStateCorruptionError("submission has no commit receipts")
        current_receipt_id = row["current_receipt_id"]
        if not isinstance(current_receipt_id, str):
            raise ExecutionStateCorruptionError(
                "committed submission has no current receipt"
            )
        current_receipt: DurableCommitReceipt | None = None
        for receipt_row in receipts:
            receipt_id = str(receipt_row["receipt_id"])
            receipt = self._validate_receipt_row(
                receipt_row, record, current=receipt_id == current_receipt_id
            )
            if receipt_id == current_receipt_id:
                current_receipt = receipt
        if current_receipt is None:
            raise ExecutionStateCorruptionError("current commit receipt is missing")
        if str(receipts[-1]["receipt_id"]) != current_receipt_id:
            raise ExecutionStateCorruptionError(
                "current receipt is not the newest committed receipt"
            )
        claims: dict[str, AtomicDispatchClaimEvidence] = {}
        for claim_row in self._connection.execute(
            """
            SELECT * FROM claims
            WHERE record_id = ?
            ORDER BY claim_sequence
            """,
            (record.record_id,),
        ).fetchall():
            claim = self._validate_claim_row(claim_row, record)
            claims[claim.claim_id] = claim
        active_claim_id = row["active_claim_id"]
        if active_claim_id is not None:
            active = claims.get(str(active_claim_id))
            if active is None:
                raise ExecutionStateCorruptionError("active claim row is missing")
            if active.recovery_epoch != record.recovery_epoch:
                raise ExecutionStateCorruptionError(
                    "active claim belongs to a stale recovery epoch"
                )
        payloads: dict[int, dict[str, Any]] = {}
        payload_rows = self._connection.execute(
            """
            SELECT * FROM operational_payloads
            WHERE record_id = ?
            ORDER BY payload_sequence
            """,
            (record.record_id,),
        ).fetchall()
        for payload_row in payload_rows:
            facts = self._validate_payload_row(payload_row, record)
            payloads[int(facts["sequence"])] = facts
        audit_rows = self._connection.execute(
            """
            SELECT * FROM audit_events
            WHERE submission_id = ? AND target_json = ?
            ORDER BY event_sequence
            """,
            (record.submission_id, target_json),
        ).fetchall()
        self._validate_audit_rows(
            audit_rows,
            record,
            claims,
            payloads,
            receipts,
            None if row["active_claim_id"] is None else str(row["active_claim_id"]),
        )
        self._validate_state_semantics(row, record, audit_rows, payload_rows, claims)
        return record, current_receipt

    def _allocation_map(self) -> dict[tuple[str, str], int]:
        allocations: dict[tuple[str, str], int] = {}
        for row in self._connection.execute(
            """
            SELECT event_sequence, submission_id, target_json
            FROM audit_events
            WHERE row_scope = 'SUBMISSION'
              AND event_kind = 'PREPARED'
              AND reason = 'NEW_PREPARE'
            ORDER BY event_sequence
            """
        ).fetchall():
            key = (str(row["submission_id"]), str(row["target_json"]))
            if key in allocations:
                raise ExecutionStateCorruptionError(
                    "submission has more than one prepare allocation event"
                )
            allocations[key] = int(row["event_sequence"])
        return allocations

    def _generation_map(self) -> dict[tuple[str, str], int]:
        return {
            (str(row["submission_id"]), str(row["target_json"])): int(
                row["generation"]
            )
            for row in self._connection.execute(
                "SELECT submission_id, target_json, generation FROM submissions"
            ).fetchall()
        }

    def _submission_rows_before(
        self, key: tuple[str, str], sequence: int
    ) -> list[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT * FROM audit_events
            WHERE row_scope = 'SUBMISSION'
              AND submission_id = ?
              AND target_json = ?
              AND event_sequence < ?
            ORDER BY event_sequence DESC
            """,
            (key[0], key[1], sequence),
        ).fetchall()

    def _state_epoch_at_cut(
        self, key: tuple[str, str], begin_sequence: int
    ) -> tuple[str, int]:
        epoch_at_cut: int | None = None
        state_at_cut: str | None = None
        for row in self._submission_rows_before(key, begin_sequence):
            if epoch_at_cut is None:
                epoch_at_cut = int(row["recovery_epoch"])
            pair = (str(row["event_kind"]), str(row["reason"]))
            if pair in _OBSERVATIONAL_PAIRS:
                continue
            if state_at_cut is None and pair in _STATE_AUTHORITATIVE_PAIRS:
                state_at_cut = str(row["new_state"])
            if epoch_at_cut is not None and state_at_cut is not None:
                break
        if epoch_at_cut is None or state_at_cut is None:
            raise ExecutionStateCorruptionError(
                "submission history cannot reconstruct state at the recovery cut"
            )
        return state_at_cut, epoch_at_cut

    def _cut_rows(
        self, key: tuple[str, str], begin_sequence: int, end_sequence: int
    ) -> list[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT * FROM audit_events
            WHERE row_scope = 'SUBMISSION'
              AND submission_id = ?
              AND target_json = ?
              AND event_sequence > ?
              AND event_sequence < ?
            ORDER BY event_sequence
            """,
            (key[0], key[1], begin_sequence, end_sequence),
        ).fetchall()

    def _run_cut_anchors(
        self, run_row: sqlite3.Row
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        run_key = int(run_row["run_sequence"])
        anchors = self._connection.execute(
            """
            SELECT * FROM audit_events
            WHERE boundary_run_sequence = ?
            ORDER BY event_sequence
            """,
            (run_key,),
        ).fetchall()
        if len(anchors) != 2:
            raise ExecutionStateCorruptionError(
                "recovery run does not have exactly two cut anchors"
            )
        begins = [row for row in anchors if str(row["boundary_phase"]) == "BEGIN"]
        ends = [row for row in anchors if str(row["boundary_phase"]) == "END"]
        if len(begins) != 1 or len(ends) != 1:
            raise ExecutionStateCorruptionError(
                "recovery run cut does not have exactly one BEGIN and one END"
            )
        begin = begins[0]
        end = ends[0]
        for anchor in (begin, end):
            self._verify_boundary_row(anchor)
            if (
                int(anchor["boundary_run_sequence"]) != run_key
                or str(anchor["target_json"]) != str(run_row["target_json"])
                or int(anchor["recovery_epoch"]) != int(run_row["recovery_epoch"])
                or str(anchor["observed_at"]) != str(run_row["observed_at"])
            ):
                raise ExecutionStateCorruptionError(
                    "recovery cut anchor does not match its run"
                )
        if int(begin["event_sequence"]) >= int(end["event_sequence"]):
            raise ExecutionStateCorruptionError(
                "recovery cut anchors are reversed"
            )
        return begin, end

    def _latest_claim_binding(
        self, key: tuple[str, str], begin_sequence: int
    ) -> sqlite3.Row | None:
        for row in self._submission_rows_before(key, begin_sequence):
            if str(row["event_kind"]) == "CLAIM_ACQUIRED" and str(
                row["reason"]
            ) == "CLAIM_ACQUIRED":
                return row
            if str(row["event_kind"]) == "CLAIM_REVOKED":
                return None
        return None

    def _validate_run_cut(
        self,
        run_row: sqlite3.Row,
        *,
        allocations: dict[tuple[str, str], int] | None = None,
    ) -> None:
        evidence = self._validate_recovery_row(run_row)
        if evidence.status != "COMPLETE":
            raise ExecutionStateCorruptionError(
                "recovery history contains a non-complete run"
            )
        if allocations is None:
            allocations = self._allocation_map()
        generations = self._generation_map()
        run_epoch = int(run_row["recovery_epoch"])
        begin, end = self._run_cut_anchors(run_row)
        begin_sequence = int(begin["event_sequence"])
        end_sequence = int(end["event_sequence"])
        for key, allocation in allocations.items():
            if begin_sequence < allocation < end_sequence:
                raise ExecutionStateCorruptionError(
                    "prepare allocation lies inside a recovery cut"
                )
        pre_cut = {
            key for key, allocation in allocations.items() if allocation < begin_sequence
        }
        expected_scanned = max(
            (generations.get(key, 0) for key in pre_cut), default=0
        )
        scanned = evidence.scanned_through_generation
        if scanned < expected_scanned:
            raise ExecutionStateCorruptionError(
                "recovery coverage omits a pre-BEGIN generation"
            )
        if scanned > expected_scanned:
            raise ExecutionStateCorruptionError(
                "recovery coverage exceeds the pre-BEGIN generation maximum"
            )
        expected_unresolved: set[str] = set()
        for key in pre_cut:
            state_at_cut, _epoch_at_cut = self._state_epoch_at_cut(
                key, begin_sequence
            )
            if state_at_cut in {"OUTCOME_UNKNOWN", "DISPATCHING"}:
                expected_unresolved.add(key[0])
        recorded_unresolved = {
            str(value) for value in evidence.unresolved_submission_ids
        }
        if recorded_unresolved - expected_unresolved:
            raise ExecutionStateCorruptionError(
                "recovery evidence marks a non-unresolved submission unresolved"
            )
        if expected_unresolved - recorded_unresolved:
            raise ExecutionStateCorruptionError(
                "recovery evidence omits a historically unresolved submission"
            )
        for key in pre_cut:
            state_at_cut, epoch_at_cut = self._state_epoch_at_cut(
                key, begin_sequence
            )
            in_cut = self._cut_rows(key, begin_sequence, end_sequence)
            if state_at_cut == "DISPATCHING":
                conversions = [
                    row
                    for row in in_cut
                    if str(row["event_kind"]) == "RECOVERY_UNKNOWN_CONVERSION"
                ]
                if len(conversions) != 1:
                    raise ExecutionStateCorruptionError(
                        "recovery run does not convert a pre-BEGIN DISPATCHING "
                        "record exactly once inside its cut"
                    )
            if epoch_at_cut < run_epoch:
                rebinds = [
                    row
                    for row in in_cut
                    if str(row["event_kind"]) == "RECOVERY_EPOCH_REBIND"
                ]
                if len(rebinds) != 1:
                    raise ExecutionStateCorruptionError(
                        "recovery run does not rebind a stale-epoch record "
                        "exactly once inside its cut"
                    )
            claim_row = self._latest_claim_binding(key, begin_sequence)
            if claim_row is not None:
                stored_claim = self._connection.execute(
                    "SELECT * FROM claims WHERE claim_id = ?",
                    (str(claim_row["claim_id"]),),
                ).fetchone()
                if stored_claim is not None and (
                    int(stored_claim["recovery_epoch"]) < run_epoch
                    or str(stored_claim["expires_at"])
                    <= str(run_row["observed_at"])
                ):
                    revocations = [
                        row
                        for row in in_cut
                        if str(row["event_kind"]) == "CLAIM_REVOKED"
                    ]
                    if len(revocations) != 1:
                        raise ExecutionStateCorruptionError(
                            "recovery run does not revoke a stale or expired "
                            "claim exactly once inside its cut"
                        )
        for row in self._connection.execute(
            """
            SELECT * FROM audit_events
            WHERE row_scope = 'SUBMISSION'
              AND event_sequence > ?
              AND event_sequence < ?
            ORDER BY event_sequence
            """,
            (begin_sequence, end_sequence),
        ).fetchall():
            key = (str(row["submission_id"]), str(row["target_json"]))
            recorded_allocation = allocations.get(key)
            if recorded_allocation is None or recorded_allocation > begin_sequence:
                raise ExecutionStateCorruptionError(
                    "recovery cut contains a row for a post-BEGIN identity"
                )

    def _current_run_cut_values(
        self, begin_sequence: int
    ) -> tuple[int, tuple[str, ...]]:
        allocations = self._allocation_map()
        generations = self._generation_map()
        pre_cut = {
            key for key, allocation in allocations.items() if allocation < begin_sequence
        }
        scanned = max((generations.get(key, 0) for key in pre_cut), default=0)
        unresolved: set[str] = set()
        for key in pre_cut:
            state_at_cut, _epoch_at_cut = self._state_epoch_at_cut(
                key, begin_sequence
            )
            if state_at_cut in {"OUTCOME_UNKNOWN", "DISPATCHING"}:
                unresolved.add(key[0])
        return scanned, tuple(sorted(unresolved))

    def _validate_recovery_invariants(self) -> None:
        run_epochs: set[int] = set()
        maximum_run_epoch = 0
        recovery_rows = self._connection.execute(
            "SELECT * FROM recovery_runs ORDER BY run_sequence"
        ).fetchall()
        submission_rows = self._connection.execute(
            "SELECT * FROM submissions ORDER BY generation"
        ).fetchall()

        records: dict[tuple[str, str], DurableSubmissionRecord] = {}
        for row in submission_rows:
            record, _receipt = self._validate_submission_row(row)
            key = (record.submission_id, record.target.model_dump_json())
            records[key] = record

        allocations = self._allocation_map()
        for key in records:
            if key not in allocations:
                raise ExecutionStateCorruptionError(
                    "durable submission has no prepare allocation event"
                )
        run_keys = {int(row["run_sequence"]) for row in recovery_rows}
        for boundary_row in self._connection.execute(
            """
            SELECT * FROM audit_events
            WHERE row_scope = 'RECOVERY_BOUNDARY'
            ORDER BY event_sequence
            """
        ).fetchall():
            self._verify_boundary_row(boundary_row)
            if int(boundary_row["boundary_run_sequence"]) not in run_keys:
                raise ExecutionStateCorruptionError(
                    "boundary anchor references a missing recovery run"
                )

        previous_run_epoch = 0
        for row in recovery_rows:
            epoch = int(row["recovery_epoch"])
            if epoch < previous_run_epoch:
                raise ExecutionStateCorruptionError(
                    "recovery epoch history moves backwards"
                )
            previous_run_epoch = epoch
            self._validate_run_cut(row, allocations=allocations)
            run_epochs.add(epoch)
            maximum_run_epoch = max(maximum_run_epoch, epoch)
        for row in self._connection.execute(
            """
            SELECT * FROM audit_events
            WHERE event_kind IN (
                'RECOVERY_EPOCH_REBIND', 'CLAIM_REVOKED',
                'RECOVERY_UNKNOWN_CONVERSION'
            )
            ORDER BY event_sequence
            """
        ).fetchall():
            key = (str(row["submission_id"]), str(row["target_json"]))
            allocation = allocations.get(key)
            if allocation is None:
                raise ExecutionStateCorruptionError(
                    "recovery audit row has no prepare allocation event"
                )
            matched = False
            for run_row in recovery_rows:
                if int(run_row["recovery_epoch"]) != int(row["recovery_epoch"]):
                    continue
                begin, end = self._run_cut_anchors(run_row)
                begin_sequence = int(begin["event_sequence"])
                if (
                    begin_sequence
                    < int(row["event_sequence"])
                    < int(end["event_sequence"])
                    and allocation < begin_sequence
                ):
                    matched = True
                    break
            if not matched:
                raise ExecutionStateCorruptionError(
                    "recovery audit row lies outside every matching recovery cut"
                )
        if run_epochs != set(range(1, maximum_run_epoch + 1)):
            raise ExecutionStateCorruptionError(
                "recovery epoch history contains a gap or foreign epoch"
            )
        for record in records.values():
            if record.recovery_epoch not in run_epochs:
                raise ExecutionStateCorruptionError(
                    "submission recovery epoch has no recovery run"
                )
        next_generation = _metadata_int(
            self._connection, "next_generation", minimum=1
        )
        next_epoch = _metadata_int(
            self._connection, "next_recovery_epoch", minimum=1
        )
        maximum_generation = max(
            (record.generation for record in records.values()), default=0
        )
        if next_generation <= maximum_generation:
            raise ExecutionStateCorruptionError(
                "generation counter would reuse a durable generation"
            )
        if next_epoch <= maximum_run_epoch:
            raise ExecutionStateCorruptionError(
                "recovery epoch counter would reuse a durable recovery epoch"
            )

    def _insert_payload(
        self,
        *,
        record_id: str,
        submission_id: str,
        target_json: str,
        payload_kind: str,
        payload_json: str,
        observed_at: datetime,
    ) -> int:
        _check_size(payload_json, name="operational payload")
        cursor = self._connection.execute(
            """
            INSERT INTO operational_payloads(
                record_id, submission_id, target_json, payload_kind,
                payload_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                submission_id,
                target_json,
                payload_kind,
                payload_json,
                _canonical_instant(observed_at),
            ),
        )
        if cursor.lastrowid is None:
            raise ExecutionStateCorruptionError(
                "operational payload sequence is missing"
            )
        return int(cursor.lastrowid)

    def _latest_payload(
        self, *, record_id: str, payload_kind: str
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT * FROM operational_payloads
            WHERE record_id = ? AND payload_kind = ?
            ORDER BY payload_sequence DESC
            LIMIT 1
            """,
            (record_id, payload_kind),
        ).fetchone()

    def _update_submission_state(
        self,
        *,
        record: DurableSubmissionRecord,
        new_state: StateLiteral,
        updated_at: datetime,
        recovery_epoch: int,
        current_receipt_id: str,
        dispatch_decision_json: str | None = None,
    ) -> DurableSubmissionRecord:
        new_record = DurableSubmissionRecord(
            schema="frontier-durable-record-v1",
            record_id=record.record_id,
            submission_id=record.submission_id,
            provenance_id=record.provenance_id,
            target=record.target,
            generation=record.generation,
            recovery_epoch=recovery_epoch,
            state=new_state,
            created_at=record.created_at,
            updated_at=updated_at,
        )
        self._connection.execute(
            """
            UPDATE submissions
            SET record_json = ?, state = ?, recovery_epoch = ?,
                current_receipt_id = ?, dispatch_decision_json = ?,
                updated_at = ?
            WHERE submission_id = ? AND target_json = ?
            """,
            (
                new_record.model_dump_json(),
                new_state,
                recovery_epoch,
                current_receipt_id,
                dispatch_decision_json,
                _canonical_instant(updated_at),
                record.submission_id,
                record.target.model_dump_json(),
            ),
        )
        return new_record

    def _validate_dispatch_claim_binding(
        self,
        *,
        row: sqlite3.Row,
        record: DurableSubmissionRecord,
        eligibility_claim: AtomicDispatchClaimEvidence | None,
        owner_id: str,
        token: str,
        dispatch_timestamp: datetime,
        current_epoch: int,
    ) -> AtomicDispatchClaimEvidence:
        active_claim_id = row["active_claim_id"]
        if not isinstance(active_claim_id, str):
            raise ExecutionStateDeniedError("active claim is required")
        claim = self._decode_claim(active_claim_id, record=record)
        if eligibility_claim is None:
            raise ExecutionStateDeniedError("frontier claim evidence is required")
        if eligibility_claim.model_dump_json() != claim.model_dump_json():
            raise ExecutionStateDeniedError(
                "frontier claim differs from the active durable claim"
            )
        if (
            claim.status != "ACQUIRED"
            or claim.recovery_epoch != current_epoch
            or claim.recovery_epoch != record.recovery_epoch
            or claim.generation != record.generation
            or claim.submission_id != record.submission_id
            or claim.target != record.target
            or claim.owner_id != owner_id
            or claim.token != token
        ):
            raise ExecutionStateDeniedError(
                "active claim does not bind this dispatch"
            )
        revoked = self._connection.execute(
            """
            SELECT 1 FROM audit_events
            WHERE claim_id = ? AND event_kind = 'CLAIM_REVOKED'
            LIMIT 1
            """,
            (claim.claim_id,),
        ).fetchone()
        if revoked is not None:
            raise ExecutionStateDeniedError("active claim was revoked")
        if claim.expires_at is None:
            raise ExecutionStateCorruptionError("stored claim has no expiry")
        if (
            claim.acquired_at > dispatch_timestamp
            or dispatch_timestamp >= claim.expires_at
        ):
            raise ExecutionStateDeniedError(
                "active claim does not authorize this dispatch instant"
            )
        return claim

    def _recover(self, target: ExecutionTargetBinding, observed_at: datetime) -> RecoveryCompleteEvidence:
        moment = _utc(observed_at, name="observed_at")
        self._verify_schema()
        allocated_epoch = False
        try:
            self._begin_immediate()
            # Step A: validate every pre-existing durable fact before any
            # current-run mutation can hide an existing contradiction.
            self._validate_recovery_invariants()
            # Step B: allocate the exact run key and choose the session epoch
            # with reads only; the BEGIN anchor below is the first write.
            run_sequence = _next_run_sequence(self._connection)
            if self._current_epoch is None:
                epoch = _metadata_int(
                    self._connection, "next_recovery_epoch", minimum=1
                )
                if epoch >= _INT64_MAX:
                    raise ExecutionStateArgumentError(
                        "recovery epoch space is exhausted"
                    )
                allocated_epoch = True
            else:
                self._require_current_recovery_epoch()
                epoch = self._current_epoch
            # Step C: the BEGIN anchor is the first audit write of this
            # recovery transaction.
            begin_sequence = self._write_boundary_anchor(
                run_sequence=run_sequence,
                target_json=target.model_dump_json(),
                recovery_epoch=epoch,
                observed_at=moment,
                phase="BEGIN",
            )
            if allocated_epoch:
                _set_metadata_int(
                    self._connection, "next_recovery_epoch", epoch + 1
                )
                self._current_epoch = epoch
            rows = self._connection.execute(
                "SELECT * FROM submissions ORDER BY generation"
            ).fetchall()
            for row in rows:
                record = self._decode_record(row)
                self._decode_envelope_provenance(row)
                latest_audit = self._connection.execute(
                    """
                    SELECT new_state FROM audit_events
                    WHERE submission_id = ? AND target_json = ?
                    ORDER BY event_sequence DESC
                    LIMIT 1
                    """,
                    (record.submission_id, row["target_json"]),
                ).fetchone()
                if (
                    latest_audit is None
                    or latest_audit["new_state"] != record.state
                ):
                    raise ExecutionStateCorruptionError(
                        "current state contradicts append-only audit history"
                    )
                current_receipt_id = row["current_receipt_id"]
                if not isinstance(current_receipt_id, str):
                    raise ExecutionStateCorruptionError(
                        "committed submission has no current receipt"
                    )
                self._decode_receipt(current_receipt_id, record=record)
                if record.updated_at > moment:
                    raise ExecutionStateArgumentError(
                        "recover observed_at must not precede stored updated_at"
                    )
                needs_rebind = record.recovery_epoch != epoch
                is_dispatching = record.state == "DISPATCHING"
                if needs_rebind:
                    new_state: StateLiteral = (
                        "OUTCOME_UNKNOWN" if is_dispatching else record.state
                    )
                    receipt = self._insert_receipt(
                        record_id=record.record_id,
                        submission_id=record.submission_id,
                        target=record.target,
                        generation=record.generation,
                        recovery_epoch=epoch,
                        committed_at=moment,
                    )
                    new_record = self._update_submission_state(
                        record=record,
                        new_state=new_state,
                        updated_at=moment,
                        recovery_epoch=epoch,
                        current_receipt_id=receipt.receipt_id,
                        dispatch_decision_json=row["dispatch_decision_json"],
                    )
                    self._write_audit(
                        submission_id=new_record.submission_id,
                        target_json=row["target_json"],
                        prior_state=record.state,
                        new_state=record.state,
                        generation=new_record.generation,
                        recovery_epoch=epoch,
                        event_kind="RECOVERY_EPOCH_REBIND",
                        reason="RECOVERY_EPOCH_REBIND",
                        observed_at=moment,
                    )
                    if is_dispatching:
                        self._write_audit(
                            submission_id=new_record.submission_id,
                            target_json=row["target_json"],
                            prior_state="DISPATCHING",
                            new_state="OUTCOME_UNKNOWN",
                            generation=new_record.generation,
                            recovery_epoch=epoch,
                            event_kind="RECOVERY_UNKNOWN_CONVERSION",
                            reason="RECOVERY_UNKNOWN_CONVERSION",
                            observed_at=moment,
                        )
                elif is_dispatching:
                    receipt = self._insert_receipt(
                        record_id=record.record_id,
                        submission_id=record.submission_id,
                        target=record.target,
                        generation=record.generation,
                        recovery_epoch=record.recovery_epoch,
                        committed_at=moment,
                    )
                    new_record = self._update_submission_state(
                        record=record,
                        new_state="OUTCOME_UNKNOWN",
                        updated_at=moment,
                        recovery_epoch=epoch,
                        current_receipt_id=receipt.receipt_id,
                        dispatch_decision_json=row["dispatch_decision_json"],
                    )
                    self._write_audit(
                        submission_id=new_record.submission_id,
                        target_json=row["target_json"],
                        prior_state="DISPATCHING",
                        new_state="OUTCOME_UNKNOWN",
                        generation=new_record.generation,
                        recovery_epoch=epoch,
                        event_kind="RECOVERY_UNKNOWN_CONVERSION",
                        reason="RECOVERY_UNKNOWN_CONVERSION",
                        observed_at=moment,
                    )
            for event_row in self._connection.execute(
                """
                SELECT * FROM audit_events
                WHERE row_scope = 'SUBMISSION'
                ORDER BY event_sequence
                """
            ).fetchall():
                self._verify_audit_row(event_row)
            active_claims = self._connection.execute(
                """
                SELECT s.*, c.claim_json, c.owner_id, c.token,
                       c.acquired_at, c.expires_at, c.recovery_epoch AS claim_epoch
                FROM submissions AS s
                JOIN claims AS c ON c.claim_id = s.active_claim_id
                WHERE s.active_claim_id IS NOT NULL
                """
            ).fetchall()
            for row in active_claims:
                record = self._decode_record(row)
                claim = self._decode_claim(str(row["active_claim_id"]), record=record)
                if claim.expires_at is None:
                    raise ExecutionStateCorruptionError(
                        "stored claim has no expiry"
                    )
                if claim.recovery_epoch != epoch or claim.expires_at <= moment:
                    self._connection.execute(
                        """
                        UPDATE submissions SET active_claim_id = NULL
                        WHERE submission_id = ? AND target_json = ?
                        """,
                        (record.submission_id, row["target_json"]),
                    )
                    self._write_audit(
                        submission_id=record.submission_id,
                        target_json=row["target_json"],
                        prior_state=record.state,
                        new_state=record.state,
                        generation=record.generation,
                        recovery_epoch=epoch,
                        event_kind="CLAIM_REVOKED",
                        reason="CLAIM_REVOKED",
                        observed_at=moment,
                        owner_id=claim.owner_id,
                        token_reference=claim.claim_id,
                        claim_id=claim.claim_id,
                    )
            # Step H: historical coverage and database-wide unresolved truth
            # come from the exact pre-BEGIN cut, never from final state.
            scanned, unresolved = self._current_run_cut_values(begin_sequence)
            evidence_id = f"execstate-recovery-v1-{run_sequence}"
            evidence = RecoveryCompleteEvidence(
                schema="frontier-recovery-v1",
                evidence_id=evidence_id,
                target=target,
                recovery_epoch=epoch,
                scanned_through_generation=scanned,
                status="COMPLETE",
                unresolved_submission_ids=unresolved,
                observed_at=moment,
            )
            evidence_json = evidence.model_dump_json()
            _check_size(evidence_json, name="recovery evidence")
            # Step I: the END anchor is the last audit write of this
            # transaction.
            self._write_boundary_anchor(
                run_sequence=run_sequence,
                target_json=target.model_dump_json(),
                recovery_epoch=epoch,
                observed_at=moment,
                phase="END",
            )
            # Step J: the run row binds the two committed anchors.
            self._connection.execute(
                """
                INSERT INTO recovery_runs(
                    run_sequence, evidence_id, target_json, recovery_epoch,
                    scanned_through_generation, status, evidence_json,
                    observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_sequence,
                    evidence_id,
                    target.model_dump_json(),
                    epoch,
                    scanned,
                    "COMPLETE",
                    evidence_json,
                    _canonical_instant(moment),
                ),
            )
            self._validate_recovery_invariants()
            self._commit()
        except ExecutionStateError:
            self._rollback()
            if allocated_epoch:
                self._current_epoch = None
            raise
        except sqlite3.Error as exc:
            self._rollback()
            if allocated_epoch:
                self._current_epoch = None
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError(
                "startup recovery transaction failed"
            ) from exc
        self._recovered = True
        return evidence

    def recover(
        self,
        target: ExecutionTargetBinding,
        *,
        observed_at: datetime,
    ) -> RecoveryCompleteEvidence:
        self._require_open()
        if not isinstance(target, ExecutionTargetBinding):
            raise TypeError("target must be an ExecutionTargetBinding")
        return self._recover(target, observed_at)

    def prepare(
        self,
        envelope: BrokerSubmissionEnvelope,
        provenance: FreshRiskProvenance,
        target: ExecutionTargetBinding,
        *,
        prepared_at: datetime,
    ) -> tuple[DurableSubmissionRecord, DurableCommitReceipt]:
        self._require_recovered()
        if not isinstance(envelope, BrokerSubmissionEnvelope):
            raise TypeError("envelope must be a BrokerSubmissionEnvelope")
        if not isinstance(provenance, FreshRiskProvenance):
            raise TypeError("provenance must be a FreshRiskProvenance")
        if not isinstance(target, ExecutionTargetBinding):
            raise TypeError("target must be an ExecutionTargetBinding")
        moment = _utc(prepared_at, name="prepared_at")
        if envelope.submission_id != target.submission_id:
            raise ExecutionStateArgumentError(
                "envelope and target submission identities differ"
            )
        if provenance.pre_dispatch_decision.envelope != envelope:
            raise ExecutionStateArgumentError(
                "provenance envelope differs from the supplied envelope"
            )
        if provenance.pre_dispatch_decision.envelope.submission_id != target.submission_id:
            raise ExecutionStateArgumentError(
                "provenance submission identity differs from the target"
            )
        if provenance.pre_dispatch_decision.eligible is not True:
            raise ExecutionStateArgumentError(
                "provenance is not an eligible fresh pre-dispatch decision"
            )
        target_json = target.model_dump_json()
        envelope_json = envelope.model_dump_json()
        provenance_json = provenance.model_dump_json()
        _check_size(envelope_json, name="envelope_json")
        _check_size(provenance_json, name="provenance_json")
        if (
            len(envelope_json.encode("utf-8"))
            + len(provenance_json.encode("utf-8"))
            > _MAX_PREPARE_BYTES
        ):
            raise ExecutionStateArgumentError("prepare payload exceeds the bound")
        epoch = self._current_epoch
        if epoch is None:
            raise ExecutionStateRecoveryRequiredError(
                "startup recovery must complete before prepare"
            )
        try:
            self._begin_immediate()
            epoch = self._require_current_recovery_epoch()
            existing = self._load_submission_row(envelope.submission_id, target_json)
            if existing is not None:
                record = self._decode_record(existing)
                current_receipt_id = existing["current_receipt_id"]
                if not isinstance(current_receipt_id, str):
                    raise ExecutionStateCorruptionError(
                        "committed submission has no current receipt"
                    )
                receipt = self._decode_receipt(current_receipt_id, record=record)
                exact_payload = (
                    existing["envelope_json"] == envelope_json
                    and existing["provenance_json"] == provenance_json
                    and existing["created_at"] == _canonical_instant(moment)
                )
                if existing["state"] == "PREPARED" and exact_payload:
                    self._write_audit(
                        submission_id=record.submission_id,
                        target_json=target_json,
                        prior_state="PREPARED",
                        new_state="PREPARED",
                        generation=record.generation,
                        recovery_epoch=record.recovery_epoch,
                        event_kind="PREPARED",
                        reason="DUPLICATE_PREPARE_REPLAY",
                        observed_at=moment,
                    )
                    self._commit()
                    return record, receipt
                if existing["state"] != "PREPARED":
                    raise ExecutionStateDeniedError(
                        "submission identity is no longer PREPARED"
                    )
                raise ExecutionStateConflictError(
                    "duplicate prepare payload contradicts stored state"
                )
            if moment < self._epoch_allocating_observed_at(epoch):
                raise ExecutionStateArgumentError(
                    "prepare prepared_at precedes its recovery epoch"
                )
            generation = self._allocate_generation()
            record = DurableSubmissionRecord(
                schema="frontier-durable-record-v1",
                record_id=f"execstate-record-v1-{generation}",
                submission_id=envelope.submission_id,
                provenance_id=provenance.provenance_id,
                target=target,
                generation=generation,
                recovery_epoch=epoch,
                state="PREPARED",
                created_at=moment,
                updated_at=moment,
            )
            record_json = record.model_dump_json()
            _check_size(record_json, name="record_json")
            self._connection.execute(
                """
                INSERT INTO submissions(
                    submission_id, target_json, record_id, generation,
                    recovery_epoch, state, record_json, envelope_json,
                    provenance_json, provenance_id, current_receipt_id,
                    active_claim_id, dispatch_decision_json, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    record.submission_id,
                    target_json,
                    record.record_id,
                    record.generation,
                    record.recovery_epoch,
                    record.state,
                    record_json,
                    envelope_json,
                    provenance_json,
                    record.provenance_id,
                    _canonical_instant(record.created_at),
                    _canonical_instant(record.updated_at),
                ),
            )
            receipt = self._insert_receipt(
                record_id=record.record_id,
                submission_id=record.submission_id,
                target=record.target,
                generation=record.generation,
                recovery_epoch=record.recovery_epoch,
                committed_at=moment,
            )
            self._connection.execute(
                """
                UPDATE submissions SET current_receipt_id = ?
                WHERE submission_id = ? AND target_json = ?
                """,
                (receipt.receipt_id, record.submission_id, target_json),
            )
            self._write_audit(
                submission_id=record.submission_id,
                target_json=target_json,
                prior_state=None,
                new_state="PREPARED",
                generation=record.generation,
                recovery_epoch=record.recovery_epoch,
                event_kind="PREPARED",
                reason="NEW_PREPARE",
                observed_at=moment,
            )
            self._commit()
            return record, receipt
        except ExecutionStateError:
            self._rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self._rollback()
            raise ExecutionStateConflictError(
                "prepare conflicts with persisted identity"
            ) from exc
        except sqlite3.Error as exc:
            self._rollback()
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError(
                "prepare transaction failed"
            ) from exc

    def read(
        self,
        submission_id: str,
        target: ExecutionTargetBinding,
    ) -> tuple[DurableSubmissionRecord, DurableCommitReceipt]:
        self._require_recovered()
        if not isinstance(submission_id, str):
            raise TypeError("submission_id must be a string")
        if not isinstance(target, ExecutionTargetBinding):
            raise TypeError("target must be an ExecutionTargetBinding")
        target_json = target.model_dump_json()
        try:
            self._begin_read()
            row = self._load_submission_row(submission_id, target_json)
            if row is None:
                raise ExecutionStateNotFoundError(
                    "submission identity was not found"
                )
            result = self._validate_submission_row(row)
            self._commit()
            return result
        except ExecutionStateError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError("read transaction failed") from exc

    def _epoch_allocating_observed_at(self, epoch: int) -> datetime:
        row = self._connection.execute(
            """
            SELECT observed_at FROM recovery_runs
            WHERE recovery_epoch = ?
            ORDER BY run_sequence ASC
            LIMIT 1
            """,
            (epoch,),
        ).fetchone()
        if row is None:
            raise ExecutionStateRecoveryRequiredError(
                "current recovery epoch has no allocating run"
            )
        return _parse_instant(row["observed_at"], name="recovery observed_at")

    def claim(
        self,
        submission_id: str,
        target: ExecutionTargetBinding,
        *,
        owner_id: str,
        token: str,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> AtomicDispatchClaimEvidence:
        self._require_recovered()
        if not isinstance(submission_id, str):
            raise TypeError("submission_id must be a string")
        if not isinstance(target, ExecutionTargetBinding):
            raise TypeError("target must be an ExecutionTargetBinding")
        owner = _opaque(owner_id, name="owner_id")
        token_value = _opaque(token, name="token")
        acquired = _utc(acquired_at, name="acquired_at")
        expires = _utc(expires_at, name="expires_at")
        if expires <= acquired:
            raise ExecutionStateArgumentError(
                "expires_at must be strictly later than acquired_at"
            )
        epoch = self._current_epoch
        if epoch is None:
            raise ExecutionStateRecoveryRequiredError(
                "startup recovery must complete before claim"
            )
        target_json = target.model_dump_json()
        try:
            # A known-stale contender must not hold the writer lock while the
            # current session attempts its one-shot claim. The check below,
            # inside BEGIN IMMEDIATE, still closes the epoch-change race.
            if self._current_epoch != _metadata_int(
                self._connection, "next_recovery_epoch", minimum=1
            ) - 1:
                raise ExecutionStateDeniedError("store recovery epoch is stale")
            self._begin_immediate()
            epoch = self._require_current_recovery_epoch()
            row = self._load_submission_row(submission_id, target_json)
            if row is None:
                raise ExecutionStateNotFoundError(
                    "submission identity was not found"
                )
            record = self._decode_record(row)
            if record.recovery_epoch != epoch:
                raise ExecutionStateDeniedError(
                    "submission belongs to a stale recovery epoch"
                )
            if row["state"] != "PREPARED":
                raise ExecutionStateDeniedError(
                    "claim is permitted only for PREPARED state"
                )
            if acquired < self._epoch_allocating_observed_at(epoch):
                raise ExecutionStateArgumentError(
                    "claim acquired_at precedes its recovery epoch"
                )
            active_claim_id = row["active_claim_id"]
            if active_claim_id is not None:
                claim = self._decode_claim(str(active_claim_id), record=record)
                if (
                    claim.owner_id == owner
                    and claim.token == token_value
                    and claim.acquired_at == acquired
                    and claim.expires_at == expires
                ):
                    self._write_audit(
                        submission_id=record.submission_id,
                        target_json=target_json,
                        prior_state="PREPARED",
                        new_state="PREPARED",
                        generation=record.generation,
                        recovery_epoch=record.recovery_epoch,
                        event_kind="CLAIM_ACQUIRED",
                        reason="CLAIM_REPLAY",
                        observed_at=acquired,
                        owner_id=owner,
                        token_reference=claim.claim_id,
                        claim_id=claim.claim_id,
                    )
                    self._commit()
                    return claim
                if claim.owner_id == owner and claim.token == token_value:
                    raise ExecutionStateConflictError(
                        "claim replay timestamps differ"
                    )
                self._write_audit(
                    submission_id=record.submission_id,
                    target_json=target_json,
                    prior_state="PREPARED",
                    new_state="PREPARED",
                    generation=record.generation,
                    recovery_epoch=record.recovery_epoch,
                    event_kind="CLAIM_CONFLICT",
                    reason="CLAIM_CONFLICT",
                    observed_at=acquired,
                    owner_id=owner,
                    token_reference=claim.claim_id,
                    claim_id=claim.claim_id,
                )
                self._commit()
                raise ExecutionStateDeniedError("submission already has a claim")
            claim_sequence = _next_sequence(self._connection, "claims")
            claim_id = f"execstate-claim-v1-{claim_sequence}"
            cursor = self._connection.execute(
                """
                UPDATE submissions SET active_claim_id = ?
                WHERE submission_id = ? AND target_json = ?
                  AND state = 'PREPARED' AND recovery_epoch = ?
                  AND active_claim_id IS NULL
                """,
                (claim_id, submission_id, target_json, epoch),
            )
            if cursor.rowcount != 1:
                current = self._load_submission_row(submission_id, target_json)
                if current is None:
                    raise ExecutionStateNotFoundError(
                        "submission identity was not found"
                    )
                current_record = self._decode_record(current)
                if current_record.recovery_epoch != epoch:
                    raise ExecutionStateDeniedError(
                        "submission belongs to a stale recovery epoch"
                    )
                if current_record.state != "PREPARED":
                    raise ExecutionStateDeniedError(
                        "claim is permitted only for PREPARED state"
                    )
                active_id = current["active_claim_id"]
                if isinstance(active_id, str):
                    competing = self._decode_claim(active_id, record=current_record)
                    self._write_audit(
                        submission_id=current_record.submission_id,
                        target_json=target_json,
                        prior_state="PREPARED",
                        new_state="PREPARED",
                        generation=current_record.generation,
                        recovery_epoch=current_record.recovery_epoch,
                        event_kind="CLAIM_CONFLICT",
                        reason="CLAIM_CONFLICT",
                        observed_at=acquired,
                        owner_id=owner,
                        token_reference=competing.claim_id,
                        claim_id=competing.claim_id,
                    )
                    self._commit()
                    raise ExecutionStateDeniedError("submission already has a claim")
                raise ExecutionStateConflictError(
                    "claim compare-and-set did not identify one row"
                )
            claim = AtomicDispatchClaimEvidence(
                schema="frontier-claim-v1",
                claim_id=claim_id,
                submission_id=record.submission_id,
                target=record.target,
                record_id=record.record_id,
                generation=record.generation,
                recovery_epoch=record.recovery_epoch,
                owner_id=owner,
                token=token_value,
                status="ACQUIRED",
                acquired_at=acquired,
                expires_at=expires,
            )
            claim_json = claim.model_dump_json()
            _check_size(claim_json, name="claim_json")
            self._connection.execute(
                """
                INSERT INTO claims(
                    claim_sequence, claim_id, record_id, submission_id,
                    target_json, generation, recovery_epoch, claim_json,
                    owner_id, token, status, acquired_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACQUIRED', ?, ?)
                """,
                (
                    claim_sequence,
                    claim.claim_id,
                    claim.record_id,
                    claim.submission_id,
                    target_json,
                    claim.generation,
                    claim.recovery_epoch,
                    claim_json,
                    claim.owner_id,
                    claim.token,
                    _canonical_instant(acquired),
                    _canonical_instant(expires),
                ),
            )
            self._write_audit(
                submission_id=record.submission_id,
                target_json=target_json,
                prior_state="PREPARED",
                new_state="PREPARED",
                generation=record.generation,
                recovery_epoch=record.recovery_epoch,
                event_kind="CLAIM_ACQUIRED",
                reason="CLAIM_ACQUIRED",
                observed_at=acquired,
                owner_id=owner,
                token_reference=claim.claim_id,
                claim_id=claim.claim_id,
            )
            self._commit()
            return claim
        except ExecutionStateError:
            self._rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self._rollback()
            raise ExecutionStateDeniedError("claim conflict") from exc
        except sqlite3.Error as exc:
            self._rollback()
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError("claim transaction failed") from exc

    def mark_dispatching(
        self,
        eligibility: FrontierEligibilityDecision,
        *,
        owner_id: str,
        token: str,
    ) -> DurableSubmissionRecord:
        self._require_recovered()
        if not isinstance(eligibility, FrontierEligibilityDecision):
            raise TypeError("eligibility must be a FrontierEligibilityDecision")
        owner = _opaque(owner_id, name="owner_id")
        token_value = _opaque(token, name="token")
        target = eligibility.target
        submission_id = eligibility.submission_id
        target_json = target.model_dump_json()
        moment = eligibility.dispatch_timestamp
        try:
            self._begin_immediate()
            current_epoch = self._require_current_recovery_epoch()
            row = self._load_submission_row(submission_id, target_json)
            if row is None:
                raise ExecutionStateNotFoundError(
                    "submission identity was not found"
                )
            record = self._decode_record(row)
            decision_json = eligibility.model_dump_json()
            _check_size(decision_json, name="dispatch decision")
            if row["state"] == "DISPATCHING":
                if row["dispatch_decision_json"] != decision_json:
                    raise ExecutionStateConflictError(
                        "dispatching replay decision differs"
                    )
                claim = self._validate_dispatch_claim_binding(
                    row=row,
                    record=record,
                    eligibility_claim=eligibility.claim,
                    owner_id=owner,
                    token=token_value,
                    dispatch_timestamp=moment,
                    current_epoch=current_epoch,
                )
                self._write_audit(
                    submission_id=record.submission_id,
                    target_json=target_json,
                    prior_state="DISPATCHING",
                    new_state="DISPATCHING",
                    generation=record.generation,
                    recovery_epoch=record.recovery_epoch,
                    event_kind="DISPATCHING",
                    reason="DISPATCHING_REPLAY",
                    observed_at=moment,
                    owner_id=owner,
                    token_reference=claim.claim_id,
                    claim_id=claim.claim_id,
                )
                self._commit()
                return record
            if row["state"] != "PREPARED":
                raise ExecutionStateDeniedError(
                    "mark_dispatching requires PREPARED state"
                )
            if row["dispatch_decision_json"] is not None:
                raise ExecutionStateCorruptionError(
                    "PREPARED row unexpectedly stores a dispatch decision"
                )
            if eligibility.eligible is not True or eligibility.reasons != ():
                raise ExecutionStateDeniedError(
                    "frontier eligibility decision is not positive"
                )
            if (
                eligibility.target != target
                or eligibility.submission_id != submission_id
                or eligibility.provenance.provenance_id != row["provenance_id"]
                or eligibility.durable_record is None
                or eligibility.commit_receipt is None
                or eligibility.recovery_evidence is None
                or eligibility.claim is None
            ):
                raise ExecutionStateDeniedError(
                    "frontier eligibility artifacts do not match this row"
                )
            if eligibility.durable_record.model_dump_json() != row["record_json"]:
                raise ExecutionStateDeniedError(
                    "frontier durable record differs from persisted state"
                )
            current_receipt_id = row["current_receipt_id"]
            if not isinstance(current_receipt_id, str):
                raise ExecutionStateCorruptionError(
                    "committed submission has no current receipt"
                )
            current_receipt = self._decode_receipt(current_receipt_id, record=record)
            if (
                eligibility.commit_receipt.model_dump_json()
                != current_receipt.model_dump_json()
            ):
                raise ExecutionStateDeniedError(
                    "frontier commit receipt differs from persisted state"
                )
            stored_recovery = self._load_recovery_evidence(
                eligibility.recovery_evidence.evidence_id
            )
            if (
                eligibility.recovery_evidence.model_dump_json()
                != stored_recovery.model_dump_json()
            ):
                raise ExecutionStateDeniedError(
                    "frontier recovery evidence is not committed here"
                )
            if (
                stored_recovery.target != target
                or stored_recovery.recovery_epoch != record.recovery_epoch
                or stored_recovery.scanned_through_generation
                < record.generation
                or stored_recovery.status != "COMPLETE"
                or stored_recovery.observed_at > moment
            ):
                raise ExecutionStateDeniedError(
                    "frontier recovery evidence scope mismatch"
                )
            claim = self._validate_dispatch_claim_binding(
                row=row,
                record=record,
                eligibility_claim=eligibility.claim,
                owner_id=owner,
                token=token_value,
                dispatch_timestamp=moment,
                current_epoch=current_epoch,
            )
            if (
                moment
                != eligibility.provenance.pre_dispatch_decision.evaluation_timestamp
                or moment < record.updated_at
            ):
                raise ExecutionStateDeniedError(
                    "dispatch timestamp is not bound to provenance"
                )
            receipt = self._insert_receipt(
                record_id=record.record_id,
                submission_id=record.submission_id,
                target=record.target,
                generation=record.generation,
                recovery_epoch=record.recovery_epoch,
                committed_at=moment,
            )
            new_record = self._update_submission_state(
                record=record,
                new_state="DISPATCHING",
                updated_at=moment,
                recovery_epoch=record.recovery_epoch,
                current_receipt_id=receipt.receipt_id,
                dispatch_decision_json=decision_json,
            )
            self._write_audit(
                submission_id=record.submission_id,
                target_json=target_json,
                prior_state="PREPARED",
                new_state="DISPATCHING",
                generation=record.generation,
                recovery_epoch=record.recovery_epoch,
                event_kind="DISPATCHING",
                reason="MARK_DISPATCHING",
                observed_at=moment,
                owner_id=owner,
                token_reference=claim.claim_id,
                claim_id=claim.claim_id,
            )
            self._commit()
            return new_record
        except ExecutionStateError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError(
                "mark_dispatching transaction failed"
            ) from exc

    def persist_transport_outcome(
        self,
        outcome: BrokerSubmissionOutcome,
        target: ExecutionTargetBinding,
        *,
        owner_id: str,
        token: str,
        observed_at: datetime,
    ) -> DurableSubmissionRecord:
        self._require_recovered()
        if not isinstance(outcome, BrokerSubmissionOutcome):
            raise TypeError("outcome must be a BrokerSubmissionOutcome")
        if not isinstance(target, ExecutionTargetBinding):
            raise TypeError("target must be an ExecutionTargetBinding")
        owner = _opaque(owner_id, name="owner_id")
        token_value = _opaque(token, name="token")
        moment = _utc(observed_at, name="observed_at")
        submission_id = outcome.submission_id
        target_json = target.model_dump_json()
        outcome_json = outcome.model_dump_json()
        _check_size(outcome_json, name="transport outcome")
        state_by_status: dict[str, tuple[StateLiteral, str]] = {
            "ACCEPTED": ("ACKNOWLEDGED", "TRANSPORT_ACCEPTED"),
            "REJECTED": ("REJECTED", "TRANSPORT_REJECTED"),
            "OUTCOME_UNKNOWN": (
                "OUTCOME_UNKNOWN",
                "TRANSPORT_OUTCOME_UNKNOWN",
            ),
        }
        try:
            self._begin_immediate()
            row = self._load_submission_row(submission_id, target_json)
            if row is None:
                raise ExecutionStateNotFoundError(
                    "submission identity was not found"
                )
            record = self._decode_record(row)
            if row["state"] == "DISPATCHING":
                active_claim_id = row["active_claim_id"]
                if not isinstance(active_claim_id, str):
                    raise ExecutionStateDeniedError(
                        "dispatching row has no active claim"
                    )
                claim = self._decode_claim(active_claim_id, record=record)
                if claim.owner_id != owner or claim.token != token_value:
                    raise ExecutionStateDeniedError(
                        "transport outcome owner/token mismatch"
                    )
                if moment < record.updated_at:
                    raise ExecutionStateArgumentError(
                        "outcome precedes the dispatching transition"
                    )
                new_state, reason = state_by_status[outcome.status]
                payload_sequence = self._insert_payload(
                    record_id=record.record_id,
                    submission_id=submission_id,
                    target_json=target_json,
                    payload_kind="TRANSPORT_OUTCOME",
                    payload_json=outcome_json,
                    observed_at=moment,
                )
                receipt = self._insert_receipt(
                    record_id=record.record_id,
                    submission_id=record.submission_id,
                    target=record.target,
                    generation=record.generation,
                    recovery_epoch=record.recovery_epoch,
                    committed_at=moment,
                )
                new_record = self._update_submission_state(
                    record=record,
                    new_state=new_state,
                    updated_at=moment,
                    recovery_epoch=record.recovery_epoch,
                    current_receipt_id=receipt.receipt_id,
                    dispatch_decision_json=row["dispatch_decision_json"],
                )
                self._write_audit(
                    submission_id=record.submission_id,
                    target_json=target_json,
                    prior_state="DISPATCHING",
                    new_state=new_state,
                    generation=record.generation,
                    recovery_epoch=record.recovery_epoch,
                    event_kind="TRANSPORT_OUTCOME",
                    reason=reason,
                    observed_at=moment,
                    owner_id=owner,
                    token_reference=claim.claim_id,
                    claim_id=claim.claim_id,
                    payload_sequence=payload_sequence,
                    status=outcome.status,
                )
                self._commit()
                return new_record
            if row["state"] in {"ACKNOWLEDGED", "REJECTED", "OUTCOME_UNKNOWN"}:
                latest = self._latest_payload(
                    record_id=record.record_id,
                    payload_kind="TRANSPORT_OUTCOME",
                )
                if (
                    latest is not None
                    and latest["payload_json"] == outcome_json
                    and latest["observed_at"] == _canonical_instant(moment)
                ):
                    self._write_audit(
                        submission_id=record.submission_id,
                        target_json=target_json,
                        prior_state=record.state,
                        new_state=record.state,
                        generation=record.generation,
                        recovery_epoch=record.recovery_epoch,
                        event_kind="TRANSPORT_OUTCOME",
                        reason="TRANSPORT_OUTCOME_REPLAY",
                        observed_at=moment,
                        owner_id=owner,
                        payload_sequence=int(latest["payload_sequence"]),
                        status=outcome.status,
                    )
                    self._commit()
                    return record
                if row["state"] == "OUTCOME_UNKNOWN" and latest is None:
                    raise ExecutionStateDeniedError(
                        "unknown state was not produced by a transport outcome"
                    )
                raise ExecutionStateConflictError(
                    "contradictory transport outcome"
                )
            raise ExecutionStateDeniedError(
                "transport outcome is not permitted in this state"
            )
        except ExecutionStateError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError(
                "transport outcome transaction failed"
            ) from exc

    def persist_pre_dispatch_no_effect(
        self,
        submission_id: str,
        target: ExecutionTargetBinding,
        proof: BrokerPreDispatchError,
        *,
        owner_id: str,
        token: str,
        observed_at: datetime,
    ) -> DurableSubmissionRecord:
        self._require_recovered()
        if not isinstance(submission_id, str):
            raise TypeError("submission_id must be a string")
        if not isinstance(target, ExecutionTargetBinding):
            raise TypeError("target must be an ExecutionTargetBinding")
        if not isinstance(proof, BrokerPreDispatchError):
            raise TypeError("proof must be a BrokerPreDispatchError")
        owner = _opaque(owner_id, name="owner_id")
        token_value = _opaque(token, name="token")
        proof_reference = _opaque(str(proof), name="proof_reference")
        moment = _utc(observed_at, name="observed_at")
        target_json = target.model_dump_json()
        payload = {
            "classification": "BROKER_PRE_DISPATCH_NO_EFFECT",
            "observed_at": _canonical_instant(moment),
            "proof_reference": proof_reference,
            "submission_id": submission_id,
            "target_json": target_json,
        }
        payload_json = _canonical_json(payload)
        _check_size(payload_json, name="no-effect payload")
        try:
            self._begin_immediate()
            row = self._load_submission_row(submission_id, target_json)
            if row is None:
                raise ExecutionStateNotFoundError(
                    "submission identity was not found"
                )
            record = self._decode_record(row)
            if row["state"] == "DISPATCHING":
                active_claim_id = row["active_claim_id"]
                if not isinstance(active_claim_id, str):
                    raise ExecutionStateDeniedError(
                        "dispatching row has no active claim"
                    )
                claim = self._decode_claim(active_claim_id, record=record)
                if claim.owner_id != owner or claim.token != token_value:
                    raise ExecutionStateDeniedError(
                        "no-effect proof owner/token mismatch"
                    )
                if moment < record.updated_at:
                    raise ExecutionStateArgumentError(
                        "no-effect proof precedes the dispatching transition"
                    )
                payload_sequence = self._insert_payload(
                    record_id=record.record_id,
                    submission_id=submission_id,
                    target_json=target_json,
                    payload_kind="PRE_DISPATCH_NO_EFFECT",
                    payload_json=payload_json,
                    observed_at=moment,
                )
                receipt = self._insert_receipt(
                    record_id=record.record_id,
                    submission_id=record.submission_id,
                    target=record.target,
                    generation=record.generation,
                    recovery_epoch=record.recovery_epoch,
                    committed_at=moment,
                )
                new_record = self._update_submission_state(
                    record=record,
                    new_state="NO_EFFECT_CONFIRMED",
                    updated_at=moment,
                    recovery_epoch=record.recovery_epoch,
                    current_receipt_id=receipt.receipt_id,
                    dispatch_decision_json=row["dispatch_decision_json"],
                )
                self._write_audit(
                    submission_id=record.submission_id,
                    target_json=target_json,
                    prior_state="DISPATCHING",
                    new_state="NO_EFFECT_CONFIRMED",
                    generation=record.generation,
                    recovery_epoch=record.recovery_epoch,
                    event_kind="PRE_DISPATCH_NO_EFFECT",
                    reason="PRE_DISPATCH_NO_EFFECT",
                    observed_at=moment,
                    owner_id=owner,
                    token_reference=claim.claim_id,
                    claim_id=claim.claim_id,
                    payload_sequence=payload_sequence,
                    status="NO_EFFECT_CONFIRMED",
                )
                self._commit()
                return new_record
            if row["state"] == "NO_EFFECT_CONFIRMED":
                latest = self._latest_payload(
                    record_id=record.record_id,
                    payload_kind="PRE_DISPATCH_NO_EFFECT",
                )
                if latest is not None and latest["payload_json"] == payload_json:
                    self._write_audit(
                        submission_id=record.submission_id,
                        target_json=target_json,
                        prior_state=record.state,
                        new_state=record.state,
                        generation=record.generation,
                        recovery_epoch=record.recovery_epoch,
                        event_kind="PRE_DISPATCH_NO_EFFECT",
                        reason="PRE_DISPATCH_NO_EFFECT_REPLAY",
                        observed_at=moment,
                        owner_id=owner,
                        payload_sequence=int(latest["payload_sequence"]),
                        status="NO_EFFECT_CONFIRMED",
                    )
                    self._commit()
                    return record
                raise ExecutionStateConflictError(
                    "contradictory no-effect proof"
                )
            raise ExecutionStateDeniedError(
                "no-effect proof is not permitted in this state"
            )
        except ExecutionStateError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError(
                "no-effect proof transaction failed"
            ) from exc

    def apply_reconciliation(
        self,
        submission_id: str,
        target: ExecutionTargetBinding,
        *,
        status: Literal["ACCEPTED", "REJECTED", "NO_EFFECT_CONFIRMED"],
        evidence_reference: str,
        observed_at: datetime,
    ) -> DurableSubmissionRecord:
        self._require_recovered()
        if not isinstance(submission_id, str):
            raise TypeError("submission_id must be a string")
        if not isinstance(target, ExecutionTargetBinding):
            raise TypeError("target must be an ExecutionTargetBinding")
        if status not in {"ACCEPTED", "REJECTED", "NO_EFFECT_CONFIRMED"}:
            raise ExecutionStateArgumentError("unsupported reconciliation status")
        reference = _opaque(evidence_reference, name="evidence_reference")
        moment = _utc(observed_at, name="observed_at")
        target_json = target.model_dump_json()
        payload = {
            "evidence_reference": reference,
            "observed_at": _canonical_instant(moment),
            "status": status,
            "submission_id": submission_id,
            "target_json": target_json,
        }
        payload_json = _canonical_json(payload)
        _check_size(payload_json, name="reconciliation payload")
        state_by_status: dict[str, tuple[StateLiteral, str]] = {
            "ACCEPTED": ("ACKNOWLEDGED", "RECONCILIATION_ACCEPTED"),
            "REJECTED": ("REJECTED", "RECONCILIATION_REJECTED"),
            "NO_EFFECT_CONFIRMED": (
                "NO_EFFECT_CONFIRMED",
                "RECONCILIATION_NO_EFFECT",
            ),
        }
        try:
            self._begin_immediate()
            row = self._load_submission_row(submission_id, target_json)
            if row is None:
                raise ExecutionStateNotFoundError(
                    "submission identity was not found"
                )
            record = self._decode_record(row)
            if row["state"] == "OUTCOME_UNKNOWN":
                if moment < record.updated_at:
                    raise ExecutionStateArgumentError(
                        "reconciliation precedes the unknown outcome"
                    )
                new_state, reason = state_by_status[status]
                payload_sequence = self._insert_payload(
                    record_id=record.record_id,
                    submission_id=submission_id,
                    target_json=target_json,
                    payload_kind="RECONCILIATION",
                    payload_json=payload_json,
                    observed_at=moment,
                )
                receipt = self._insert_receipt(
                    record_id=record.record_id,
                    submission_id=record.submission_id,
                    target=record.target,
                    generation=record.generation,
                    recovery_epoch=record.recovery_epoch,
                    committed_at=moment,
                )
                new_record = self._update_submission_state(
                    record=record,
                    new_state=new_state,
                    updated_at=moment,
                    recovery_epoch=record.recovery_epoch,
                    current_receipt_id=receipt.receipt_id,
                    dispatch_decision_json=row["dispatch_decision_json"],
                )
                self._write_audit(
                    submission_id=record.submission_id,
                    target_json=target_json,
                    prior_state="OUTCOME_UNKNOWN",
                    new_state=new_state,
                    generation=record.generation,
                    recovery_epoch=record.recovery_epoch,
                    event_kind="RECONCILIATION",
                    reason=reason,
                    observed_at=moment,
                    payload_sequence=payload_sequence,
                    status=status,
                )
                self._commit()
                return new_record
            if row["state"] in {
                "ACKNOWLEDGED",
                "REJECTED",
                "NO_EFFECT_CONFIRMED",
            }:
                latest = self._latest_payload(
                    record_id=record.record_id,
                    payload_kind="RECONCILIATION",
                )
                if latest is not None and latest["payload_json"] == payload_json:
                    self._write_audit(
                        submission_id=record.submission_id,
                        target_json=target_json,
                        prior_state=record.state,
                        new_state=record.state,
                        generation=record.generation,
                        recovery_epoch=record.recovery_epoch,
                        event_kind="RECONCILIATION",
                        reason="RECONCILIATION_REPLAY",
                        observed_at=moment,
                        payload_sequence=int(latest["payload_sequence"]),
                        status=status,
                    )
                    self._commit()
                    return record
                raise ExecutionStateConflictError(
                    "contradictory reconciliation"
                )
            raise ExecutionStateDeniedError(
                "reconciliation is not permitted in this state"
            )
        except ExecutionStateError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError(
                "reconciliation transaction failed"
            ) from exc

    def read_audit(
        self,
        submission_id: str,
        target: ExecutionTargetBinding,
    ) -> tuple[str, ...]:
        self._require_recovered()
        if not isinstance(submission_id, str):
            raise TypeError("submission_id must be a string")
        if not isinstance(target, ExecutionTargetBinding):
            raise TypeError("target must be an ExecutionTargetBinding")
        target_json = target.model_dump_json()
        try:
            self._begin_read()
            row = self._load_submission_row(submission_id, target_json)
            if row is None:
                raise ExecutionStateNotFoundError(
                    "submission identity was not found"
                )
            self._validate_submission_row(row)
            rows = self._connection.execute(
                """
                SELECT * FROM audit_events
                WHERE row_scope = 'SUBMISSION'
                  AND submission_id = ?
                  AND target_json = ?
                ORDER BY event_sequence
                """,
                (submission_id, target_json),
            ).fetchall()
            result = tuple(self._verify_audit_row(row) for row in rows)
            self._commit()
            return result
        except ExecutionStateError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            if _is_busy(exc):
                raise ExecutionStateBusyError("database is busy") from exc
            raise ExecutionStateCorruptionError(
                "audit read transaction failed"
            ) from exc

    def _verify_audit_row(self, row: sqlite3.Row) -> str:
        event_json = row["event_json"]
        if not isinstance(event_json, str):
            raise ExecutionStateCorruptionError("audit event JSON is missing")
        try:
            data = json.loads(event_json)
        except (TypeError, ValueError) as exc:
            raise ExecutionStateCorruptionError(
                "audit event JSON is malformed"
            ) from exc
        if not isinstance(data, dict) or _canonical_json(data) != event_json:
            raise ExecutionStateCorruptionError(
                "audit event JSON is not canonical"
            )
        expected = {
            "event_sequence": row["event_sequence"],
            "row_scope": _ROW_SCOPE_SUBMISSION,
            "submission_id": row["submission_id"],
            "target_json": row["target_json"],
            "prior_state": row["prior_state"],
            "new_state": row["new_state"],
            "generation": row["generation"],
            "recovery_epoch": row["recovery_epoch"],
            "event_kind": row["event_kind"],
            "observed_at": row["observed_at"],
            "owner_id": row["owner_id"],
            "token_reference": row["token_reference"],
            "claim_id": row["claim_id"],
            "payload_sequence": row["payload_sequence"],
            "status": row["status"],
            "reason": row["reason"],
        }
        if data != expected:
            raise ExecutionStateCorruptionError(
                "audit event contradicts its extracted columns"
            )
        if row["row_scope"] != _ROW_SCOPE_SUBMISSION:
            raise ExecutionStateCorruptionError(
                "boundary anchor was validated as a submission audit row"
            )
        _parse_instant(row["observed_at"], name="audit observed_at")
        if (str(data["event_kind"]), str(data["reason"])) not in _REASON_PAIRS:
            raise ExecutionStateCorruptionError(
                "audit event kind/reason pair is not contracted"
            )
        return event_json

    def _verify_boundary_row(self, row: sqlite3.Row) -> str:
        event_json = row["event_json"]
        if not isinstance(event_json, str):
            raise ExecutionStateCorruptionError("boundary anchor JSON is missing")
        try:
            data = json.loads(event_json)
        except (TypeError, ValueError) as exc:
            raise ExecutionStateCorruptionError(
                "boundary anchor JSON is malformed"
            ) from exc
        if not isinstance(data, dict) or _canonical_json(data) != event_json:
            raise ExecutionStateCorruptionError(
                "boundary anchor JSON is not canonical"
            )
        phase = row["boundary_phase"]
        if phase not in _CUT_PHASES:
            raise ExecutionStateCorruptionError(
                "boundary anchor phase is not contracted"
            )
        expected_kind = _CUT_BEGIN_KIND if phase == "BEGIN" else _CUT_END_KIND
        expected = {
            "event_sequence": row["event_sequence"],
            "row_scope": _ROW_SCOPE_BOUNDARY,
            "target_json": row["target_json"],
            "recovery_epoch": row["recovery_epoch"],
            "event_kind": expected_kind,
            "boundary_phase": phase,
            "boundary_run_sequence": row["boundary_run_sequence"],
            "observed_at": row["observed_at"],
            "reason": expected_kind,
        }
        if data != expected:
            raise ExecutionStateCorruptionError(
                "boundary anchor contradicts its extracted columns"
            )
        if (
            row["row_scope"] != _ROW_SCOPE_BOUNDARY
            or row["submission_id"] is not None
            or row["prior_state"] is not None
            or row["new_state"] is not None
            or row["generation"] is not None
            or row["owner_id"] is not None
            or row["token_reference"] is not None
            or row["claim_id"] is not None
            or row["payload_sequence"] is not None
            or row["status"] is not None
            or row["event_kind"] != expected_kind
            or row["reason"] != expected_kind
            or not isinstance(row["boundary_run_sequence"], int)
            or row["boundary_run_sequence"] < 1
        ):
            raise ExecutionStateCorruptionError(
                "boundary anchor row shape is invalid"
            )
        _parse_instant(row["observed_at"], name="boundary observed_at")
        return event_json
