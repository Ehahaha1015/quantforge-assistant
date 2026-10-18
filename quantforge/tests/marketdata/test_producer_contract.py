"""PRICE-001 producer oracles A..U (frozen contract sections 12-18 and 26)."""

from __future__ import annotations

from datetime import UTC, timedelta, timezone
from decimal import Decimal

import pytest
from _marketdata_fixtures import (
    FRESH_AT,
    MAXIMUM_AGE,
    TEST_OBSERVATION_ID,
    TEST_SOURCE_ID,
    RecordingSource,
    forge_observation,
    make_artifact,
    make_observation,
    make_target,
    produce,
)

from quantforge.frontier import FrontierReason, evaluate_frontier_eligibility
from quantforge.marketdata import (
    PriceArgumentError,
    PriceCoverageError,
    PriceFactError,
    PriceFutureObservationError,
    PriceObservationError,
    PriceObservationIdentityError,
    PriceObservationRequest,
    PriceProvenanceMismatchError,
    PriceSourceError,
    PriceSourceIdentityError,
    PriceSourceRejectedError,
    PriceStaleObservationError,
    PriceTargetMismatchError,
    PriceTimestampError,
    RejectingPriceObservationSource,
    VerifiedPriceObservation,
)
from quantforge.marketdata.canonical import _observation_id
from quantforge.provenance import FreshRiskProvenance, RiskPriceFact


def _fact(symbol: str, price: Decimal) -> RiskPriceFact:
    return RiskPriceFact(symbol=symbol, price=price)


def _provenance_prices() -> tuple[RiskPriceFact, ...]:
    return make_artifact().risk_prices


def test_oracle_a_rejecting_default_never_produces_evidence() -> None:
    from quantforge.marketdata import produce_price_freshness_evidence

    with pytest.raises(PriceSourceRejectedError):
        produce_price_freshness_evidence(
            provenance=make_artifact(),
            target=make_target(),
            dispatch_timestamp=make_artifact().pre_dispatch_decision.evaluation_timestamp,
        )
    with pytest.raises(PriceSourceRejectedError):
        produce(
            None,
            source=RejectingPriceObservationSource(),
        )


def test_oracle_b_field_mapping_and_frozen_schema() -> None:
    provenance = make_artifact()
    observation = make_observation(provenance=provenance)
    evidence = produce(observation, provenance=provenance)

    assert evidence.schema == "frontier-price-v1"
    assert evidence.market_source == observation.market_source
    assert evidence.source_observation_id == observation.source_observation_id
    assert evidence.provenance_id == provenance.provenance_id
    assert evidence.price_set_commitment == provenance.price_set_commitment
    assert evidence.risk_prices == provenance.risk_prices
    assert evidence.observed_at == observation.observed_at
    assert evidence.maximum_age == observation.maximum_age
    assert evidence.evidence_id.startswith("marketdata-price-v1-")
    assert len(evidence.evidence_id) == len("marketdata-price-v1-") + 64


def test_oracle_c_identical_inputs_are_byte_identical() -> None:
    first = produce(make_observation())
    second = produce(make_observation())
    assert first.model_dump_json() == second.model_dump_json()


def test_oracle_d_missing_symbol_fails_closed() -> None:
    facts = tuple(fact for fact in _provenance_prices() if fact.symbol == "AAPL")
    observation = make_observation(risk_prices=facts)
    with pytest.raises(PriceCoverageError):
        produce(observation)


def test_oracle_e_unexpected_symbol_fails_closed() -> None:
    facts = _provenance_prices() + (_fact("TSLA", Decimal("250.00")),)
    observation = make_observation(risk_prices=facts)
    with pytest.raises(PriceCoverageError):
        produce(observation)


def test_oracle_f_duplicate_symbol_fails_closed() -> None:
    aapl = _fact("AAPL", Decimal("100.00"))
    observation = forge_observation(
        risk_prices=(aapl, aapl), observation_id="marketdata-obs-v1-" + "1" * 64
    )
    with pytest.raises(PriceCoverageError):
        produce(observation)


def test_oracle_f_noncanonical_order_fails_closed() -> None:
    facts = tuple(reversed(_provenance_prices()))
    observation = forge_observation(risk_prices=facts)
    with pytest.raises(PriceCoverageError):
        produce(observation)


