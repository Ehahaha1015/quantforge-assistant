"""Pure deterministic evaluation of explicit frontier evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..provenance import FreshRiskProvenance
from .models import (
    AccountReconciliationEvidence,
    AtomicDispatchClaimEvidence,
    BrokerCapabilityDeclaration,
    DurableCommitReceipt,
    DurableSubmissionRecord,
    ExecutionTargetBinding,
    FrontierEligibilityDecision,
    FrontierError,
    FrontierReason,
    OperatorExecutionEnableEvidence,
    PriceFreshnessEvidence,
    RecoveryCompleteEvidence,
    _utc,
)


def _target_mismatch(
    target: ExecutionTargetBinding, artifact_target: ExecutionTargetBinding
) -> bool:
    return artifact_target != target


def _capability_is_sufficient(capability: BrokerCapabilityDeclaration) -> bool:
    return (
        capability.client_identity_mode in ("BROKER_ENFORCED", "LOCAL_ONLY")
        and capability.lookup_mode == "BY_CLIENT_ID"
        and capability.reconciliation_mode == "SUPPORTED"
        and capability.timeout_semantics == "OUTCOME_UNKNOWN"
        and capability.frontier_classification == "CONSERVATIVE"
        and capability.acknowledgement_semantics == "ACK_NOT_FILL"
    )


def _receipt_matches_record(
    receipt: DurableCommitReceipt, record: DurableSubmissionRecord
) -> bool:
    return (
        receipt.record_id == record.record_id
        and receipt.submission_id == record.submission_id
        and receipt.target == record.target
        and receipt.generation == record.generation
        and receipt.recovery_epoch == record.recovery_epoch
    )


def _expected_reasons(
    provenance: FreshRiskProvenance,
    dispatch_timestamp: datetime,
    target: ExecutionTargetBinding,
    *,
    price_evidence: PriceFreshnessEvidence | None,
    account_evidence: AccountReconciliationEvidence | None,
    durable_record: DurableSubmissionRecord | None,
    commit_receipt: DurableCommitReceipt | None,
    recovery_evidence: RecoveryCompleteEvidence | None,
    operator_evidence: OperatorExecutionEnableEvidence | None,
    capability: BrokerCapabilityDeclaration | None,
    claim: AtomicDispatchClaimEvidence | None,
) -> tuple[FrontierReason, ...]:
    """Validate cross-artifact invariants and compute every denial reason."""
    moment = _utc(dispatch_timestamp, name="dispatch_timestamp")
    if commit_receipt is not None and durable_record is None:
        raise FrontierError("commit receipt cannot exist without a durable record")
    if (
        commit_receipt is not None
        and durable_record is not None
        and not _receipt_matches_record(commit_receipt, durable_record)
    ):
        raise FrontierError("commit receipt contradicts the durable record")

    reasons: set[FrontierReason] = set()
    decision = provenance.pre_dispatch_decision
    submission_id = decision.envelope.submission_id

    if decision.eligible is not True:
        reasons.add(FrontierReason.PRE_DISPATCH_INELIGIBLE)
    if moment != decision.evaluation_timestamp:
        reasons.add(FrontierReason.DISPATCH_TIMESTAMP_MISMATCH)
    if target.submission_id != submission_id:
        reasons.add(FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH)

    if price_evidence is None:
        reasons.add(FrontierReason.PRICE_PROVENANCE_MISMATCH)
    else:
        if price_evidence.observed_at > moment:
            reasons.add(FrontierReason.FUTURE_PRICE_EVIDENCE)
        elif moment - price_evidence.observed_at > price_evidence.maximum_age:
            reasons.add(FrontierReason.STALE_PRICE_EVIDENCE)
        if (
            price_evidence.provenance_id != provenance.provenance_id
            or price_evidence.price_set_commitment != provenance.price_set_commitment
            or price_evidence.risk_prices != provenance.risk_prices
        ):
            reasons.add(FrontierReason.PRICE_PROVENANCE_MISMATCH)

    if account_evidence is None:
        reasons.add(FrontierReason.ACCOUNT_PROVENANCE_MISMATCH)
    else:
        if account_evidence.observed_at > moment:
            reasons.add(FrontierReason.FUTURE_ACCOUNT_EVIDENCE)
        elif moment - account_evidence.observed_at > account_evidence.maximum_age:
            reasons.add(FrontierReason.STALE_ACCOUNT_EVIDENCE)
        if _target_mismatch(target, account_evidence.target):
            reasons.add(FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH)
        if (
            account_evidence.provenance_id != provenance.provenance_id
            or account_evidence.portfolio_commitment != provenance.portfolio_commitment
            or account_evidence.observed_portfolio != provenance.portfolio
            or account_evidence.reconciliation_status != "MATCHED"
            or account_evidence.mismatch_details != ()
        ):
            reasons.add(FrontierReason.ACCOUNT_PROVENANCE_MISMATCH)

    if durable_record is None:
        reasons.add(FrontierReason.DURABLE_RECORD_MISSING)
    else:
        if (
            durable_record.submission_id != submission_id
            or durable_record.provenance_id != provenance.provenance_id
        ):
            reasons.add(FrontierReason.DURABLE_RECORD_MISSING)
        if _target_mismatch(target, durable_record.target):
            reasons.add(FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH)
        if durable_record.created_at > moment or durable_record.updated_at > moment:
            reasons.add(FrontierReason.DURABLE_COMMIT_UNACKNOWLEDGED)
        if commit_receipt is None:
            reasons.add(FrontierReason.DURABLE_COMMIT_UNACKNOWLEDGED)
        else:
            if _target_mismatch(target, commit_receipt.target):
                reasons.add(FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH)
            if (
                commit_receipt.committed is not True
                or commit_receipt.committed_at < durable_record.updated_at
                or commit_receipt.committed_at > moment
            ):
                reasons.add(FrontierReason.DURABLE_COMMIT_UNACKNOWLEDGED)
        if durable_record.state in ("DISPATCHING", "OUTCOME_UNKNOWN"):
            reasons.add(FrontierReason.OUTCOME_UNKNOWN)
        elif durable_record.state in (
            "ACKNOWLEDGED",
            "REJECTED",
            "NO_EFFECT_CONFIRMED",
        ):
            reasons.add(FrontierReason.FRONTIER_ALREADY_CROSSED)

    if recovery_evidence is None:
        reasons.add(FrontierReason.RECOVERY_INCOMPLETE)
    else:
        recovery_current = durable_record is not None
        if _target_mismatch(target, recovery_evidence.target):
            reasons.add(FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH)
        if durable_record is not None and (
            recovery_evidence.recovery_epoch != durable_record.recovery_epoch
            or recovery_evidence.scanned_through_generation < durable_record.generation
        ):
            recovery_current = False
        if (
            recovery_evidence.status != "COMPLETE"
            or recovery_evidence.observed_at > moment
        ):
            recovery_current = False
        if not recovery_current:
            reasons.add(FrontierReason.RECOVERY_INCOMPLETE)
        if submission_id in recovery_evidence.unresolved_submission_ids:
            reasons.add(FrontierReason.OUTCOME_UNKNOWN)

    if claim is None:
        reasons.add(FrontierReason.CLAIM_UNAVAILABLE)
    else:
        claim_available = durable_record is not None
        if _target_mismatch(target, claim.target):
            reasons.add(FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH)
        if durable_record is not None and (
            claim.submission_id != durable_record.submission_id
            or claim.record_id != durable_record.record_id
            or claim.generation != durable_record.generation
            or claim.recovery_epoch != durable_record.recovery_epoch
        ):
            claim_available = False
        if (
            claim.status != "ACQUIRED"
            or claim.acquired_at > moment
            or (claim.expires_at is not None and moment >= claim.expires_at)
        ):
            claim_available = False
        if not claim_available:
            reasons.add(FrontierReason.CLAIM_UNAVAILABLE)

    if operator_evidence is None:
        reasons.add(FrontierReason.OPERATOR_EVIDENCE_MISSING)
    else:
        if _target_mismatch(target, operator_evidence.target):
            reasons.add(FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH)
        if operator_evidence.enabled is not True:
            reasons.add(FrontierReason.OPERATOR_DISABLED)
        if (
            operator_evidence.observed_at > moment
            or moment - operator_evidence.observed_at > operator_evidence.maximum_age
        ):
            reasons.add(FrontierReason.OPERATOR_EVIDENCE_STALE)

    if capability is None:
        reasons.add(FrontierReason.CAPABILITY_INSUFFICIENT)
    else:
        if _target_mismatch(target, capability.target):
            reasons.add(FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH)
        if not _capability_is_sufficient(capability):
            reasons.add(FrontierReason.CAPABILITY_INSUFFICIENT)

    return tuple(reason for reason in FrontierReason if reason in reasons)


def _check_optional_type(value: Any, expected: type[Any], *, name: str) -> None:
    if value is not None and not isinstance(value, expected):
        raise TypeError(f"{name} must be {expected.__name__} or None")


def evaluate_frontier_eligibility(
    provenance: FreshRiskProvenance,
    dispatch_timestamp: datetime,
    target: ExecutionTargetBinding,
    *,
    price_evidence: PriceFreshnessEvidence | None,
    account_evidence: AccountReconciliationEvidence | None,
    durable_record: DurableSubmissionRecord | None,
    commit_receipt: DurableCommitReceipt | None,
    recovery_evidence: RecoveryCompleteEvidence | None,
    operator_evidence: OperatorExecutionEnableEvidence | None,
    capability: BrokerCapabilityDeclaration | None,
    claim: AtomicDispatchClaimEvidence | None,
) -> FrontierEligibilityDecision:
    """Evaluate explicit evidence without crossing or approaching the frontier."""
    if not isinstance(provenance, FreshRiskProvenance):
        raise TypeError("provenance must be a FreshRiskProvenance")
    if not isinstance(dispatch_timestamp, datetime):
        raise TypeError("dispatch_timestamp must be a datetime")
    if not isinstance(target, ExecutionTargetBinding):
        raise TypeError("target must be an ExecutionTargetBinding")
    _check_optional_type(price_evidence, PriceFreshnessEvidence, name="price_evidence")
    _check_optional_type(
        account_evidence,
        AccountReconciliationEvidence,
        name="account_evidence",
    )
    _check_optional_type(durable_record, DurableSubmissionRecord, name="durable_record")
    _check_optional_type(commit_receipt, DurableCommitReceipt, name="commit_receipt")
    _check_optional_type(
        recovery_evidence,
        RecoveryCompleteEvidence,
        name="recovery_evidence",
    )
    _check_optional_type(
        operator_evidence,
        OperatorExecutionEnableEvidence,
        name="operator_evidence",
    )
    _check_optional_type(capability, BrokerCapabilityDeclaration, name="capability")
    _check_optional_type(claim, AtomicDispatchClaimEvidence, name="claim")

    moment = _utc(dispatch_timestamp, name="dispatch_timestamp")
    reasons = _expected_reasons(
        provenance,
        moment,
        target,
        price_evidence=price_evidence,
        account_evidence=account_evidence,
        durable_record=durable_record,
        commit_receipt=commit_receipt,
        recovery_evidence=recovery_evidence,
        operator_evidence=operator_evidence,
        capability=capability,
        claim=claim,
    )
    return FrontierEligibilityDecision(
        schema="frontier-decision-v1",
        submission_id=provenance.pre_dispatch_decision.envelope.submission_id,
        provenance_id=provenance.provenance_id,
        provenance=provenance,
        dispatch_timestamp=moment,
        target=target,
        price_evidence=price_evidence,
        account_evidence=account_evidence,
        durable_record=durable_record,
        commit_receipt=commit_receipt,
        recovery_evidence=recovery_evidence,
        operator_evidence=operator_evidence,
        capability=capability,
        claim=claim,
        eligible=reasons == (),
        reasons=reasons,
    )
