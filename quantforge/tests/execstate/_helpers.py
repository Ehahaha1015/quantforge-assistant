from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantforge.broker import create_broker_submission_envelope
from quantforge.domain import OrderIntent, Position
from quantforge.execstate import SQLiteExecutionStateStore
from quantforge.frontier import (
    AccountReconciliationEvidence,
    BrokerCapabilityDeclaration,
    ExecutionTargetBinding,
    OperatorExecutionEnableEvidence,
    PriceFreshnessEvidence,
    evaluate_frontier_eligibility,
)
from quantforge.live import authorize_live_order, create_live_authorization
from quantforge.portfolio import PortfolioState
from quantforge.predispatch import (
    AuthorizationFreshnessPolicy,
    SubmissionHistoryState,
)
from quantforge.provenance import evaluate_pre_dispatch_with_provenance
from quantforge.risk import RiskDecision, RiskLimits

ORDER_AT = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
AUTHORIZED_AT = datetime(2030, 1, 1, 12, 1, tzinfo=UTC)
RECOVER_AT = datetime(2030, 1, 1, 12, 1, 50, tzinfo=UTC)
PREPARE_AT = datetime(2030, 1, 1, 12, 1, 55, tzinfo=UTC)
CLAIM_AT = datetime(2030, 1, 1, 12, 1, 56, tzinfo=UTC)
RECOVER_RESCAN_AT = datetime(2030, 1, 1, 12, 1, 57, tzinfo=UTC)
EVIDENCE_AT = datetime(2030, 1, 1, 12, 1, 59, tzinfo=UTC)
DISPATCH_AT = datetime(2030, 1, 1, 12, 2, tzinfo=UTC)
OUTCOME_AT = datetime(2030, 1, 1, 12, 2, 1, tzinfo=UTC)
CLAIM_EXPIRES_AT = datetime(2030, 1, 1, 12, 3, tzinfo=UTC)


def make_provenance(*, limits: RiskLimits | None = None) -> Any:
    order = OrderIntent(
        strategy_id="alpha",
        symbol="AAPL",
        timestamp=ORDER_AT,
        side="BUY",
        quantity=Decimal("5.00"),
    )
    authorization = create_live_authorization(
        order, "operator-7", AUTHORIZED_AT
    )
    permission = authorize_live_order(
        order, RiskDecision(approved=True), authorization
    )
    envelope = create_broker_submission_envelope(order, permission)
    portfolio = PortfolioState(
        initial_cash=Decimal("1000.00"),
        cash=Decimal("1000.00"),
        positions=(
            Position(
                symbol="MSFT",
                quantity=Decimal("2.0"),
                average_price=Decimal("50.00"),
            ),
        ),
        cumulative_fees=Decimal("0.00"),
        last_fill_timestamp=datetime(2030, 1, 1, 11, 59, tzinfo=UTC),
    )
    return evaluate_pre_dispatch_with_provenance(
        envelope,
        SubmissionHistoryState(
            submission_id=envelope.submission_id,
            state="NEVER_DISPATCHED",
        ),
        AuthorizationFreshnessPolicy(max_authorization_age=timedelta(minutes=5)),
        DISPATCH_AT,
        portfolio,
        {"MSFT": Decimal("50.0"), "AAPL": Decimal("100.00")},
        Decimal("1.00"),
        limits
        if limits is not None
        else RiskLimits(
            max_order_notional=Decimal("1000.00"),
            max_position_notional=Decimal("1000.0"),
            max_gross_exposure=Decimal("2000.000"),
        ),
    )


def make_target(submission_id: str, *, account_reference: str = "acct-7") -> ExecutionTargetBinding:
    return ExecutionTargetBinding(
        schema="frontier-target-v1",
        broker="broker-x",
        environment="SANDBOX",
        account_reference=account_reference,
        adapter_id="adapter-x",
        adapter_version="1",
        submission_id=submission_id,
    )


def make_store(tmp_path: Path) -> SQLiteExecutionStateStore:
    return SQLiteExecutionStateStore(tmp_path / "execstate.sqlite3")


def recover_store(
    store: SQLiteExecutionStateStore,
    target: ExecutionTargetBinding,
    *,
    observed_at: datetime = RECOVER_AT,
) -> Any:
    return store.recover(target, observed_at=observed_at)


