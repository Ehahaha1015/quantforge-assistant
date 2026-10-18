from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _frontier_fixtures import (
    DISPATCH_AT,
    SUBMISSION_ID,
    complete_inputs,
    make_provenance,
    rebuild,
)

from quantforge.frontier import (
    AccountReconciliationEvidence,
    AtomicDispatchClaimEvidence,
    BrokerCapabilityDeclaration,
    DurableCommitReceipt,
    DurableSubmissionRecord,
    ExecutionTargetBinding,
    FrontierError,
    FrontierReason,
    OperatorExecutionEnableEvidence,
    PriceFreshnessEvidence,
    RecoveryCompleteEvidence,
    evaluate_frontier_eligibility,
)
from quantforge.portfolio import PortfolioState
from quantforge.provenance import RiskPriceFact


def test_complete_literal_fixture_is_eligible() -> None:
    values = complete_inputs()
    decision = evaluate_frontier_eligibility(**values)

    assert decision.eligible is True
    assert decision.reasons == ()
    assert decision.schema == "frontier-decision-v1"
    assert decision.submission_id == SUBMISSION_ID
    assert decision.provenance_id == values["provenance"].provenance_id
    assert decision.dispatch_timestamp == DISPATCH_AT


def test_all_missing_evidence_has_exact_canonical_reasons() -> None:
    values = complete_inputs()
    for name in (
        "price_evidence",
        "account_evidence",
        "durable_record",
        "commit_receipt",
        "recovery_evidence",
        "operator_evidence",
        "capability",
        "claim",
    ):
        values[name] = None

    decision = evaluate_frontier_eligibility(**values)

    assert decision.reasons == (
        FrontierReason.PRICE_PROVENANCE_MISMATCH,
        FrontierReason.ACCOUNT_PROVENANCE_MISMATCH,
        FrontierReason.DURABLE_RECORD_MISSING,
        FrontierReason.RECOVERY_INCOMPLETE,
        FrontierReason.CLAIM_UNAVAILABLE,
        FrontierReason.OPERATOR_EVIDENCE_MISSING,
        FrontierReason.CAPABILITY_INSUFFICIENT,
    )


def test_ineligible_fresh_pre_dispatch_provenance_denies_without_replay() -> None:
    values = complete_inputs()
    provenance = make_provenance(history_state="ACKNOWLEDGED")
    values["provenance"] = provenance
    values["price_evidence"] = rebuild(
        values["price_evidence"],
        provenance_id=provenance.provenance_id,
        price_set_commitment=provenance.price_set_commitment,
        risk_prices=provenance.risk_prices,
    )
    values["account_evidence"] = rebuild(
        values["account_evidence"],
        provenance_id=provenance.provenance_id,
        portfolio_commitment=provenance.portfolio_commitment,
        observed_portfolio=provenance.portfolio,
    )
    values["durable_record"] = rebuild(
        values["durable_record"], provenance_id=provenance.provenance_id
    )

    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.PRE_DISPATCH_INELIGIBLE,
    )


