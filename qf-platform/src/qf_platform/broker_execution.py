"""Host composition of frozen Core execution contracts and an official plugin.

No HTTP/live enable surface is installed. Production dispatch is paused, even
when a caller supplies an authorized envelope. PAPER qualification exercises the
same risk, operator, Frontier, credential, durable claim and reconciliation path.
Secrets, vendor SDKs and fills stay outside Core/Lab; ACK is never a fill.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, cast

from quantforge.broker import (
    BrokerPreDispatchError,
    BrokerSubmissionEnvelope,
    BrokerSubmissionOutcome,
)
from quantforge.credentials import (
    CredentialBoundary,
    CredentialLease,
    CredentialPurpose,
    CredentialReference,
    CredentialScope,
)
from quantforge.execstate import (
    ExecutionStateNotFoundError,
    SQLiteExecutionStateStore,
)
from quantforge.execution_ledger import (
    BrokerObservationStatus,
    BrokerOrderObservation,
    DurableExecutionLedger,
    ReconciliationResult,
)
from quantforge.frontier import (
    AccountReconciliationEvidence,
    BrokerCapabilityDeclaration,
    ExecutionTargetBinding,
    PriceFreshnessEvidence,
    evaluate_frontier_eligibility,
)
from quantforge.operator import (
    SQLiteOperatorControlStore,
    operator_evidence_for_frontier,
)
from quantforge.portfolio import PortfolioState
from quantforge.predispatch import (
    AuthorizationFreshnessPolicy,
    SubmissionHistoryState,
)
from quantforge.product.storage import local_root, writer_lock
from quantforge.provenance import evaluate_pre_dispatch_with_provenance
from quantforge.risk import RiskLimits

from .accounting_book import CoreAccountingBook
from .storage import Ledger, PlatformError


def _platform_boundary[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def bounded(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except PlatformError:
            raise
        except Exception:
            raise PlatformError("BROKER_EXECUTION_CORE_BOUNDARY_DENIED") from None

    return bounded


@dataclass(frozen=True)
class BrokerRiskObservation:
    """Authenticated, coherent official account/quote/rule input, never AI data.

    Target uses a stable redacted account reference. The selected plugin owns
    the mapping to the actual account in its scoped credential lease. Rules and
    prices require official source evidence; unknowns cannot authorize send.
    """

    target: ExecutionTargetBinding
    portfolio: PortfolioState
    prices: Mapping[str, Decimal]
    available_cash: Decimal
    estimated_fee: Decimal
    observed_at: datetime
    account_hash: str
    price_hash: str
    rule_hash: str
    lot_size: int
    tick_size: Decimal
    limit_price: Decimal
    session_open: bool
    trading_permission: bool
    capability: BrokerCapabilityDeclaration

    def __post_init__(self) -> None:
        object.__setattr__(self, "prices", MappingProxyType(dict(self.prices)))


class ExecutionGateway(Protocol):
    """Selected vendor-supported plugin, with exact Core target identity.

    Implementations must never retry a sent request or log leased material.
    submit accepts a limit/lot validated snapshot, preserves the client ID and
    distinguishes ACK from fills. query uses the client ID and exact attempt;
    absence is UNKNOWN, not proof of no effect. cancel ACK is not finality.
    """

    def observe(
        self,
        envelope: BrokerSubmissionEnvelope,
        target: ExecutionTargetBinding,
        lease: CredentialLease,
    ) -> BrokerRiskObservation: ...

    def submit(
        self,
        envelope: BrokerSubmissionEnvelope,
        observation: BrokerRiskObservation,
        lease: CredentialLease,
    ) -> BrokerSubmissionOutcome: ...

    def query_attempt(
        self,
        submission_id: str,
        target: ExecutionTargetBinding,
        generation: int,
        recovery_epoch: int,
        lease: CredentialLease,
    ) -> BrokerOrderObservation: ...

    def cancel_order(
        self,
        submission_id: str,
        target: ExecutionTargetBinding,
        lease: CredentialLease,
    ) -> None: ...

    def cancel_capability(
        self,
        target: ExecutionTargetBinding,
        lease: CredentialLease,
    ) -> BrokerCancelCapability: ...


@dataclass(frozen=True)
class BrokerCancelCapability:
    target: ExecutionTargetBinding
    supported: bool
    observed_at: datetime
    evidence_hash: str


class BrokerExecutionSession:
    """One backend-owned composition; fresh evidence and no automatic replay.

    Caller supplies an already authorized Core envelope, never a model-created
    enable flag. Core recomputes risk from the authenticated observation.
    A profile lease/revision guard runs before and after observation and before
    send. The product kill latch is independent of Core's authenticated operator
    source. Production money remains disabled under this user's instruction.
    """

    def __init__(
        self,
        root: Path,
        gateway: ExecutionGateway,
        operator: SQLiteOperatorControlStore,
        reference: CredentialReference,
        *,
        binding_guard: Callable[[ExecutionTargetBinding], None],
        paused: Callable[[], bool],
        accounting: CoreAccountingBook,
        credentials: CredentialBoundary | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        owns_operator: bool = False,
    ) -> None:
        self.root = local_root(root)
        self.gateway, self.operator, self.reference = gateway, operator, reference
        self.guard, self.paused, self.clock = binding_guard, paused, clock
        if type(accounting) is not CoreAccountingBook:
            raise PlatformError("INDEPENDENT_CORE_ACCOUNTING_REQUIRED")
        self.accounting = accounting
        audit_root = self.root / "account-checks"
        audit_root.mkdir(exist_ok=True)
        self.account_checks = Ledger(audit_root)
        self.owns_operator = owns_operator
        self.credentials = credentials
        self.store = SQLiteExecutionStateStore(self.root / "execstate.sqlite3")
        self.ledger = DurableExecutionLedger(
            self.store, self.root / "observations.sqlite3"
        )

    def close(self) -> None:
        self.ledger.close()
        self.store.close()
        if self.owns_operator:
            self.operator.close()

    def _lease(
        self, target: ExecutionTargetBinding, purpose: CredentialPurpose
    ) -> CredentialLease:
        self.guard(target)
        if self.credentials is None:
            from quantforge.credentials_windows.composition import (
                build_credential_boundary,
            )

            self.credentials = cast(
                Callable[[], CredentialBoundary], build_credential_boundary
            )()
        return self.credentials.acquire(
            CredentialScope.from_target(
                target,
                provider="windows-credential-manager-v1",
                consumer="qf-platform-execution",
                purpose=purpose,
            ),
            self.reference,
            requested_at=self.clock(),
        )

    def _authority(self, target: ExecutionTargetBinding) -> None:
        self.guard(target)
        if target.environment == "PRODUCTION" or self.paused():
            raise PlatformError("REAL_EXPERIMENT_PAUSED_NO_DISPATCH_AUTHORITY")

    @staticmethod
    def _validate(
        observed: BrokerRiskObservation,
        envelope: BrokerSubmissionEnvelope,
        target: ExecutionTargetBinding,
        now: datetime,
    ) -> None:
        if (
            type(observed) is not BrokerRiskObservation
            or observed.target != target
            or observed.capability.target != target
            or observed.observed_at.tzinfo is None
            or not 0 <= (now - observed.observed_at).total_seconds() <= 30
            or any(
                not re.fullmatch(r"[a-f0-9]{64}", h)
                for h in (
                    observed.account_hash,
                    observed.price_hash,
                    observed.rule_hash,
                )
            )
            or observed.session_open is not True
            or observed.trading_permission is not True
            or type(observed.lot_size) is not int
            or observed.lot_size < 1
            or any(
                type(v) is not Decimal or not v.is_finite() or v <= 0
                for v in (
                    observed.tick_size,
                    observed.limit_price,
                    *observed.prices.values(),
                )
            )
            or any(
                type(v) is not Decimal or not v.is_finite() or v < 0
                for v in (observed.estimated_fee, observed.available_cash)
            )
            or envelope.order.symbol not in observed.prices
            or envelope.order.quantity % observed.lot_size != 0
            or observed.limit_price % observed.tick_size != 0
            or (
                envelope.order.side == "BUY"
                and (
                    envelope.order.quantity * observed.limit_price
                    + observed.estimated_fee
                    > observed.available_cash
                )
            )
        ):
            raise PlatformError("BROKER_AUTHENTICATED_RISK_INPUT_INVALID")

    @_platform_boundary
    def dispatch(
        self,
        envelope: BrokerSubmissionEnvelope,
        target: ExecutionTargetBinding,
        limits: RiskLimits,
    ) -> dict[str, str]:
        self._authority(target)
        # Serializes target observations/claims/send in this local host. Core's
        # atomic database claim remains the actual durable dispatch authority.
        with writer_lock(self.root):
            # Freeze an independent durable reducer result before observing the
            # broker. A callback returning that observation is not accepted.
            expected = self.accounting.snapshot(target)
            recovery = self.store.recover(target, observed_at=self.clock())
            try:
                existing, _ = self.store.read(envelope.submission_id, target)
            except ExecutionStateNotFoundError:
                existing = None
            if existing is not None:
                # PREPARED also stays locked after crash; a human must inspect.
                raise PlatformError("BROKER_PRIOR_ATTEMPT_NO_AUTOMATIC_REDISPATCH")
            lease = self._lease(target, CredentialPurpose.ACCOUNT_READ)
            try:
                try:
                    observed = self.gateway.observe(envelope, target, lease)
                except Exception:
                    raise PlatformError(
                        "BROKER_EXECUTION_OBSERVATION_UNAVAILABLE"
                    ) from None
            finally:
                lease.discard()
            self.guard(target)
            now = self.clock()
            self._validate(observed, envelope, target, now)
            expected_portfolio = expected.portfolio
            if (
                type(observed.portfolio) is not PortfolioState
                or expected_portfolio != observed.portfolio
            ):
                raise PlatformError("BROKER_ACCOUNT_RECONCILIATION_REQUIRED")
            with self.account_checks.transaction() as db:
                self.account_checks.append(
                    db,
                    kind="ACCOUNT_MATCH_BEFORE_PREPARE",
                    submission_id=envelope.submission_id,
                    target=target.model_dump(mode="json"),
                    expected_journal_commitment=expected.journal_commitment,
                    expected_portfolio_commitment=expected.portfolio_commitment,
                    anchor_evidence_hash=expected.anchor_evidence_hash,
                    observed_account_hash=observed.account_hash,
                    observed_price_hash=observed.price_hash,
                    observed_rule_hash=observed.rule_hash,
                    prices={k: str(v) for k, v in observed.prices.items()},
                    lot_size=observed.lot_size,
                    tick_size=str(observed.tick_size),
                    limit_price=str(observed.limit_price),
                    estimated_fee=str(observed.estimated_fee),
                    available_cash=str(observed.available_cash),
                    capability=observed.capability.model_dump(mode="json"),
                    derivation="CORE_CREATE_PORTFOLIO_AND_APPLY_FILL",
                    trust="STRUCTURAL_PORTFOLIO_EQUALITY_PLUGIN_ACCOUNT_HASH_SELF_ATTESTED",
                )
            prices = dict(observed.prices)
            # BUY risk uses the actual worst-case limit instead of a lower last
            # quote; per-account available cash is checked independently above.
            if envelope.order.side == "BUY":
                prices[envelope.order.symbol] = max(
                    observed.limit_price, prices[envelope.order.symbol]
                )
            provenance = evaluate_pre_dispatch_with_provenance(
                envelope,
                SubmissionHistoryState(
                    submission_id=envelope.submission_id, state="NEVER_DISPATCHED"
                ),
                AuthorizationFreshnessPolicy(
                    max_authorization_age=timedelta(minutes=5)
                ),
                now,
                observed.portfolio,
                prices,
                observed.estimated_fee,
                limits,
            )
            if not provenance.pre_dispatch_decision.eligible:
                raise PlatformError("CORE_FRESH_RISK_OR_AUTHORIZATION_DENIED")
            record, receipt = self.store.prepare(
                envelope, provenance, target, prepared_at=now
            )
            owner, token = "platform-execution", "claim-" + uuid.uuid4().hex
            claim = self.store.claim(
                envelope.submission_id,
                target,
                owner_id=owner,
                token=token,
                acquired_at=now,
                expires_at=now + timedelta(seconds=30),
            )
            # Recovery scans through the just-committed generation and preserves
            # the active claim. Never replace durable evidence with local flags.
            recovery = self.store.recover(target, observed_at=now)
            price = PriceFreshnessEvidence(
                schema="frontier-price-v1",
                evidence_id="price-" + observed.price_hash,
                market_source=target.broker,
                source_observation_id=observed.price_hash,
                provenance_id=provenance.provenance_id,
                price_set_commitment=provenance.price_set_commitment,
                risk_prices=provenance.risk_prices,
                observed_at=observed.observed_at,
                maximum_age=timedelta(seconds=30),
            )
            account = AccountReconciliationEvidence(
                schema="frontier-account-v1",
                evidence_id="account-" + observed.account_hash,
                target=target,
                observation_id=observed.account_hash,
                provenance_id=provenance.provenance_id,
                portfolio_commitment=provenance.portfolio_commitment,
                scope="FULL_PORTFOLIO_STATE",
                observed_portfolio=expected_portfolio,
                observed_at=observed.observed_at,
                maximum_age=timedelta(seconds=30),
                reconciliation_status="MATCHED",
                mismatch_details=(),
            )
            decision = evaluate_frontier_eligibility(
                provenance,
                now,
                target,
                price_evidence=price,
                account_evidence=account,
                durable_record=record,
                commit_receipt=receipt,
                recovery_evidence=recovery,
                operator_evidence=operator_evidence_for_frontier(self.operator, target),
                capability=observed.capability,
                claim=claim,
            )
            if not decision.eligible:
                raise PlatformError("CORE_FRONTIER_DENIED")
            lease = self._lease(target, CredentialPurpose.ORDER_SUBMIT)
            try:
                # Pre-mark revocation leaves PREPARED conservatively parked;
                # Core permits no-effect proof only after DISPATCHING.
                try:
                    self._authority(target)
                except PlatformError:
                    raise PlatformError("BROKER_PRE_MARK_AUTHORITY_REVOKED") from None
                self.store.mark_dispatching(decision, owner_id=owner, token=token)
                try:
                    # A kill/revocation after mark but before calling the SDK is
                    # proven no-effect. SDK failures are always outcome unknown.
                    self._authority(target)
                    live_now = self.clock()
                    self._validate(observed, envelope, target, live_now)
                    if self.accounting.snapshot(target) != expected:
                        raise PlatformError("HOST_ACCOUNTING_CHANGED_BEFORE_SEND")
                    current_operator = operator_evidence_for_frontier(
                        self.operator, target
                    )
                    if (
                        not 0 <= (live_now - now).total_seconds() <= 5
                        or current_operator != decision.operator_evidence
                        or current_operator is None
                        or not current_operator.enabled
                        or not 0
                        <= (live_now - current_operator.observed_at).total_seconds()
                        <= current_operator.maximum_age.total_seconds()
                        or claim.expires_at is None
                        or live_now >= claim.expires_at
                    ):
                        raise PlatformError("HOST_PRE_SEND_EVIDENCE_EXPIRED")
                except Exception as exc:
                    reason = (
                        str(exc)
                        if isinstance(exc, PlatformError)
                        and str(exc)
                        in {
                            "REAL_EXPERIMENT_PAUSED_NO_DISPATCH_AUTHORITY",
                            "HOST_PRE_SEND_EVIDENCE_EXPIRED",
                            "HOST_ACCOUNTING_CHANGED_BEFORE_SEND",
                            "BROKER_AUTHENTICATED_RISK_INPUT_INVALID",
                            "BROKER_EXECUTION_BINDING_TARGET_MISMATCH",
                        }
                        else "HOST_PRE_SEND_CHECK_UNAVAILABLE"
                    )
                    self.store.persist_pre_dispatch_no_effect(
                        envelope.submission_id,
                        target,
                        BrokerPreDispatchError(reason),
                        owner_id=owner,
                        token=token,
                        observed_at=self.clock(),
                    )
                    raise PlatformError(reason) from None
                try:
                    result = self.gateway.submit(envelope, observed, lease)
                    if (
                        type(result) is not BrokerSubmissionOutcome
                        or result.submission_id != envelope.submission_id
                    ):
                        raise ValueError
                    # Do not persist arbitrary vendor messages, account IDs or
                    # raw SDK references; the query path uses the client ID.
                    result = BrokerSubmissionOutcome(
                        submission_id=result.submission_id, status=result.status
                    )
                except Exception:  # uncertain effect; never retry or expose SDK error
                    result = BrokerSubmissionOutcome(
                        submission_id=envelope.submission_id, status="OUTCOME_UNKNOWN"
                    )
                saved = self.store.persist_transport_outcome(
                    result,
                    target,
                    owner_id=owner,
                    token=token,
                    observed_at=self.clock(),
                )
            finally:
                lease.discard()
            return {"submission_id": envelope.submission_id, "state": saved.state}

    @_platform_boundary
    def reconcile(
        self, submission_id: str, target: ExecutionTargetBinding
    ) -> dict[str, str]:
        # Kill closes submit/cancel; authenticated read-only reconciliation
        # remains available to resolve an already-unknown production outcome.
        with writer_lock(self.root):
            self.store.recover(target, observed_at=self.clock())
            record, _ = self.store.read(submission_id, target)
            lease = self._lease(target, CredentialPurpose.ORDER_QUERY)
            try:
                try:
                    observed = self.gateway.query_attempt(
                        submission_id,
                        target,
                        record.generation,
                        record.recovery_epoch,
                        lease,
                    )
                except Exception:
                    raise PlatformError("BROKER_EXECUTION_QUERY_UNAVAILABLE") from None
            finally:
                lease.discard()
            self.guard(target)
            if (
                type(observed) is not BrokerOrderObservation
                or observed.target != target
                or observed.submission_id != submission_id
                or observed.generation != record.generation
                or observed.recovery_epoch != record.recovery_epoch
                or observed.observed_at.tzinfo is None
                or not 0 <= (self.clock() - observed.observed_at).total_seconds() <= 30
                or observed.observed_at < record.updated_at
            ):
                raise PlatformError("BROKER_QUERY_TARGET_OR_FRESHNESS_INVALID")
            # No raw SDK messages enter normal evidence.
            observed = BrokerOrderObservation(
                submission_id=observed.submission_id,
                target=observed.target,
                generation=observed.generation,
                recovery_epoch=observed.recovery_epoch,
                status=observed.status,
                broker_reference=None,
                broker_message=None,
                observed_at=observed.observed_at,
            )
            # Core recording raises on CONFLICT. Assess without writing first
            # so contradictory broker evidence is visible and never committed.
            assessed = self.ledger.assess_observation(observed)
            if (
                assessed.result != ReconciliationResult.COMPATIBLE
                or observed.status == BrokerObservationStatus.UNKNOWN
            ):
                return {
                    "submission_id": submission_id,
                    "state": record.state,
                    "reconciliation": "UNKNOWN",
                    "classification": "BROKER_LOCAL_STATE_CONFLICT"
                    if assessed.result == ReconciliationResult.CONFLICT
                    else "BROKER_OBSERVATION_UNRESOLVED",
                }
            assessed = self.ledger.record_observation(observed)
            saved = record
            if record.state == "OUTCOME_UNKNOWN":
                saved = self.store.apply_reconciliation(
                    submission_id,
                    target,
                    status=cast(Literal["ACCEPTED", "REJECTED"], observed.status.value),
                    evidence_reference=assessed.assessment_id,
                    observed_at=observed.observed_at,
                )
            return {
                "submission_id": submission_id,
                "state": saved.state,
                "reconciliation": "VERIFIED_OBSERVATION",
            }

    @_platform_boundary
    def cancel(
        self, submission_id: str, target: ExecutionTargetBinding
    ) -> dict[str, str]:
        self._authority(target)
        # Cancel does not create terminal state or permission to resubmit.
        with writer_lock(self.root):
            self.store.recover(target, observed_at=self.clock())
            record, _ = self.store.read(submission_id, target)
            if record.state not in {"ACKNOWLEDGED", "OUTCOME_UNKNOWN"}:
                raise PlatformError("BROKER_CANCEL_NOT_ELIGIBLE")
            operator = operator_evidence_for_frontier(self.operator, target)
            if (
                operator is None
                or not operator.enabled
                or not 0
                <= (self.clock() - operator.observed_at).total_seconds()
                <= operator.maximum_age.total_seconds()
            ):
                raise PlatformError("CORE_OPERATOR_CANCEL_DENIED")
            lease = self._lease(target, CredentialPurpose.ORDER_CANCEL)
            try:
                self._authority(target)
                capability = self.gateway.cancel_capability(target, lease)
                if (
                    type(capability) is not BrokerCancelCapability
                    or capability.target != target
                    or capability.supported is not True
                    or capability.observed_at.tzinfo is None
                    or not 0
                    <= (self.clock() - capability.observed_at).total_seconds()
                    <= 30
                    or not re.fullmatch(r"[a-f0-9]{64}", capability.evidence_hash)
                ):
                    raise PlatformError("BROKER_CANCEL_CAPABILITY_UNKNOWN")
                self.gateway.cancel_order(submission_id, target, lease)
            except PlatformError:
                raise
            except Exception:
                raise PlatformError(
                    "BROKER_CANCEL_OUTCOME_UNKNOWN_QUERY_REQUIRED"
                ) from None
            finally:
                lease.discard()
            return {
                "submission_id": submission_id,
                "state": record.state,
                "cancel": "QUERY_REQUIRED",
            }
