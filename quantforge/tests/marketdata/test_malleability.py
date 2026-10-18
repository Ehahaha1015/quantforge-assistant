"""PRICE-001 revalidation and malleability oracles (AG, AH, section 25 G14)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from _marketdata_fixtures import (
    MAXIMUM_AGE,
    RecordingSource,
    forge_observation,
    make_artifact,
    make_observation,
    make_target,
    produce,
)

from quantforge.marketdata import (
    PriceObservationError,
    PriceObservationIdentityError,
    PriceProvenanceMismatchError,
    PriceSourceIdentityError,
    VerifiedPriceObservation,
)


def test_model_construct_with_mutated_maximum_age_fails_closed() -> None:
    provenance = make_artifact()
    observation = make_observation(provenance=provenance)
    forged = VerifiedPriceObservation.model_construct(
        schema=observation.schema,
        observation_id=observation.observation_id,
        market_source=observation.market_source,
        source_observation_id=observation.source_observation_id,
        observed_at=observation.observed_at,
        maximum_age=MAXIMUM_AGE + timedelta(seconds=1),
        risk_prices=provenance.risk_prices,
    )
    with pytest.raises(PriceObservationIdentityError):
        produce(forged, provenance=provenance)


def test_model_copy_update_fails_closed() -> None:
    provenance = make_artifact()
    observation = make_observation(provenance=provenance)
    copied = observation.model_copy(
        update={"maximum_age": MAXIMUM_AGE + timedelta(seconds=5)}
    )
    with pytest.raises(PriceObservationIdentityError):
        produce(copied, provenance=provenance)


def test_object_setattr_mutation_fails_closed() -> None:
    provenance = make_artifact()
    observation = make_observation(provenance=provenance)
    object.__setattr__(observation, "observed_at", observation.observed_at)
    object.__setattr__(observation, "source_observation_id", "obs-2")
    with pytest.raises(PriceObservationIdentityError):
        produce(observation, provenance=provenance)


def test_dict_reconstruction_of_tampered_payload_fails_closed() -> None:
    provenance = make_artifact()
    observation = make_observation(provenance=provenance)
    payload = observation.model_dump()
    payload["maximum_age"] = MAXIMUM_AGE + timedelta(seconds=9)
    with pytest.raises(PriceObservationError):
        VerifiedPriceObservation.model_validate(payload)


def test_json_reconstruction_of_tampered_payload_fails_closed() -> None:
    import json

    provenance = make_artifact()
    observation = make_observation(provenance=provenance)
    payload = observation.model_dump(mode="json")
    payload["source_observation_id"] = "obs-2"
    tampered = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    assert tampered != observation.model_dump_json()
    with pytest.raises(PriceObservationError):
        VerifiedPriceObservation.model_validate_json(tampered)


def test_subclass_instance_is_rejected_by_exact_type_check() -> None:
    class _SneakyObservation(VerifiedPriceObservation):
        pass

    observation = make_observation()
    sneaky = _SneakyObservation(
        schema=observation.schema,
        observation_id=observation.observation_id,
        market_source=observation.market_source,
        source_observation_id=observation.source_observation_id,
        observed_at=observation.observed_at,
        maximum_age=observation.maximum_age,
        risk_prices=observation.risk_prices,
    )
    with pytest.raises(PriceObservationError):
        produce(sneaky)


def test_duck_typed_lookalike_return_is_rejected() -> None:
    class _Lookalike:
        schema = "marketdata-observation-v1"
        observation_id = "marketdata-obs-v1-" + "0" * 64
        market_source = "marketdata-test-source"
        source_observation_id = "obs-1"
        observed_at = make_observation().observed_at
        maximum_age = MAXIMUM_AGE
        risk_prices = make_artifact().risk_prices

    with pytest.raises(PriceObservationError):
        produce(None, source=RecordingSource(_Lookalike()))


def test_provenance_substitution_with_valid_other_provenance_fails_closed() -> None:
    original = make_artifact()
    other = make_artifact(prices={"MSFT": "50.0", "AAPL": "101.00"})
    observation = make_observation(provenance=original)
    with pytest.raises(PriceProvenanceMismatchError):
        produce(observation, provenance=other)


def test_observation_bound_to_other_source_fails_closed() -> None:
    other = "marketdata-other-source"
    observation = make_observation(market_source=other)
    with pytest.raises(PriceSourceIdentityError):
        produce(observation)


def test_forged_observation_schema_fails_closed() -> None:
    observation = forge_observation(
        risk_prices=make_artifact().risk_prices,
        schema="marketdata-observation-v2",
    )
    with pytest.raises(PriceObservationError):
        produce(observation)


def test_revalidation_does_not_trust_model_construction() -> None:
    """A constructed observation still fails when its facts were tampered."""
    provenance = make_artifact()
    observation = make_observation(provenance=provenance)
    forged = forge_observation(
        risk_prices=observation.risk_prices,
        observed_at=provenance.pre_dispatch_decision.evaluation_timestamp
        - MAXIMUM_AGE
        - timedelta(microseconds=1),
    )
    from quantforge.marketdata import PriceStaleObservationError

    with pytest.raises(PriceStaleObservationError):
        produce(forged, provenance=provenance, target=make_target())