@pytest.mark.parametrize(
    "price",
    [
        Decimal(0),
        Decimal("-0.0"),
        Decimal(-1),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        1.5,
        True,
    ],
)
def test_oracles_g_h_i_invalid_prices_fail_closed(price: object) -> None:
    forged_fact = RiskPriceFact.model_construct(symbol="AAPL", price=price)
    facts = (forged_fact,) + tuple(
        fact for fact in _provenance_prices() if fact.symbol != "AAPL"
    )
    observation = forge_observation(
        risk_prices=facts, observation_id="marketdata-obs-v1-" + "2" * 64
    )
    with pytest.raises(PriceFactError):
        produce(observation)


@pytest.mark.parametrize("price", [Decimal(0), Decimal(-1), 1.5, True])
def test_frozen_fact_rejects_invalid_price_inputs(price: object) -> None:
    from quantforge.provenance import RiskProvenanceError

    with pytest.raises(RiskProvenanceError):
        RiskPriceFact(symbol="AAPL", price=price)


def test_oracle_j_wrong_provenance_identity_fails_closed() -> None:
    provenance = make_artifact()
    forged = FreshRiskProvenance.model_construct(
        schema=provenance.schema,
        provenance_id="riskprov1-" + "0" * 64,
        price_set_commitment=provenance.price_set_commitment,
        portfolio_commitment=provenance.portfolio_commitment,
        pre_dispatch_decision=provenance.pre_dispatch_decision,
        portfolio=provenance.portfolio,
        risk_prices=provenance.risk_prices,
        estimated_fee=provenance.estimated_fee,
        limits=provenance.limits,
    )
    with pytest.raises(PriceProvenanceMismatchError):
        produce(make_observation(provenance=provenance), provenance=forged)


def test_oracle_k_wrong_price_set_commitment_fails_closed() -> None:
    provenance = make_artifact()
    forged = FreshRiskProvenance.model_construct(
        schema=provenance.schema,
        provenance_id=provenance.provenance_id,
        price_set_commitment="riskprice1-" + "0" * 64,
        portfolio_commitment=provenance.portfolio_commitment,
        pre_dispatch_decision=provenance.pre_dispatch_decision,
        portfolio=provenance.portfolio,
        risk_prices=provenance.risk_prices,
        estimated_fee=provenance.estimated_fee,
        limits=provenance.limits,
    )
    with pytest.raises(PriceProvenanceMismatchError):
        produce(make_observation(provenance=provenance), provenance=forged)


def test_oracle_l_risk_prices_mismatch_fails_closed() -> None:
    provenance = make_artifact()
    tampered = tuple(
        _fact(fact.symbol, fact.price + Decimal(1)) for fact in provenance.risk_prices
    )
    observation = make_observation(provenance=provenance, risk_prices=tampered)
    with pytest.raises(PriceProvenanceMismatchError):
        produce(observation, provenance=provenance)


@pytest.mark.parametrize("source_id", ["", " with space", "x" * 129, 12345, None])
def test_oracle_m_malformed_source_identity_fails_closed(source_id: object) -> None:
    source = RecordingSource(make_observation(), source_id=source_id)
    with pytest.raises(PriceSourceIdentityError):
        produce(None, source=source)


def test_oracle_m_observation_source_mismatch_fails_closed() -> None:
    other_id = "marketdata-other-source"
    observation = VerifiedPriceObservation(
        schema="marketdata-observation-v1",
        observation_id=_observation_id(
            market_source=other_id,
            source_observation_id=TEST_OBSERVATION_ID,
            observed_at=FRESH_AT,
            maximum_age=MAXIMUM_AGE,
            risk_prices=_provenance_prices(),
        ),
        market_source=other_id,
        source_observation_id=TEST_OBSERVATION_ID,
        observed_at=FRESH_AT,
        maximum_age=MAXIMUM_AGE,
        risk_prices=_provenance_prices(),
    )
    with pytest.raises(PriceSourceIdentityError):
        produce(observation)


def test_oracle_m_malformed_observation_source_field_fails_closed() -> None:
    observation = forge_observation(
        risk_prices=_provenance_prices(), market_source="bad source"
    )
    with pytest.raises(PriceSourceIdentityError):
        produce(observation)


def test_oracle_n_observation_identity_mismatch_fails_closed() -> None:
    observation = forge_observation(
        risk_prices=_provenance_prices(),
        observation_id="marketdata-obs-v1-" + "3" * 64,
    )
    with pytest.raises(PriceObservationIdentityError):
        produce(observation)


def test_oracle_o_target_submission_mismatch_fails_closed() -> None:
    target = make_target(submission_id="broker1-" + "0" * 32)
    source = RecordingSource(make_observation())
    with pytest.raises(PriceTargetMismatchError):
        produce(None, target=target, source=source)
    assert source.calls == 0