@pytest.mark.parametrize(
    ("field", "changes", "expected"),
    [
        (
            "price_evidence",
            {"observed_at": datetime(2030, 1, 1, 12, 1, 58, 999999, tzinfo=UTC)},
            (FrontierReason.STALE_PRICE_EVIDENCE,),
        ),
        (
            "price_evidence",
            {"observed_at": datetime(2030, 1, 1, 12, 2, 0, 1, tzinfo=UTC)},
            (FrontierReason.FUTURE_PRICE_EVIDENCE,),
        ),
        (
            "price_evidence",
            {
                "provenance_id": "riskprov1-" + "0" * 64,
                "price_set_commitment": "riskprice1-" + "0" * 64,
            },
            (FrontierReason.PRICE_PROVENANCE_MISMATCH,),
        ),
        (
            "account_evidence",
            {"observed_at": datetime(2030, 1, 1, 12, 1, 58, 999999, tzinfo=UTC)},
            (FrontierReason.STALE_ACCOUNT_EVIDENCE,),
        ),
        (
            "account_evidence",
            {"observed_at": datetime(2030, 1, 1, 12, 2, 0, 1, tzinfo=UTC)},
            (FrontierReason.FUTURE_ACCOUNT_EVIDENCE,),
        ),
        (
            "commit_receipt",
            {"committed": False},
            (FrontierReason.DURABLE_COMMIT_UNACKNOWLEDGED,),
        ),
        (
            "durable_record",
            {"state": "OUTCOME_UNKNOWN"},
            (FrontierReason.OUTCOME_UNKNOWN,),
        ),
        (
            "durable_record",
            {"state": "ACKNOWLEDGED"},
            (FrontierReason.FRONTIER_ALREADY_CROSSED,),
        ),
        (
            "recovery_evidence",
            {"status": "INCOMPLETE"},
            (FrontierReason.RECOVERY_INCOMPLETE,),
        ),
        (
            "claim",
            {"status": "CONFLICT"},
            (FrontierReason.CLAIM_UNAVAILABLE,),
        ),
        (
            "claim",
            {"expires_at": DISPATCH_AT},
            (FrontierReason.CLAIM_UNAVAILABLE,),
        ),
        (
            "operator_evidence",
            {"enabled": False},
            (FrontierReason.OPERATOR_DISABLED,),
        ),
        (
            "operator_evidence",
            {"observed_at": datetime(2030, 1, 1, 12, 1, 58, 999999, tzinfo=UTC)},
            (FrontierReason.OPERATOR_EVIDENCE_STALE,),
        ),
        (
            "capability",
            {"lookup_mode": "UNSUPPORTED"},
            (FrontierReason.CAPABILITY_INSUFFICIENT,),
        ),
    ],
)
def test_literal_single_denial_oracles(
    field: str, changes: dict[str, object], expected: tuple[FrontierReason, ...]
) -> None:
    values = complete_inputs()
    values[field] = rebuild(values[field], **changes)
    if field == "durable_record":
        record = values[field]
        values["commit_receipt"] = rebuild(
            values["commit_receipt"],
            record_id=record.record_id,
            submission_id=record.submission_id,
            target=record.target,
            generation=record.generation,
            recovery_epoch=record.recovery_epoch,
        )

    assert evaluate_frontier_eligibility(**values).reasons == expected


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("price_evidence", (FrontierReason.PRICE_PROVENANCE_MISMATCH,)),
        ("account_evidence", (FrontierReason.ACCOUNT_PROVENANCE_MISMATCH,)),
        (
            "commit_receipt",
            (FrontierReason.DURABLE_COMMIT_UNACKNOWLEDGED,),
        ),
        ("recovery_evidence", (FrontierReason.RECOVERY_INCOMPLETE,)),
        ("claim", (FrontierReason.CLAIM_UNAVAILABLE,)),
        (
            "operator_evidence",
            (FrontierReason.OPERATOR_EVIDENCE_MISSING,),
        ),
        ("capability", (FrontierReason.CAPABILITY_INSUFFICIENT,)),
    ],
)
def test_each_missing_evidence_denies(
    field: str, expected: tuple[FrontierReason, ...]
) -> None:
    values = complete_inputs()
    values[field] = None
    assert evaluate_frontier_eligibility(**values).reasons == expected


def test_missing_record_has_exact_three_reason_oracle() -> None:
    values = complete_inputs()
    for name in (
        "durable_record",
        "commit_receipt",
        "recovery_evidence",
        "claim",
    ):
        values[name] = None
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.DURABLE_RECORD_MISSING,
        FrontierReason.RECOVERY_INCOMPLETE,
        FrontierReason.CLAIM_UNAVAILABLE,
    )


def test_multi_reason_accumulates_in_declaration_order() -> None:
    values = complete_inputs()
    values["price_evidence"] = rebuild(
        values["price_evidence"],
        observed_at=DISPATCH_AT + timedelta(microseconds=1),
        provenance_id="riskprov1-" + "0" * 64,
    )
    values["operator_evidence"] = rebuild(
        values["operator_evidence"],
        enabled=False,
        observed_at=DISPATCH_AT + timedelta(microseconds=1),
    )
    values["capability"] = None
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.FUTURE_PRICE_EVIDENCE,
        FrontierReason.PRICE_PROVENANCE_MISMATCH,
        FrontierReason.OPERATOR_DISABLED,
        FrontierReason.OPERATOR_EVIDENCE_STALE,
        FrontierReason.CAPABILITY_INSUFFICIENT,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"price_set_commitment": "riskprice1-" + "0" * 64},
        {"provenance_id": "riskprov1-" + "0" * 64},
    ],
)
def test_price_identity_binding_oracles(mutation: dict[str, object]) -> None:
    values = complete_inputs()
    values["price_evidence"] = rebuild(values["price_evidence"], **mutation)
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.PRICE_PROVENANCE_MISMATCH,
    )


def test_price_fact_oracle_detects_changed_decimal() -> None:
    values = complete_inputs()
    price = values["price_evidence"]
    values["price_evidence"] = rebuild(
        price,
        risk_prices=(
            RiskPriceFact(symbol="AAPL", price=Decimal("100.0001")),
            price.risk_prices[1],
        ),
    )
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.PRICE_PROVENANCE_MISMATCH,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"portfolio_commitment": "riskportfolio1-" + "0" * 64},
        {"provenance_id": "riskprov1-" + "0" * 64},
        {
            "reconciliation_status": "MISMATCHED",
            "mismatch_details": ("cash differs",),
        },
    ],
)
def test_account_binding_oracles(mutation: dict[str, object]) -> None:
    values = complete_inputs()
    values["account_evidence"] = rebuild(values["account_evidence"], **mutation)
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.ACCOUNT_PROVENANCE_MISMATCH,
    )


