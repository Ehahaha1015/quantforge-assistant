from __future__ import annotations

import json
import warnings
from datetime import timedelta
from decimal import (
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    ROUND_UP,
    Clamped,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Underflow,
    getcontext,
    localcontext,
)

import pytest
from _frontier_fixtures import complete_inputs, make_provenance, rebuild
from pydantic import BaseModel, ValidationError

from quantforge import frontier
from quantforge.frontier import (
    AccountReconciliationEvidence,
    BrokerCapabilityDeclaration,
    DurableSubmissionRecord,
    ExecutionTargetBinding,
    FrontierEligibilityDecision,
    FrontierError,
    PriceFreshnessEvidence,
    RecoveryCompleteEvidence,
    evaluate_frontier_eligibility,
)

PUBLIC_NAMES = {
    "AccountReconciliationEvidence",
    "AtomicDispatchClaimEvidence",
    "BrokerCapabilityDeclaration",
    "DurableCommitReceipt",
    "DurableSubmissionRecord",
    "ExecutionTargetBinding",
    "FrontierEligibilityDecision",
    "FrontierError",
    "FrontierReason",
    "OperatorExecutionEnableEvidence",
    "PriceFreshnessEvidence",
    "RecoveryCompleteEvidence",
    "evaluate_frontier_eligibility",
}


def test_exact_public_exports_and_model_contract() -> None:
    assert set(frontier.__all__) == PUBLIC_NAMES
    assert all(hasattr(frontier, name) for name in PUBLIC_NAMES)
    for model in (
        ExecutionTargetBinding,
        PriceFreshnessEvidence,
        AccountReconciliationEvidence,
        DurableSubmissionRecord,
        BrokerCapabilityDeclaration,
        RecoveryCompleteEvidence,
        FrontierEligibilityDecision,
    ):
        assert issubclass(model, BaseModel)
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["frozen"] is True
        assert model.model_config["strict"] is True


def test_every_complete_artifact_has_canonical_round_trip() -> None:
    values = complete_inputs()
    artifacts = [
        values["target"],
        values["price_evidence"],
        values["account_evidence"],
        values["durable_record"],
        values["commit_receipt"],
        values["recovery_evidence"],
        values["operator_evidence"],
        values["capability"],
        values["claim"],
        evaluate_frontier_eligibility(**values),
    ]
    for artifact in artifacts:
        payload = artifact.model_dump_json()
        assert payload == json.dumps(
            json.loads(payload),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        assert not payload.startswith("\ufeff")
        assert not payload.endswith("\n")
        assert type(artifact).from_json(payload) == artifact
        with pytest.raises(TypeError):
            artifact.model_dump_json(indent=2)


def test_from_json_rejects_noncanonical_duplicate_unknown_and_wrong_input() -> None:
    target = complete_inputs()["target"]
    canonical = target.model_dump_json()
    with pytest.raises(FrontierError):
        ExecutionTargetBinding.from_json(" " + canonical)
    with pytest.raises(FrontierError):
        ExecutionTargetBinding.from_json(
            canonical.replace("{", '{"schema":"frontier-target-v1",', 1)
        )
    data = json.loads(canonical)
    data["extra"] = "x"
    with pytest.raises(FrontierError):
        ExecutionTargetBinding.from_json(
            json.dumps(data, sort_keys=True, separators=(",", ":"))
        )
    with pytest.raises(FrontierError):
        ExecutionTargetBinding.from_json(b"not text")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("schema", "wrong"),
        ("broker", "contains space"),
        ("environment", "LIVE"),
        ("submission_id", "broker1-not-hex"),
    ],
)
def test_target_strict_validation(field: str, bad_value: object) -> None:
    target = complete_inputs()["target"]
    fields = {name: getattr(target, name) for name in type(target).model_fields}
    fields[field] = bad_value
    with pytest.raises(FrontierError):
        ExecutionTargetBinding(**fields)


def test_missing_field_naive_datetime_and_contradictory_binding_are_errors() -> None:
    values = complete_inputs()
    target = values["target"]
    fields = {name: getattr(target, name) for name in type(target).model_fields}
    del fields["broker"]
    with pytest.raises(FrontierError):
        ExecutionTargetBinding(**fields)
    with pytest.raises(FrontierError):
        rebuild(
            values["price_evidence"],
            observed_at=values["dispatch_timestamp"].replace(tzinfo=None),
        )
    foreign = rebuild(target, submission_id="broker1-" + "0" * 32)
    with pytest.raises(FrontierError):
        rebuild(values["durable_record"], target=foreign)


def test_model_inputs_require_tuples_real_models_and_positive_ages() -> None:
    values = complete_inputs()
    price = values["price_evidence"]
    with pytest.raises(FrontierError):
        rebuild(price, risk_prices=list(price.risk_prices))
    with pytest.raises(FrontierError):
        rebuild(price, risk_prices=tuple(reversed(price.risk_prices)))
    with pytest.raises(FrontierError):
        rebuild(price, risk_prices=(price.risk_prices[0], price.risk_prices[0]))
    with pytest.raises(FrontierError):
        rebuild(price, maximum_age=timedelta(0))
    with pytest.raises(FrontierError):
        rebuild(price, maximum_age=timedelta(microseconds=-1))
    with pytest.raises(FrontierError):
        rebuild(values["account_evidence"], target={})
    with pytest.raises(FrontierError):
        rebuild(values["durable_record"], generation=True)


