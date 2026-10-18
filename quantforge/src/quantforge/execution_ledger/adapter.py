"""Locked broker-neutral transport boundary for the execution ledger.

The boundary is declarative only. It holds no state and performs no
transport, credential, external-process, entropy, or clock work. Every method
fails closed until a future contract authorizes a real adapter.
"""

from __future__ import annotations

from typing import Protocol

from quantforge.broker import BrokerSubmissionEnvelope, BrokerSubmissionOutcome
from quantforge.frontier import ExecutionTargetBinding

from .errors import BrokerAdapterNotImplementedError
from .models import BrokerOrderObservation

__all__ = ["BrokerAdapter", "LockedBrokerAdapter"]


class BrokerAdapter(Protocol):
    """Broker-neutral interface whose default behavior is locked."""

    __slots__ = ()

    def submit(
        self, envelope: BrokerSubmissionEnvelope
    ) -> BrokerSubmissionOutcome:
        raise BrokerAdapterNotImplementedError("submit")

    def query_order(
        self, submission_id: str, target: ExecutionTargetBinding
    ) -> BrokerOrderObservation:
        raise BrokerAdapterNotImplementedError("query_order")

    def cancel_order(
        self, submission_id: str, target: ExecutionTargetBinding
    ) -> None:
        raise BrokerAdapterNotImplementedError("cancel_order")


class LockedBrokerAdapter(BrokerAdapter):
    """The only concrete adapter: every method fails closed, without state."""

    __slots__ = ()

    def submit(
        self, envelope: BrokerSubmissionEnvelope
    ) -> BrokerSubmissionOutcome:
        raise BrokerAdapterNotImplementedError("submit")

    def query_order(
        self, submission_id: str, target: ExecutionTargetBinding
    ) -> BrokerOrderObservation:
        raise BrokerAdapterNotImplementedError("query_order")

    def cancel_order(
        self, submission_id: str, target: ExecutionTargetBinding
    ) -> None:
        raise BrokerAdapterNotImplementedError("cancel_order")
