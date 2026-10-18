from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantforge.risk import RiskDecision, RiskError, RiskLimits, RiskReason


def limits(**overrides) -> RiskLimits:
    values = {
        "max_order_notional": Decimal(10000),
        "max_position_notional": Decimal(50000),
        "max_gross_exposure": Decimal(100000),
    }
    values.update(overrides)
    return RiskLimits(**values)


def test_reason_codes_are_exactly_the_contract_set() -> None:
    assert [reason.value for reason in RiskReason] == [
        "SELL_EXCEEDS_POSITION",
        "ORDER_NOTIONAL_LIMIT",
        "INSUFFICIENT_CASH",
        "POSITION_NOTIONAL_LIMIT",
        "GROSS_EXPOSURE_LIMIT",
    ]


def test_limits_require_three_positive_exact_decimals() -> None:
    assert limits().max_order_notional == Decimal(10000)
    assert RiskLimits(
        max_order_notional=1, max_position_notional="2", max_gross_exposure=Decimal(3)
    ).max_gross_exposure == Decimal(3)
    for field in ("max_order_notional", "max_position_notional", "max_gross_exposure"):
        for bad in (0, -1, Decimal("-0.01")):
            with pytest.raises(RiskError, match="greater than zero"):
                limits(**{field: bad})
        for bad in (1.5, True, Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with pytest.raises(RiskError):
                limits(**{field: bad})
    with pytest.raises(RiskError, match="risk limits"):
        RiskLimits(max_order_notional=Decimal(1), max_position_notional=Decimal(1))
    with pytest.raises(RiskError, match="risk limits"):
        limits(extra_limit=Decimal(1))


def test_limits_are_immutable_and_round_trip_deterministically() -> None:
    value = limits()
    with pytest.raises(ValidationError):
        value.max_order_notional = Decimal(1)  # type: ignore[misc]
    payload = value.model_dump_json()
    assert payload == value.model_dump_json()
    restored = RiskLimits.from_json(payload)
    assert restored == value
    assert restored.max_gross_exposure == Decimal(100000)
    with pytest.raises(RiskError, match="payload is invalid"):
        RiskLimits.from_json("{}")


def test_decision_approval_has_no_reasons() -> None:
    decision = RiskDecision(approved=True)
    assert decision.reasons == ()
    assert decision.detail == ""
    with pytest.raises(RiskError, match="must not carry rejection reasons"):
        RiskDecision(approved=True, reasons=(RiskReason.INSUFFICIENT_CASH,))


def test_decision_rejection_requires_reasons() -> None:
    with pytest.raises(RiskError, match="require at least one rejection reason"):
        RiskDecision(approved=False)
    decision = RiskDecision(approved=False, reasons=(RiskReason.INSUFFICIENT_CASH,))
    assert decision.approved is False
    assert decision.reasons == (RiskReason.INSUFFICIENT_CASH,)


def test_decision_reasons_are_canonical_and_deduplicated() -> None:
    decision = RiskDecision(
        approved=False,
        reasons=(
            RiskReason.GROSS_EXPOSURE_LIMIT,
            "INSUFFICIENT_CASH",
            RiskReason.GROSS_EXPOSURE_LIMIT,
            RiskReason.SELL_EXCEEDS_POSITION,
        ),
    )
    assert decision.reasons == (
        RiskReason.SELL_EXCEEDS_POSITION,
        RiskReason.INSUFFICIENT_CASH,
        RiskReason.GROSS_EXPOSURE_LIMIT,
    )
    assert RiskDecision(
        approved=False,
        reasons=(RiskReason.INSUFFICIENT_CASH, RiskReason.SELL_EXCEEDS_POSITION),
    ) == RiskDecision(
        approved=False,
        reasons=(RiskReason.SELL_EXCEEDS_POSITION, RiskReason.INSUFFICIENT_CASH),
    )


def test_decision_requires_strict_booleans_and_known_reasons() -> None:
    for bad in ("true", 1, 0, None):
        with pytest.raises(RiskError, match="risk decision is invalid"):
            RiskDecision(approved=bad)  # type: ignore[arg-type]
    with pytest.raises(RiskError, match="risk decision is invalid"):
        RiskDecision(approved=False, reasons=("NOT_A_REASON",))


def test_decision_round_trips_and_detail_is_secondary() -> None:
    decision = RiskDecision(
        approved=False,
        reasons=(RiskReason.ORDER_NOTIONAL_LIMIT,),
        detail="order notional 12000 exceeds limit 10000",
    )
    payload = decision.model_dump_json()
    assert payload == decision.model_dump_json()
    restored = RiskDecision.from_json(payload)
    assert restored == decision
    assert restored.reasons[0].value == "ORDER_NOTIONAL_LIMIT"
    assert restored.detail.startswith("order notional")
    with pytest.raises(RiskError, match="payload is invalid"):
        RiskDecision.from_json("{not json")


def test_values_layer_has_no_lower_layer_dependency() -> None:
    from pathlib import Path

    from quantforge.risk import values

    source = Path(values.__file__).read_text(encoding="utf-8")
    for forbidden in ("quantforge.domain", "quantforge.portfolio", "quantforge.data"):
        assert forbidden not in source
