from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from quantforge.broker import create_broker_submission_envelope
from quantforge.domain import OrderIntent, Position
from quantforge.frontier import (
    AccountReconciliationEvidence,
    AtomicDispatchClaimEvidence,
    BrokerCapabilityDeclaration,
    DurableCommitReceipt,
    DurableSubmissionRecord,
    ExecutionTargetBinding,
    OperatorExecutionEnableEvidence,
    PriceFreshnessEvidence,
    RecoveryCompleteEvidence,
)
from quantforge.live import authorize_live_order, create_live_authorization
from quantforge.portfolio import PortfolioState
from quantforge.predispatch import (
    AuthorizationFreshnessPolicy,
    SubmissionHistoryState,
)
from quantforge.provenance import (
    FreshRiskProvenance,
    evaluate_pre_dispatch_with_provenance,
)
from quantforge.risk import RiskDecision, RiskLimits

DISPATCH_AT = datetime(2030, 1, 1, 12, 2, tzinfo=UTC)
SUBMISSION_ID = "broker1-cd2ee7ab6550b22073eb4317083ff179"
PROVENANCE_ID = (
    "riskprov1-f3927418f0c4d7393183d6ad6a610c5ff4cecd668fe24788a36de64318e6547a"
)
PRICE_COMMITMENT = (
    "riskprice1-da724c15da8d5e9082483e864b18bbb145fa18aceba977c0013b00a6e47091ff"
)
PORTFOLIO_COMMITMENT = (
    "riskportfolio1-ed850f4206d03c37a265301735753c6736d5beb5ed3b97b1c08ea7bbd55a423c"
)


def make_provenance(
    *,
    history_state: str = "NEVER_DISPATCHED",
    prices: dict[str, Decimal] | None = None,
) -> FreshRiskProvenance:
    order = OrderIntent(
        strategy_id="alpha",
        symbol="AAPL",
        timestamp=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        side="BUY",
        quantity=Decimal("5.00"),
    )
    authorization = create_live_authorization(
        order, "operator-7", datetime(2030, 1, 1, 12, 1, tzinfo=UTC)
    )
    permission = authorize_live_order(order, RiskDecision(approved=True), authorization)
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
            submission_id=envelope.submission_id, state=history_state
        ),
        AuthorizationFreshnessPolicy(max_authorization_age=timedelta(minutes=5)),
        DISPATCH_AT,
        portfolio,
        (
            {"MSFT": Decimal("50.0"), "AAPL": Decimal("100.00")}
            if prices is None
            else prices
        ),
        Decimal("1.00"),
        RiskLimits(
            max_order_notional=Decimal("1000.00"),
            max_position_notional=Decimal("1000.0"),
            max_gross_exposure=Decimal("2000.000"),
        ),
    )


def complete_inputs() -> dict[str, Any]:
    provenance = make_provenance()
    assert provenance.pre_dispatch_decision.envelope.submission_id == SUBMISSION_ID
    assert provenance.provenance_id == PROVENANCE_ID
    assert provenance.price_set_commitment == PRICE_COMMITMENT
    assert provenance.portfolio_commitment == PORTFOLIO_COMMITMENT
    target = ExecutionTargetBinding(
        schema="frontier-target-v1",
        broker="broker-x",
        environment="SANDBOX",
        account_reference="acct-7",
        adapter_id="adapter-x",
        adapter_version="1",
        submission_id=SUBMISSION_ID,
    )
    record = DurableSubmissionRecord(
        schema="frontier-durable-record-v1",
        record_id="record-7",
        submission_id=SUBMISSION_ID,
        provenance_id=PROVENANCE_ID,
        target=target,
        generation=7,
        recovery_epoch=3,
        state="PREPARED",
        created_at=datetime(2030, 1, 1, 12, 1, 57, tzinfo=UTC),
        updated_at=datetime(2030, 1, 1, 12, 1, 58, tzinfo=UTC),
    )
    return {
        "provenance": provenance,
        "dispatch_timestamp": DISPATCH_AT,
        "target": target,
        "price_evidence": PriceFreshnessEvidence(
            schema="frontier-price-v1",
            evidence_id="price-evidence-7",
            market_source="market-source-x",
            source_observation_id="quote-77",
            provenance_id=PROVENANCE_ID,
            price_set_commitment=PRICE_COMMITMENT,
            risk_prices=provenance.risk_prices,
            observed_at=datetime(2030, 1, 1, 12, 1, 59, tzinfo=UTC),
            maximum_age=timedelta(seconds=1),
        ),
        "account_evidence": AccountReconciliationEvidence(
            schema="frontier-account-v1",
            evidence_id="account-evidence-7",
            target=target,
            observation_id="account-snapshot-77",
            provenance_id=PROVENANCE_ID,
            portfolio_commitment=PORTFOLIO_COMMITMENT,
            scope="FULL_PORTFOLIO_STATE",
            observed_portfolio=provenance.portfolio,
            observed_at=datetime(2030, 1, 1, 12, 1, 59, tzinfo=UTC),
            maximum_age=timedelta(seconds=1),
            reconciliation_status="MATCHED",
            mismatch_details=(),
        ),
        "durable_record": record,
        "commit_receipt": DurableCommitReceipt(
            schema="frontier-commit-receipt-v1",
            receipt_id="receipt-7",
            record_id="record-7",
            submission_id=SUBMISSION_ID,
            target=target,
            generation=7,
            recovery_epoch=3,
            committed=True,
            committed_at=datetime(2030, 1, 1, 12, 1, 58, 500000, tzinfo=UTC),
        ),
        "recovery_evidence": RecoveryCompleteEvidence(
            schema="frontier-recovery-v1",
            evidence_id="recovery-3",
            target=target,
            recovery_epoch=3,
            scanned_through_generation=7,
            status="COMPLETE",
            unresolved_submission_ids=(),
            observed_at=datetime(2030, 1, 1, 12, 1, 59, tzinfo=UTC),
        ),
        "operator_evidence": OperatorExecutionEnableEvidence(
            schema="frontier-operator-v1",
            evidence_id="operator-evidence-7",
            target=target,
            action_id="enable-action-7",
            enabled=True,
            observed_at=datetime(2030, 1, 1, 12, 1, 59, tzinfo=UTC),
            maximum_age=timedelta(seconds=1),
        ),
        "capability": BrokerCapabilityDeclaration(
            schema="frontier-capability-v1",
            declaration_id="capability-7",
            source_reference="broker-contract-7",
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
        ),
        "claim": AtomicDispatchClaimEvidence(
            schema="frontier-claim-v1",
            claim_id="claim-7",
            submission_id=SUBMISSION_ID,
            target=target,
            record_id="record-7",
            generation=7,
            recovery_epoch=3,
            owner_id="worker-7",
            token="token-7",
            status="ACQUIRED",
            acquired_at=datetime(2030, 1, 1, 12, 1, 59, tzinfo=UTC),
            expires_at=datetime(2030, 1, 1, 12, 2, 1, tzinfo=UTC),
        ),
    }


def rebuild(value: Any, **changes: Any) -> Any:
    fields = {name: getattr(value, name) for name in type(value).model_fields}
    fields.update(changes)
    return type(value)(**fields)
