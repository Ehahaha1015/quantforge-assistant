"""EXECSTATE-002 section 10: the frozen package-root public surface.

Every contracted export must be importable from the package root and must be
the exact object defined by its owning module, so no shadowing, aliasing, or
partial re-export can drift from the frozen contract.
"""

from __future__ import annotations

from types import ModuleType

import quantforge.execution_ledger as package
from quantforge.execution_ledger import (
    adapter,
    errors,
    ledger,
    models,
    projection,
    reconciliation,
    sidecar,
)

#: EXECSTATE-002 section 10 "Public exports (draft)" freeze.
_CONTRACTED_EXPORTS: dict[str, ModuleType] = {
    "DurableExecutionLedger": ledger,
    "AttemptPhase": models,
    "ExecutionAttemptView": models,
    "BrokerOrderObservation": models,
    "BrokerObservationStatus": models,
    "ReconciliationAssessment": models,
    "ReconciliationResult": models,
    "BrokerAdapter": adapter,
    "LockedBrokerAdapter": adapter,
    "ExecutionLedgerError": errors,
    "ExecutionLedgerPathError": errors,
    "ExecutionLedgerSchemaError": errors,
    "ExecutionLedgerCorruptionError": errors,
    "ExecutionLedgerBusyError": errors,
    "ExecutionLedgerNotFoundError": errors,
    "ExecutionLedgerDeniedError": errors,
    "ExecutionLedgerCommitUnknownError": errors,
    "BrokerAdapterNotImplementedError": errors,
    "ReconciliationConflict": errors,
}


def test_contracted_exports_are_bound_at_the_package_root() -> None:
    for name, module in _CONTRACTED_EXPORTS.items():
        assert hasattr(package, name), name
        assert getattr(package, name) is getattr(module, name), name
        assert name in package.__all__, name


def test_package_all_is_unique_and_resolvable() -> None:
    assert len(set(package.__all__)) == len(package.__all__)
    for name in package.__all__:
        assert getattr(package, name) is not None, name
        assert not isinstance(getattr(package, name), ModuleType), name
    assert list(package.__all__) == sorted(package.__all__)


def test_object_owning_modules_freeze_their_exports() -> None:
    assert adapter.__all__ == ["BrokerAdapter", "LockedBrokerAdapter"]
    assert ledger.__all__ == ["DurableExecutionLedger"]
    assert reconciliation.__all__ == [
        "COMPATIBILITY_MATRIX",
        "assess",
        "is_contradictory",
        "is_exact_replay",
        "raise_on_conflict",
        "recorded_assessment",
    ]


def test_module_exports_are_unique_and_resolvable() -> None:
    for module in (package, adapter, ledger, projection, reconciliation, sidecar):
        names = module.__all__
        assert len(set(names)) == len(names), module.__name__
        for name in names:
            assert not isinstance(getattr(module, name), ModuleType), (
                module.__name__,
                name,
            )