def test_oracle_o_forged_target_fails_closed() -> None:
    from quantforge.frontier import ExecutionTargetBinding

    target = ExecutionTargetBinding.model_construct(
        schema="frontier-target-v1",
        broker="",
        environment="SANDBOX",
        account_reference="acct-7",
        adapter_id="adapter-x",
        adapter_version="1",
        submission_id="broker1-" + "0" * 32,
    )
    with pytest.raises(PriceTargetMismatchError):
        produce(None, target=target, source=RecordingSource(make_observation()))


def test_oracle_p_future_observation_fails_closed() -> None:
    observation = make_observation(
        observed_at=make_artifact().pre_dispatch_decision.evaluation_timestamp
        + timedelta(microseconds=1)
    )
    with pytest.raises(PriceFutureObservationError):
        produce(observation)


def test_oracle_q_stale_observation_fails_closed() -> None:
    moment = make_artifact().pre_dispatch_decision.evaluation_timestamp
    observation = make_observation(
        observed_at=moment - MAXIMUM_AGE - timedelta(microseconds=1)
    )
    with pytest.raises(PriceStaleObservationError):
        produce(observation)


def test_oracle_r_exact_boundary_matches_frontier() -> None:
    provenance = make_artifact()
    moment = provenance.pre_dispatch_decision.evaluation_timestamp
    observation = make_observation(
        provenance=provenance, observed_at=moment - MAXIMUM_AGE
    )
    evidence = produce(observation, provenance=provenance)
    decision = evaluate_frontier_eligibility(
        provenance,
        moment,
        make_target(),
        price_evidence=evidence,
        account_evidence=None,
        durable_record=None,
        commit_receipt=None,
        recovery_evidence=None,
        operator_evidence=None,
        capability=None,
        claim=None,
    )
    assert FrontierReason.STALE_PRICE_EVIDENCE not in decision.reasons
    assert FrontierReason.FUTURE_PRICE_EVIDENCE not in decision.reasons
    assert FrontierReason.PRICE_PROVENANCE_MISMATCH not in decision.reasons


def test_oracle_s_naive_dispatch_timestamp_fails_closed() -> None:
    naive = make_artifact().pre_dispatch_decision.evaluation_timestamp.replace(
        tzinfo=None
    )
    with pytest.raises(PriceTimestampError):
        produce(make_observation(), dispatch_timestamp=naive)


def test_oracle_s_wrong_dispatch_instant_fails_closed() -> None:
    moment = make_artifact().pre_dispatch_decision.evaluation_timestamp
    with pytest.raises(PriceTimestampError):
        produce(make_observation(), dispatch_timestamp=moment + timedelta(seconds=1))


def test_oracle_s_equivalent_offset_instant_is_accepted() -> None:
    plus_eight = timezone(timedelta(hours=8))
    moment = make_artifact().pre_dispatch_decision.evaluation_timestamp
    evidence = produce(
        make_observation(), dispatch_timestamp=moment.astimezone(plus_eight)
    )
    assert evidence.schema == "frontier-price-v1"


def test_oracle_s_naive_observation_timestamp_fails_closed() -> None:
    moment = make_artifact().pre_dispatch_decision.evaluation_timestamp
    observation = forge_observation(
        risk_prices=_provenance_prices(),
        observed_at=moment.replace(tzinfo=None),
    )
    with pytest.raises(PriceTimestampError):
        produce(observation)


def test_oracle_t_ordinary_source_exception_is_wrapped_without_secret_text() -> None:
    failure = ValueError("secret-token-XYZ")
    source = RecordingSource(make_observation(), failure=failure)
    with pytest.raises(PriceSourceError) as excinfo:
        produce(None, source=source)
    assert excinfo.value.__cause__ is failure
    assert "secret-token-XYZ" not in str(excinfo.value)
    assert TEST_SOURCE_ID in str(excinfo.value)
    assert source.calls == 1


def test_oracle_t_typed_source_errors_propagate_unchanged() -> None:
    rejection = PriceSourceRejectedError("rejected by policy")
    source = RecordingSource(make_observation(), failure=rejection)
    with pytest.raises(PriceSourceRejectedError) as excinfo:
        produce(None, source=source)
    assert excinfo.value is rejection

    typed = PriceObservationError("malformed")
    source = RecordingSource(make_observation(), failure=typed)
    with pytest.raises(PriceObservationError) as excinfo:
        produce(None, source=source)
    assert excinfo.value is typed


