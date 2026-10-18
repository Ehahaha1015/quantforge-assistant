"""Deterministic PRICE-001 test fixtures (tests/marketdata only)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from quantforge.frontier import ExecutionTargetBinding
from quantforge.marketdata import VerifiedPriceObservation
from quantforge.marketdata.canonical import _observation_id
from quantforge.provenance import RiskPriceFact
from tests.provenance._fixtures import EVALUATED_AT, SUBMISSION_ID, make_artifact

TEST_SOURCE_ID = "marketdata-test-source"
TEST_OBSERVATION_ID = "obs-1"
FRESH_AT = EVALUATED_AT - timedelta(seconds=10)
MAXIMUM_AGE = timedelta(seconds=30)
OBSERVATION_SCHEMA = "marketdata-observation-v1"
REQUEST_SCHEMA = "marketdata-request-v1"
_UNSET = object()


def make_target(**changes: Any) -> ExecutionTargetBinding:
    values: dict[str, Any] = {
        "schema": "frontier-target-v1",
        "broker": "broker-x",
        "environment": "SANDBOX",
        "account_reference": "acct-7",
        "adapter_id": "adapter-x",
        "adapter_version": "1",
        "submission_id": SUBMISSION_ID,
    }
    values.update(changes)
    return ExecutionTargetBinding(**values)


def make_observation(
    *,
    provenance: Any | None = None,
    risk_prices: tuple[RiskPriceFact, ...] | None = None,
    observed_at: Any = FRESH_AT,
    maximum_age: Any = MAXIMUM_AGE,
    market_source: str = TEST_SOURCE_ID,
    source_observation_id: str = TEST_OBSERVATION_ID,
) -> VerifiedPriceObservation:
    artifact = make_artifact() if provenance is None else provenance
    facts = artifact.risk_prices if risk_prices is None else risk_prices
    return VerifiedPriceObservation(
        schema=OBSERVATION_SCHEMA,
        observation_id=_observation_id(
            market_source=market_source,
            source_observation_id=source_observation_id,
            observed_at=observed_at,
            maximum_age=maximum_age,
            risk_prices=facts,
        ),
        market_source=market_source,
        source_observation_id=source_observation_id,
        observed_at=observed_at,
        maximum_age=maximum_age,
        risk_prices=facts,
    )


def forge_observation(
    *,
    risk_prices: tuple[RiskPriceFact, ...],
    observation_id: str | None = None,
    market_source: Any = TEST_SOURCE_ID,
    source_observation_id: Any = TEST_OBSERVATION_ID,
    observed_at: Any = FRESH_AT,
    maximum_age: Any = MAXIMUM_AGE,
    schema: Any = OBSERVATION_SCHEMA,
) -> VerifiedPriceObservation:
    """Build an observation with model_construct, bypassing model validation."""
    derived = (
        _observation_id(
            market_source=market_source,
            source_observation_id=source_observation_id,
            observed_at=observed_at,
            maximum_age=maximum_age,
            risk_prices=risk_prices,
        )
        if observation_id is None
        else observation_id
    )
    return VerifiedPriceObservation.model_construct(
        schema=schema,
        observation_id=derived,
        market_source=market_source,
        source_observation_id=source_observation_id,
        observed_at=observed_at,
        maximum_age=maximum_age,
        risk_prices=risk_prices,
    )


class RecordingSource:
    """Deterministic test-only source double: counts reads and calls."""

    def __init__(
        self,
        observation: Any = None,
        *,
        source_id: Any = TEST_SOURCE_ID,
        failure: BaseException | None = None,
    ) -> None:
        self.observation = observation
        self.failure = failure
        self._source_id = source_id
        self.source_id_reads = 0
        self.calls = 0
        self.requests: list[Any] = []

    @property
    def source_id(self) -> Any:
        self.source_id_reads += 1
        return self._source_id

    def observe(self, request: Any) -> Any:
        self.calls += 1
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return self.observation


def produce(
    observation: Any = None,
    *,
    provenance: Any | None = None,
    target: Any | None = None,
    dispatch_timestamp: Any = EVALUATED_AT,
    source: Any = _UNSET,
) -> Any:
    from quantforge.marketdata import produce_price_freshness_evidence

    artifact = make_artifact() if provenance is None else provenance
    selected_target = make_target() if target is None else target
    selected_source = RecordingSource(observation) if source is _UNSET else source
    return produce_price_freshness_evidence(
        provenance=artifact,
        target=selected_target,
        dispatch_timestamp=dispatch_timestamp,
        source=selected_source,
    )
