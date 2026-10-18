"""Public error boundary for the broker-neutral execution ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ReconciliationAssessment


class ExecutionLedgerError(ValueError):
    """Base semantic error for the execution-ledger subsystem."""


class ExecutionLedgerPathError(ExecutionLedgerError):
    """Sidecar path, open, permission, or filesystem failure."""


class ExecutionLedgerSchemaError(ExecutionLedgerError):
    """Sidecar schema, version, or metadata mismatch."""


class ExecutionLedgerCorruptionError(ExecutionLedgerError):
    """Tampered, malformed, or internally inconsistent ledger truth."""


class ExecutionLedgerBusyError(ExecutionLedgerError):
    """Sidecar lock contention with no implicit retry."""


class ExecutionLedgerNotFoundError(ExecutionLedgerError):
    """No exact ledger identity exists."""


class ExecutionLedgerDeniedError(ExecutionLedgerError):
    """A valid but unsafe or malformed request was denied."""


class ExecutionLedgerCommitUnknownError(ExecutionLedgerError):
    """A sidecar commit outcome is unknown and must be resolved by reopening."""


class BrokerAdapterNotImplementedError(ExecutionLedgerError):
    """Every broker adapter method defaults to this locked error."""

    def __init__(self, method: str) -> None:
        if not isinstance(method, str) or not method:
            raise TypeError("method must be a non-empty string")
        self.method = method
        super().__init__(f"broker adapter method is not implemented: {method}")


class ReconciliationConflict(ExecutionLedgerError):
    """External reconciliation disagreement.

    This class is deliberately unrelated to the corruption error family: a
    disagreement with an external observation never means local durable truth
    is corrupt, and a local contradiction is never reported as a
    reconciliation conflict.
    """

    def __init__(
        self, message: str, *, assessment: ReconciliationAssessment
    ) -> None:
        if assessment is None:
            raise TypeError("assessment is required")
        self.assessment = assessment
        super().__init__(message)
