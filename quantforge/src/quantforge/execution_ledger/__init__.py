"""Broker-neutral durable execution ledger (EXECSTATE-002).

The package exposes exactly the frozen EXECSTATE-002 public surface: the
read-and-record ledger facade, the exportable value objects, the locked
transport boundary, and the error family. EXECSTATE-001 schema-v2 remains
the only authoritative execution state machine; nothing in this package
grants dispatch authority.
"""

from .adapter import BrokerAdapter, LockedBrokerAdapter
from .errors import (
    BrokerAdapterNotImplementedError,
    ExecutionLedgerBusyError,
    ExecutionLedgerCommitUnknownError,
    ExecutionLedgerCorruptionError,
    ExecutionLedgerDeniedError,
    ExecutionLedgerError,
    ExecutionLedgerNotFoundError,
    ExecutionLedgerPathError,
    ExecutionLedgerSchemaError,
    ReconciliationConflict,
)
from .ledger import DurableExecutionLedger
from .models import (
    AttemptPhase,
    BrokerObservationStatus,
    BrokerOrderObservation,
    ExecutionAttemptView,
    ReconciliationAssessment,
    ReconciliationReason,
    ReconciliationResult,
)

__all__ = [
    "AttemptPhase",
    "BrokerAdapter",
    "BrokerAdapterNotImplementedError",
    "BrokerObservationStatus",
    "BrokerOrderObservation",
    "DurableExecutionLedger",
    "ExecutionAttemptView",
    "ExecutionLedgerBusyError",
    "ExecutionLedgerCommitUnknownError",
    "ExecutionLedgerCorruptionError",
    "ExecutionLedgerDeniedError",
    "ExecutionLedgerError",
    "ExecutionLedgerNotFoundError",
    "ExecutionLedgerPathError",
    "ExecutionLedgerSchemaError",
    "LockedBrokerAdapter",
    "ReconciliationAssessment",
    "ReconciliationConflict",
    "ReconciliationReason",
    "ReconciliationResult",
]
