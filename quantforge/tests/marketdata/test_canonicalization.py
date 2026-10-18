"""Canonicalization cross-checks (contract sections 9.2, 11 and 25 G5)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext

import pytest
from _marketdata_fixtures import EVALUATED_AT, make_artifact

from quantforge.marketdata.canonical import (
    _canonical_decimal as package_projection,
)
from quantforge.marketdata.canonical import _observation_id
from quantforge.provenance import RiskPriceFact

# Section 21 explicitly permits this test-only private import so the local
# projection can be cross-checked byte-for-byte against the frozen rule.
from quantforge.provenance.risk import _canonical_decimal as frozen_projection
from quantforge.provenance.risk import _price_commitment

_VALUES = [
    Decimal(1),
    Decimal("1.0"),
    Decimal("1.00"),
    Decimal("1E+0"),
    Decimal("0.1"),
    Decimal("1E-8"),
    Decimal("123456789012345678901234567890.000"),
    Decimal(100),
    Decimal("1.2300E+5"),
    Decimal(0),
    Decimal("-0"),
    Decimal(-1),
]


@pytest.mark.parametrize("value", _VALUES)
def test_decimal_projection_matches_the_frozen_provenance_rule(
    value: Decimal,
) -> None:
    assert package_projection(value) == frozen_projection(value)


@pytest.mark.parametrize(
    ("precision", "emax", "emin"),
    [(1, 1, -1), (2, 2, -2), (5, 9, -9), (28, 999999, -999999)],
)
def test_decimal_projection_is_context_independent(
    precision: int, emax: int, emin: int
) -> None:
    expected = {value: frozen_projection(value) for value in _VALUES}
    with localcontext() as context:
        context.prec = precision
        context.Emax = emax
        context.Emin = emin
        context.traps[InvalidOperation] = True
        observed = {value: package_projection(value) for value in _VALUES}
    assert observed == expected


def test_equivalent_decimal_spellings_share_identity_and_commitment() -> None:
    one_scale_one = RiskPriceFact(symbol="X", price=Decimal("1.0"))
    one_scale_two = RiskPriceFact(symbol="X", price=Decimal("1.00"))
    integer_one = RiskPriceFact(symbol="X", price=Decimal(1))
    assert _price_commitment((one_scale_one,)) == _price_commitment((one_scale_two,))
    assert _price_commitment((one_scale_one,)) == _price_commitment((integer_one,))

    provenance = make_artifact()
    observed_at = provenance.pre_dispatch_decision.evaluation_timestamp
    identities = {
        _observation_id(
            market_source="marketdata-test-source",
            source_observation_id="obs-1",
            observed_at=observed_at,
            maximum_age=timedelta(seconds=30),
            risk_prices=(fact,),
        )
        for fact in (one_scale_one, one_scale_two, integer_one)
    }
    assert len(identities) == 1


def test_producer_accepts_equivalent_decimal_spellings() -> None:
    provenance = make_artifact()
    rescaled = tuple(
        RiskPriceFact(symbol=fact.symbol, price=fact.price.quantize(Decimal("0.01")))
        for fact in provenance.risk_prices
    )
    from _marketdata_fixtures import make_observation, produce

    observation = make_observation(provenance=provenance, risk_prices=rescaled)
    evidence = produce(observation, provenance=provenance)
    assert evidence.risk_prices == provenance.risk_prices
    assert evidence.provenance_id == provenance.provenance_id
    assert EVALUATED_AT == provenance.pre_dispatch_decision.evaluation_timestamp
