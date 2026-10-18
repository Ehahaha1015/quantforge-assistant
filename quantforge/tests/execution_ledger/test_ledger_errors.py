from __future__ import annotations

import pytest

from quantforge.execstate import ExecutionStateCorruptionError
from quantforge.execution_ledger import (
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

_SUBCLASSES = (
    ExecutionLedgerPathError,
    ExecutionLedgerSchemaError,
    ExecutionLedgerCorruptionError,
    ExecutionLedgerBusyError,
    ExecutionLedgerNotFoundError,
    ExecutionLedgerDeniedError,
    ExecutionLedgerCommitUnknownError,
    BrokerAdapterNotImplementedError,
    ReconciliationConflict,
)


def test_error_hierarchy() -> None:
    assert issubclass(ExecutionLedgerError, ValueError)
    for error in _SUBCLASSES:
        assert issubclass(error, ExecutionLedgerError)


def test_conflict_and_corruption_are_mutually_distinct() -> None:
    assert not issubclass(ReconciliationConflict, ExecutionLedgerCorruptionError)
    assert not issubclass(ReconciliationConflict, ExecutionStateCorruptionError)
    assert not issubclass(ExecutionLedgerCorruptionError, ReconciliationConflict)


def test_catching_one_class_does_not_catch_the_other() -> None:
    with pytest.raises(ExecutionLedgerCorruptionError):
        raise ExecutionLedgerCorruptionError("corrupt")
    with pytest.raises(ReconciliationConflict):
        raise ReconciliationConflict("conflict", assessment=object())
    try:
        raise ReconciliationConflict("conflict", assessment=object())
    except ExecutionLedgerCorruptionError:  # pragma: no cover
        pytest.fail("conflict was caught as corruption")
    except ReconciliationConflict as exc:
        assert isinstance(exc.assessment, object)


def test_conflict_requires_an_assessment() -> None:
    with pytest.raises(TypeError):
        ReconciliationConflict("conflict")  # type: ignore[call-arg]
    assessment = object()
    error = ReconciliationConflict("conflict", assessment=assessment)
    assert error.assessment is assessment
    assert "conflict" in str(error)


def test_adapter_not_implemented_carries_method_name() -> None:
    error = BrokerAdapterNotImplementedError("submit")
    assert "submit" in str(error)