def test_account_portfolio_value_oracle() -> None:
    values = complete_inputs()
    portfolio = values["account_evidence"].observed_portfolio
    fields = {name: getattr(portfolio, name) for name in type(portfolio).model_fields}
    fields["cash"] = Decimal(999)
    changed = PortfolioState(**fields)
    values["account_evidence"] = rebuild(
        values["account_evidence"], observed_portfolio=changed
    )
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.ACCOUNT_PROVENANCE_MISMATCH,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("broker", "broker-y"),
        ("account_reference", "acct-8"),
        ("environment", "PRODUCTION"),
    ],
)
def test_wrong_account_target_component_oracles(field: str, value: str) -> None:
    values = complete_inputs()
    target = rebuild(values["account_evidence"].target, **{field: value})
    values["account_evidence"] = rebuild(values["account_evidence"], target=target)
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH,
    )


def test_recovery_unresolved_current_submission_is_outcome_unknown() -> None:
    values = complete_inputs()
    values["recovery_evidence"] = rebuild(
        values["recovery_evidence"], unresolved_submission_ids=(SUBMISSION_ID,)
    )
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.OUTCOME_UNKNOWN,
    )


def test_claim_generation_mismatch_is_unavailable() -> None:
    values = complete_inputs()
    values["claim"] = rebuild(values["claim"], generation=8)
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.CLAIM_UNAVAILABLE,
    )


def test_internally_consistent_unknown_capability_is_insufficient() -> None:
    values = complete_inputs()
    values["capability"] = rebuild(
        values["capability"],
        client_identity_mode="UNKNOWN",
        uniqueness_scope="UNKNOWN",
        uniqueness_window=None,
        duplicate_behavior="UNKNOWN",
    )
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.CAPABILITY_INSUFFICIENT,
    )


def test_target_mismatch_is_one_environment_reason() -> None:
    values = complete_inputs()
    foreign = rebuild(values["target"], account_reference="acct-8")
    values["account_evidence"] = rebuild(values["account_evidence"], target=foreign)
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH,
    )


def test_foreign_evaluator_target_submission_is_environment_mismatch() -> None:
    values = complete_inputs()
    values["target"] = rebuild(values["target"], submission_id="broker1-" + "0" * 32)
    assert evaluate_frontier_eligibility(**values).reasons == (
        FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH,
    )


def test_dispatch_mismatch_and_timezone_equivalence() -> None:
    values = complete_inputs()
    values["dispatch_timestamp"] = datetime.fromisoformat("2030-01-01T20:02:00+08:00")
    assert evaluate_frontier_eligibility(**values).eligible

    values["dispatch_timestamp"] = DISPATCH_AT - timedelta(microseconds=1)
    decision = evaluate_frontier_eligibility(**values)
    assert FrontierReason.DISPATCH_TIMESTAMP_MISMATCH in decision.reasons


def test_receipt_cross_artifact_contradiction_is_error() -> None:
    values = complete_inputs()
    values["commit_receipt"] = rebuild(values["commit_receipt"], generation=8)
    with pytest.raises(FrontierError):
        evaluate_frontier_eligibility(**values)

    values = complete_inputs()
    values["durable_record"] = None
    with pytest.raises(FrontierError):
        evaluate_frontier_eligibility(**values)


def test_wrong_runtime_types_and_omitted_keyword_are_type_errors() -> None:
    values = complete_inputs()
    values["price_evidence"] = True
    with pytest.raises(TypeError):
        evaluate_frontier_eligibility(**values)

    values = complete_inputs()
    del values["claim"]
    with pytest.raises(TypeError):
        evaluate_frontier_eligibility(**values)


def test_all_evidence_models_are_exercised() -> None:
    values = complete_inputs()
    assert isinstance(values["target"], ExecutionTargetBinding)
    assert isinstance(values["price_evidence"], PriceFreshnessEvidence)
    assert isinstance(values["account_evidence"], AccountReconciliationEvidence)
    assert isinstance(values["durable_record"], DurableSubmissionRecord)
    assert isinstance(values["commit_receipt"], DurableCommitReceipt)
    assert isinstance(values["recovery_evidence"], RecoveryCompleteEvidence)
    assert isinstance(values["operator_evidence"], OperatorExecutionEnableEvidence)
    assert isinstance(values["capability"], BrokerCapabilityDeclaration)
    assert isinstance(values["claim"], AtomicDispatchClaimEvidence)
