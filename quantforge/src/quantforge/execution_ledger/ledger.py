"""Composition facade for the broker-neutral execution ledger.

The facade composes four reviewed layers without adding a new authority:

* EXECSTATE remains the only authoritative state machine; the facade reads it
  through `read` and `read_audit` and never mutates it.
* The sidecar stores canonical external observation evidence only.
* Reconciliation is a pure informational judgement.
* Projection is a read-only attempt view; phases are never persisted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from quantforge.execstate import (
    ExecutionStateNotFoundError,
    SQLiteExecutionStateStore,
)
from quantforge.frontier import (
    DurableCommitReceipt,
    DurableSubmissionRecord,
    ExecutionTargetBinding,
)

from . import projection, reconciliation
from .errors import (
    ExecutionLedgerCorruptionError,
    ExecutionLedgerDeniedError,
    ExecutionLedgerNotFoundError,
)
from .identity import derive_attempt_id
from .models import (
    BrokerOrderObservation,
    ExecutionAttemptView,
    ReconciliationAssessment,
)
from .projection import AuditFact
from .sidecar import SQLiteObservationStore

__all__ = ["DurableExecutionLedger"]


class DurableExecutionLedger:
    """Read-and-record facade over EXECSTATE evidence and sidecar evidence.

    The constructor requires an already-open, recovered
    `SQLiteExecutionStateStore`. The facade never calls recovery itself; a
    store that has not completed recovery fails closed on the first durable
    read. Closing the facade closes the sidecar only; the caller keeps
    ownership of the EXECSTATE store.
    """

    def __init__(
        self,
        execstate_store: SQLiteExecutionStateStore,
        sidecar_path: str | Path,
    ) -> None:
        if not isinstance(execstate_store, SQLiteExecutionStateStore):
            raise TypeError(
                "execstate_store must be a SQLiteExecutionStateStore"
            )
        self._store = execstate_store
        self._sidecar = SQLiteObservationStore(sidecar_path)
        self._closed = False

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._sidecar.close()

    def assess_observation(
        self, observation: BrokerOrderObservation
    ) -> ReconciliationAssessment:
        """Return the pure assessment for one observation without writing."""

        self._require_open()
        if not isinstance(observation, BrokerOrderObservation):
            raise TypeError(
                "observation must be a BrokerOrderObservation"
            )
        attempt_id = derive_attempt_id(
            observation.submission_id,
            observation.target.model_dump_json(),
            observation.generation,
            observation.recovery_epoch,
        )
        return self._assess_recorded(
            observation, self._load_recorded(attempt_id)
        )

    def _assess_recorded(
        self,
        observation: BrokerOrderObservation,
        recorded: tuple[BrokerOrderObservation, ...],
    ) -> ReconciliationAssessment:
        _record, _receipt, facts = self._load_durable(
            observation.submission_id, observation.target
        )
        attempt_id = derive_attempt_id(
            observation.submission_id,
            observation.target.model_dump_json(),
            observation.generation,
            observation.recovery_epoch,
        )
        view = self._find_view(facts, observation, attempt_id)
        if view is None:
            raise ExecutionLedgerNotFoundError(
                "observation references an unknown execution attempt"
            )
        return reconciliation.assess(
            execstate_state=view.execstate_state,
            observation=observation,
            recorded=self._require_coherent(recorded),
            attempt_id=attempt_id,
        )

    def record_observation(
        self, observation: BrokerOrderObservation
    ) -> ReconciliationAssessment:
        """Assess and append one non-conflicting observation as evidence.

        The assessment that authorizes the append is computed inside the
        same sidecar `BEGIN IMMEDIATE` writer transaction as the append, so
        a concurrent writer can never persist opposing evidence between the
        assessment and the append. A conflict rolls back with zero writes.
        """

        self._require_open()
        if not isinstance(observation, BrokerOrderObservation):
            raise TypeError(
                "observation must be a BrokerOrderObservation"
            )
        stored, assessment = self._sidecar._append_assessed(
            observation,
            lambda recorded: self._assess_recorded(
                observation, recorded
            ),
        )
        if stored.observation_id is None:
            raise ExecutionLedgerCorruptionError(
                "recorded observation has no persisted identity"
            )
        return reconciliation.recorded_assessment(
            assessment, stored.observation_id
        )

    def read_attempt(
        self, submission_id: str, target: ExecutionTargetBinding
    ) -> ExecutionAttemptView:
        """Reconstruct the current read-only attempt view."""

        self._require_open()
        record, _receipt, views = self._views(submission_id, target)
        current_id = derive_attempt_id(
            record.submission_id,
            record.target.model_dump_json(),
            record.generation,
            record.recovery_epoch,
        )
        for view in views:
            if view.attempt_id == current_id:
                return view
        raise ExecutionLedgerCorruptionError(
            "current durable attempt is missing from audit history"
        )

    def read_attempt_history(
        self, submission_id: str, target: ExecutionTargetBinding
    ) -> tuple[ExecutionAttemptView, ...]:
        """Reconstruct every durable attempt view in audit order."""

        self._require_open()
        _record, _receipt, views = self._views(submission_id, target)
        return views

    def read_observations(
        self, attempt_id: str
    ) -> tuple[BrokerOrderObservation, ...]:
        """Read recorded evidence for one attempt in sequence order."""

        self._require_open()
        return self._load_recorded(attempt_id)

    def _require_open(self) -> None:
        if self._closed:
            raise ExecutionLedgerDeniedError(
                "execution ledger facade is closed"
            )

    def _load_durable(
        self, submission_id: str, target: ExecutionTargetBinding
    ) -> tuple[
        DurableSubmissionRecord,
        DurableCommitReceipt,
        tuple[AuditFact, ...],
    ]:
        if not isinstance(submission_id, str):
            raise TypeError("submission_id must be a string")
        if not isinstance(target, ExecutionTargetBinding):
            raise TypeError(
                "target must be an ExecutionTargetBinding"
            )
        try:
            record, receipt = self._store.read(submission_id, target)
            audit_rows = self._store.read_audit(submission_id, target)
        except ExecutionStateNotFoundError as exc:
            raise ExecutionLedgerNotFoundError(
                "no durable submission exists for this identity"
            ) from exc
        if (
            record.submission_id != submission_id
            or record.target != target
        ):
            raise ExecutionLedgerCorruptionError(
                "durable record contradicts the requested identity"
            )
        facts = tuple(self._parse_fact(row) for row in audit_rows)
        return record, receipt, facts

    def _parse_fact(self, text: object) -> AuditFact:
        if not isinstance(text, str):
            raise ExecutionLedgerCorruptionError(
                "stored audit row is not text"
            )
        try:
            document = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ExecutionLedgerCorruptionError(
                "stored audit row is malformed"
            ) from exc
        if not isinstance(document, dict):
            raise ExecutionLedgerCorruptionError(
                "stored audit row is not an object"
            )
        return AuditFact.from_mapping(document)

    def _views(
        self, submission_id: str, target: ExecutionTargetBinding
    ) -> tuple[
        DurableSubmissionRecord,
        DurableCommitReceipt,
        tuple[ExecutionAttemptView, ...],
    ]:
        record, receipt, facts = self._load_durable(submission_id, target)
        if not facts:
            raise ExecutionLedgerCorruptionError(
                "durable submission has no audit facts"
            )
        base = projection.project_history(
            submission_id=submission_id,
            target=target,
            audit_facts=facts,
            current_execstate_state=record.state,
            current_receipt_id=receipt.receipt_id,
        )
        observations: list[BrokerOrderObservation] = []
        for view in base:
            observations.extend(self._load_recorded(view.attempt_id))
        views = projection.project_history(
            submission_id=submission_id,
            target=target,
            audit_facts=facts,
            observations=tuple(observations),
            current_execstate_state=record.state,
            current_receipt_id=receipt.receipt_id,
        )
        return record, receipt, views

    def _find_view(
        self,
        facts: tuple[AuditFact, ...],
        observation: BrokerOrderObservation,
        attempt_id: str,
    ) -> ExecutionAttemptView | None:
        record, receipt = self._store.read(
            observation.submission_id, observation.target
        )
        views = projection.project_history(
            submission_id=observation.submission_id,
            target=observation.target,
            audit_facts=facts,
            current_execstate_state=record.state,
            current_receipt_id=receipt.receipt_id,
        )
        for view in views:
            if view.attempt_id == attempt_id:
                return view
        return None

    def _load_recorded(
        self, attempt_id: str
    ) -> tuple[BrokerOrderObservation, ...]:
        return self._require_coherent(
            self._sidecar.read_observations(attempt_id)
        )

    @staticmethod
    def _require_coherent(
        recorded: tuple[BrokerOrderObservation, ...],
    ) -> tuple[BrokerOrderObservation, ...]:
        for index, item in enumerate(recorded):
            others = recorded[:index] + recorded[index + 1 :]
            if reconciliation.is_contradictory(item, others):
                raise ExecutionLedgerCorruptionError(
                    "recorded observations contradict each other"
                )
        return recorded