def prepare_store(
    store: SQLiteExecutionStateStore,
    provenance: Any,
    target: ExecutionTargetBinding,
    *,
    prepared_at: datetime = PREPARE_AT,
) -> Any:
    return store.prepare(
        provenance.pre_dispatch_decision.envelope,
        provenance,
        target,
        prepared_at=prepared_at,
    )


def claim_store(
    store: SQLiteExecutionStateStore,
    submission_id: str,
    target: ExecutionTargetBinding,
    *,
    owner_id: str = "worker-1",
    token: str = "token-1",
    acquired_at: datetime = CLAIM_AT,
    expires_at: datetime = CLAIM_EXPIRES_AT,
) -> Any:
    return store.claim(
        submission_id,
        target,
        owner_id=owner_id,
        token=token,
        acquired_at=acquired_at,
        expires_at=expires_at,
    )


def build_eligibility(
    store: SQLiteExecutionStateStore,
    provenance: Any,
    target: ExecutionTargetBinding,
    record: Any,
    receipt: Any,
    recovery_evidence: Any,
    claim: Any,
    *,
    dispatch_at: datetime = DISPATCH_AT,
    price_at: datetime = EVIDENCE_AT,
    account_at: datetime = EVIDENCE_AT,
    operator_at: datetime = EVIDENCE_AT,
) -> Any:
    price = PriceFreshnessEvidence(
        schema="frontier-price-v1",
        evidence_id="price-evidence-1",
        market_source="market-source-x",
        source_observation_id="quote-1",
        provenance_id=provenance.provenance_id,
        price_set_commitment=provenance.price_set_commitment,
        risk_prices=provenance.risk_prices,
        observed_at=price_at,
        maximum_age=timedelta(seconds=2),
    )
    account = AccountReconciliationEvidence(
        schema="frontier-account-v1",
        evidence_id="account-evidence-1",
        target=target,
        observation_id="account-snapshot-1",
        provenance_id=provenance.provenance_id,
        portfolio_commitment=provenance.portfolio_commitment,
        scope="FULL_PORTFOLIO_STATE",
        observed_portfolio=provenance.portfolio,
        observed_at=account_at,
        maximum_age=timedelta(seconds=2),
        reconciliation_status="MATCHED",
        mismatch_details=(),
    )
    operator = OperatorExecutionEnableEvidence(
        schema="frontier-operator-v1",
        evidence_id="operator-evidence-1",
        target=target,
        action_id="enable-action-1",
        enabled=True,
        observed_at=operator_at,
        maximum_age=timedelta(seconds=2),
    )
    capability = BrokerCapabilityDeclaration(
        schema="frontier-capability-v1",
        declaration_id="capability-1",
        source_reference="broker-contract-1",
        target=target,
        client_identity_mode="BROKER_ENFORCED",
        uniqueness_scope="ACCOUNT",
        uniqueness_window=timedelta(days=1),
        duplicate_behavior="RETURN_EXISTING",
        lookup_mode="BY_CLIENT_ID",
        reconciliation_mode="SUPPORTED",
        timeout_semantics="OUTCOME_UNKNOWN",
        frontier_classification="CONSERVATIVE",
        acknowledgement_semantics="ACK_NOT_FILL",
    )
    return evaluate_frontier_eligibility(
        provenance,
        dispatch_at,
        target,
        price_evidence=price,
        account_evidence=account,
        durable_record=record,
        commit_receipt=receipt,
        recovery_evidence=recovery_evidence,
        operator_evidence=operator,
        capability=capability,
        claim=claim,
    )


def prepare_claim_recover(
    tmp_path: Path,
) -> tuple[
    SQLiteExecutionStateStore,
    Any,
    ExecutionTargetBinding,
    Any,
    Any,
    Any,
    Any,
    Any,
]:
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(tmp_path)
    recover_store(store, target)
    record, receipt = prepare_store(store, provenance, target)
    claim = claim_store(store, envelope.submission_id, target)
    recovery = recover_store(store, target, observed_at=RECOVER_RESCAN_AT)
    return (
        store,
        provenance,
        target,
        record,
        receipt,
        recovery,
        claim,
        envelope,
    )