def test_oracle_t_base_exception_propagates_unchanged() -> None:
    class _Abort(BaseException):
        pass

    abort = _Abort("abort")
    source = RecordingSource(make_observation(), failure=abort)
    with pytest.raises(_Abort) as excinfo:
        produce(None, source=source)
    assert excinfo.value is abort
    assert source.calls == 1


def test_oracle_u_source_called_exactly_once_on_success() -> None:
    source = RecordingSource(make_observation())
    produce(None, source=source)
    assert source.calls == 1
    assert source.source_id_reads == 1
    assert source.requests[0].schema == "marketdata-request-v1"
    assert source.requests[0].symbols == ("AAPL", "MSFT")


def test_oracle_u_source_not_called_when_pre_call_validation_fails() -> None:
    source = RecordingSource(make_observation())
    with pytest.raises(PriceTargetMismatchError):
        produce(
            None,
            target=make_target(submission_id="broker1-" + "1" * 32),
            source=source,
        )
    assert source.calls == 0


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (None, PriceArgumentError),
        (object(), PriceArgumentError),
    ],
)
def test_oracle_u_structural_source_failures(source: object, expected: type) -> None:
    with pytest.raises(expected):
        produce(None, source=source)


def test_source_without_source_id_attribute_fails_closed() -> None:
    class _NoIdentity:
        def observe(self, request: PriceObservationRequest) -> object:
            return None

    with pytest.raises(PriceArgumentError):
        produce(None, source=_NoIdentity())


def test_non_callable_observe_fails_closed() -> None:
    class _NotCallable:
        source_id = TEST_SOURCE_ID
        observe = None

    with pytest.raises(PriceArgumentError):
        produce(None, source=_NotCallable())


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"provenance": object()}, TypeError),
        ({"target": object()}, TypeError),
        ({"dispatch_timestamp": "2030-01-01T12:02:00Z"}, TypeError),
    ],
)
def test_wrong_runtime_argument_types_raise_type_error(
    kwargs: dict[str, object], expected: type
) -> None:
    provenance = kwargs.get("provenance", make_artifact())
    target = kwargs.get("target", make_target())
    moment = kwargs.get(
        "dispatch_timestamp",
        make_artifact().pre_dispatch_decision.evaluation_timestamp,
    )
    with pytest.raises(expected):
        from quantforge.marketdata import produce_price_freshness_evidence

        produce_price_freshness_evidence(
            provenance=provenance,  # type: ignore[arg-type]
            target=target,  # type: ignore[arg-type]
            dispatch_timestamp=moment,  # type: ignore[arg-type]
            source=RecordingSource(make_observation()),
        )


def test_null_source_is_argument_error() -> None:
    with pytest.raises(PriceArgumentError):
        produce(None, source=None)


def test_request_model_rejects_noncanonical_inputs() -> None:
    with pytest.raises(PriceArgumentError):
        PriceObservationRequest(schema="marketdata-request-v1", symbols=())
    with pytest.raises(PriceArgumentError):
        PriceObservationRequest(
            schema="marketdata-request-v1", symbols=("AAPL", "AAPL")
        )
    with pytest.raises(PriceArgumentError):
        PriceObservationRequest(
            schema="marketdata-request-v1", symbols=("MSFT", "AAPL")
        )
    with pytest.raises(PriceArgumentError):
        PriceObservationRequest(
            schema="marketdata-request-v1", symbols=("AAPL", "\ud800")
        )
    with pytest.raises(PriceArgumentError):
        PriceObservationRequest(
            schema="marketdata-request-v1",
            symbols=["AAPL"],  # type: ignore[arg-type]
        )


def test_same_symbol_order_and_position_overlap_uses_union() -> None:
    provenance = make_artifact()
    request_symbols = tuple(
        sorted(
            {position.symbol for position in provenance.portfolio.positions}
            | {provenance.pre_dispatch_decision.envelope.order.symbol},
            key=lambda symbol: symbol.encode("utf-8"),
        )
    )
    assert request_symbols == ("AAPL", "MSFT")
    assert produce(make_observation(provenance=provenance)).schema == (
        "frontier-price-v1"
    )


def test_utc_normalization_of_observation_and_dispatch() -> None:
    provenance = make_artifact()
    moment = provenance.pre_dispatch_decision.evaluation_timestamp
    plus_eight = timezone(timedelta(hours=8))
    observation = make_observation(
        provenance=provenance,
        observed_at=(moment - timedelta(seconds=10)).astimezone(plus_eight),
    )
    assert observation.observed_at.tzinfo is UTC
    evidence = produce(observation, provenance=provenance)
    assert evidence.observed_at == moment - timedelta(seconds=10)
