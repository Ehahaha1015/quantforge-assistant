"""Public error boundary for durable execution-state persistence."""

from __future__ import annotations


class ExecutionStateError(ValueError):
    """Base semantic error for the execution-state subsystem."""


class ExecutionStateArgumentError(ExecutionStateError):
    """Malformed or invalid caller arguments."""


class ExecutionStatePathError(ExecutionStateError):
    """Database path, open, permission, or filesystem failure."""


class ExecutionStateSchemaError(ExecutionStateError):
    """Database schema/version/metadata mismatch."""


class ExecutionStateCorruptionError(ExecutionStateError):
    """Tampered, malformed, or internally inconsistent durable state."""


class ExecutionStateBusyError(ExecutionStateError):
    """SQLite busy/lock contention with no implicit retry."""


class ExecutionStateCommitUnknownError(ExecutionStateError):
    """A commit outcome is unknown and must be resolved by reopening."""


class ExecutionStateNotFoundError(ExecutionStateError):
    """No exact submission/target identity exists."""


class ExecutionStateDeniedError(ExecutionStateError):
    """A valid but unsafe state/claim request was denied."""


class ExecutionStateConflictError(ExecutionStateError):
    """A contradictory duplicate payload or state transition."""


class ExecutionStateRecoveryRequiredError(ExecutionStateError):
    """The store instance has not completed startup recovery."""
