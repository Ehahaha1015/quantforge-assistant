"""Deterministic OPERATOR-001 error taxonomy."""

from __future__ import annotations


class OperatorError(ValueError):
    """Base semantic error for OPERATOR-001."""


class OperatorArgumentError(OperatorError):
    """Malformed caller input."""


class OperatorAuthenticationError(OperatorError):
    """Verification failed or authentication input was malformed."""


class OperatorTargetMismatchError(OperatorError):
    """A target's durable scope differs from the requested scope."""


class OperatorConflictError(OperatorError):
    """A request identity was reused with a different canonical request."""


class OperatorSchemaError(OperatorError):
    """Schema, metadata, trigger or sqlite_master mismatch."""


class OperatorUnsupportedSchemaError(OperatorSchemaError):
    """Unknown or unsupported durable schema version."""


class OperatorPathError(OperatorError):
    """Path, open, permission or filesystem failure."""


class OperatorBusyError(OperatorError):
    """SQLite contention without implicit retry."""


class OperatorCorruptionError(OperatorError):
    """Tampered or internally inconsistent durable state."""


class OperatorCommitUnknownError(OperatorError):
    """The outcome of a COMMIT could not be determined."""


class OperatorSequenceExhaustedError(OperatorError):
    """The signed 64-bit control-sequence domain is exhausted."""


class OperatorNotFoundError(OperatorError):
    """No committed operator control state exists for the scope."""


class OperatorClosedError(OperatorError):
    """The store has been closed."""


__all__ = [
    "OperatorArgumentError",
    "OperatorAuthenticationError",
    "OperatorBusyError",
    "OperatorClosedError",
    "OperatorCommitUnknownError",
    "OperatorConflictError",
    "OperatorCorruptionError",
    "OperatorError",
    "OperatorNotFoundError",
    "OperatorPathError",
    "OperatorSchemaError",
    "OperatorSequenceExhaustedError",
    "OperatorTargetMismatchError",
    "OperatorUnsupportedSchemaError",
]