def test_status_detail_and_capability_consistency() -> None:
    values = complete_inputs()
    with pytest.raises(FrontierError):
        rebuild(
            values["account_evidence"],
            reconciliation_status="MATCHED",
            mismatch_details=("cash differs",),
        )
    with pytest.raises(FrontierError):
        rebuild(
            values["capability"],
            client_identity_mode="LOCAL_ONLY",
            uniqueness_scope="ACCOUNT",
        )


def test_decision_direct_construction_replays_and_canonicalizes_reasons() -> None:
    values = complete_inputs()
    for field in (
        "price_evidence",
        "account_evidence",
        "durable_record",
        "commit_receipt",
        "recovery_evidence",
        "operator_evidence",
        "capability",
        "claim",
    ):
        values[field] = None
    decision = evaluate_frontier_eligibility(**values)
    fields = {name: getattr(decision, name) for name in type(decision).model_fields}
    fields["reasons"] = tuple(reversed(decision.reasons)) + decision.reasons
    assert FrontierEligibilityDecision(**fields).reasons == decision.reasons

    fields["eligible"] = True
    with pytest.raises(FrontierError):
        FrontierEligibilityDecision(**fields)


def test_coordinated_eligible_tamper_is_rejected_by_decision_replay() -> None:
    decision = evaluate_frontier_eligibility(**complete_inputs())
    data = json.loads(decision.model_dump_json())
    data["operator_evidence"]["enabled"] = False
    data["eligible"] = True
    data["reasons"] = []
    tampered = json.dumps(
        data, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    with pytest.raises(FrontierError):
        FrontierEligibilityDecision.from_json(tampered)


def test_frozen_models_reject_mutation_and_extra_fields() -> None:
    target = complete_inputs()["target"]
    with pytest.raises(ValidationError):
        target.broker = "other"  # type: ignore[misc]
    fields = {name: getattr(target, name) for name in type(target).model_fields}
    fields["extra"] = "forbidden"
    with pytest.raises(FrontierError):
        ExecutionTargetBinding(**fields)


def test_hostile_decimal_context_does_not_change_decision_or_json() -> None:
    eligible = complete_inputs()
    denied = complete_inputs()
    denied["operator_evidence"] = None
    baseline_context = _decimal_context_signature()
    baseline_eligible = evaluate_frontier_eligibility(**eligible)
    baseline_denied = evaluate_frontier_eligibility(**denied)
    assert _decimal_context_signature() == baseline_context

    trap_signals = (
        Clamped,
        DivisionByZero,
        Inexact,
        InvalidOperation,
        Overflow,
        Rounded,
        Underflow,
    )
    for rounding in (ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, ROUND_UP):
        for traps_enabled in (False, True):
            with localcontext() as context:
                context.prec = 1
                context.rounding = rounding
                context.Emax = 9
                context.Emin = -9
                if traps_enabled:
                    for signal in trap_signals:
                        context.traps[signal] = True
                hostile_eligible = evaluate_frontier_eligibility(**eligible)
                hostile_denied = evaluate_frontier_eligibility(**denied)
                assert hostile_eligible == baseline_eligible
                assert hostile_denied == baseline_denied
                assert (
                    hostile_eligible.model_dump_json()
                    == baseline_eligible.model_dump_json()
                )
                assert (
                    hostile_denied.model_dump_json()
                    == baseline_denied.model_dump_json()
                )
    assert _decimal_context_signature() == baseline_context


def _decimal_context_signature() -> tuple[object, ...]:
    context = getcontext()
    return (
        context.prec,
        context.rounding,
        context.Emax,
        context.Emin,
        context.capitals,
        context.clamp,
        tuple(
            sorted(
                (signal.__name__, enabled) for signal, enabled in context.traps.items()
            )
        ),
        tuple(
            sorted(
                (signal.__name__, enabled) for signal, enabled in context.flags.items()
            )
        ),
    )


def test_price_mapping_insertion_order_does_not_change_frontier_decision() -> None:
    first = make_provenance(prices={"MSFT": Decimal(50), "AAPL": Decimal(100)})
    second = make_provenance(prices={"AAPL": Decimal(100), "MSFT": Decimal(50)})
    assert first == second
    values = complete_inputs()
    values["provenance"] = first
    baseline = evaluate_frontier_eligibility(**values)
    values["provenance"] = second
    assert evaluate_frontier_eligibility(**values) == baseline


def test_schema_payload_field_does_not_warn_on_class_creation() -> None:
    """The persisted ``schema`` payload key must stay, so the package silences
    pydantic's shadowing warning instead of renaming a hash-verified field."""
    with warnings.catch_warnings(record=True) as caught:

        class _Shadowing(ExecutionTargetBinding):
            pass

    shadowing = [item for item in caught if "shadows an attribute" in str(item.message)]
    assert shadowing == []
