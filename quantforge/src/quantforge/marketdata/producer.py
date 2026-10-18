"""Frozen PRICE-001 deterministic producer boundary (contract section 17)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ..frontier import (
    ExecutionTargetBinding,
    FrontierError,
    PriceFreshnessEvidence,
)
from ..provenance import (
    FreshRiskProvenance,
    RiskPriceFact,
    RiskProvenanceError,
)
from .canonical import _evidence_id, _observation_id
from .errors import (
    PriceArgumentError,
    PriceCoverageError,
    PriceEvidenceConstructionError,
    PriceEvidenceError,
    PriceFactError,
    PriceFutureObservationError,
    PriceObservationError,
    PriceObservationIdentityError,
    PriceProvenanceMismatchError,
    PriceSourceError,
    PriceSourceIdentityError,
    PriceStaleObservationError,
    PriceTargetMismatchError,
    PriceTimestampError,
)
from .models import PriceObservationRequest, VerifiedPriceObservation
from .source import PriceObservationSource, RejectingPriceObservationSource

_OPAQUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_OBSERVATION_SCHEMA = "marketdata-observation-v1"
_REQUEST_SCHEMA = "marketdata-request-v1"
_REJECTING_SOURCE = RejectingPriceObservationSource()


def _opaque(value: Any) -> bool:
    return isinstance(value, str) and _OPAQUE_PATTERN.fullmatch(value) is not None


def _utc(value: Any, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise PriceTimestampError(f"{name} must be a datetime")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PriceTimestampError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)
    except PriceTimestampError:
        raise
    except (OverflowError, TypeError, ValueError) as exc:
        raise PriceTimestampError(
            f"{name} is outside the representable UTC range"
        ) from exc


def _revalidated_provenance(
    provenance: FreshRiskProvenance,
) -> FreshRiskProvenance:
    try:
        checked = FreshRiskProvenance(
            schema=provenance.schema,
            provenance_id=provenance.provenance_id,
            price_set_commitment=provenance.price_set_commitment,
            portfolio_commitment=provenance.portfolio_commitment,
            pre_dispatch_decision=provenance.pre_dispatch_decision,
            portfolio=provenance.portfolio,
            risk_prices=provenance.risk_prices,
            estimated_fee=provenance.estimated_fee,
            limits=provenance.limits,
        )
    except RiskProvenanceError as exc:
        raise PriceProvenanceMismatchError("provenance is invalid") from exc
    if checked != provenance:
        raise PriceProvenanceMismatchError(
            "provenance reconstruction does not match the supplied value"
        )
    return checked


def _revalidated_target(
    target: ExecutionTargetBinding,
) -> ExecutionTargetBinding:
    try:
        checked = ExecutionTargetBinding(
            schema=target.schema,
            broker=target.broker,
            environment=target.environment,
            account_reference=target.account_reference,
            adapter_id=target.adapter_id,
            adapter_version=target.adapter_version,
            submission_id=target.submission_id,
        )
    except FrontierError as exc:
        raise PriceTargetMismatchError("target is invalid") from exc
    if checked != target:
        raise PriceTargetMismatchError(
            "target reconstruction does not match the supplied value"
        )
    return checked


def _required_symbols(
    provenance: FreshRiskProvenance,
) -> set[str]:
    positions = provenance.portfolio.positions
    order_symbol = provenance.pre_dispatch_decision.envelope.order.symbol
    return {position.symbol for position in positions} | {order_symbol}


def _validated_price_facts(
    facts: Any,
) -> tuple[RiskPriceFact, ...]:
    if not isinstance(facts, tuple):
        raise PriceFactError("risk_prices must be a tuple")
    checked: list[RiskPriceFact] = []
    for fact in facts:
        if not isinstance(fact, RiskPriceFact):
            raise PriceFactError("risk_prices must contain RiskPriceFact values")
        price = fact.price
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise PriceFactError("risk price must be a finite positive Decimal")
        symbol = fact.symbol
        if not isinstance(symbol, str) or not symbol:
            raise PriceFactError("risk price symbols must be non-empty strings")
        try:
            symbol.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise PriceFactError("risk price symbols must be valid UTF-8") from exc
        checked.append(fact)
    return tuple(checked)


def _canonical_price_order(
    facts: tuple[RiskPriceFact, ...],
) -> None:
    symbols = [fact.symbol for fact in facts]
    if len(set(symbols)) != len(symbols):
        raise PriceCoverageError("risk_prices contains a duplicate symbol")
    if list(facts) != sorted(facts, key=lambda fact: fact.symbol.encode("utf-8")):
        raise PriceCoverageError("risk_prices is not in canonical symbol order")


def _observe_once(
    source: PriceObservationSource,
    request: PriceObservationRequest,
    source_id: str,
) -> Any:
    try:
        return source.observe(request)
    except PriceEvidenceError:
        raise
    except Exception as exc:
        raise PriceSourceError(
            f"price observation source {source_id!r} failed"
        ) from exc


def produce_price_freshness_evidence(
    *,
    provenance: FreshRiskProvenance,
    target: ExecutionTargetBinding,
    dispatch_timestamp: datetime,
    source: PriceObservationSource = _REJECTING_SOURCE,
) -> PriceFreshnessEvidence:
    """Produce frozen price freshness evidence from one verified observation."""
    if type(provenance) is not FreshRiskProvenance:
        raise TypeError(
            "provenance must be a quantforge.provenance.FreshRiskProvenance"
        )
    if type(target) is not ExecutionTargetBinding:
        raise TypeError("target must be a quantforge.frontier.ExecutionTargetBinding")
    if not isinstance(dispatch_timestamp, datetime):
        raise TypeError("dispatch_timestamp must be a datetime")
    if source is None or not hasattr(source, "observe") or not callable(source.observe):
        raise PriceArgumentError("source must provide a callable observe attribute")

    try:
        source_id = source.source_id
    except AttributeError as exc:
        raise PriceArgumentError("source must provide source_id") from exc
    if not _opaque(source_id):
        raise PriceSourceIdentityError("source_id is malformed")

    checked_provenance = _revalidated_provenance(provenance)
    checked_target = _revalidated_target(target)

    required = _required_symbols(checked_provenance)
    provenance_symbols = {fact.symbol for fact in checked_provenance.risk_prices}
    if provenance_symbols != required:
        raise PriceCoverageError(
            "provenance risk prices do not cover the required symbols"
        )

    envelope = checked_provenance.pre_dispatch_decision.envelope
    if checked_target.submission_id != envelope.submission_id:
        raise PriceTargetMismatchError(
            "target submission does not match the provenance"
        )

    moment = _utc(dispatch_timestamp, name="dispatch_timestamp")
    if moment != checked_provenance.pre_dispatch_decision.evaluation_timestamp:
        raise PriceTimestampError(
            "dispatch instant does not match the frozen evaluation instant"
        )

    request = PriceObservationRequest(
        schema=_REQUEST_SCHEMA,
        symbols=tuple(sorted(required, key=lambda symbol: symbol.encode("utf-8"))),
    )

    observation = _observe_once(source, request, source_id)

    if type(observation) is not VerifiedPriceObservation:
        raise PriceObservationError("source must return a VerifiedPriceObservation")
    if observation.schema != _OBSERVATION_SCHEMA:
        raise PriceObservationError("observation schema is malformed")
    if not _opaque(observation.market_source):
        raise PriceSourceIdentityError("observation market_source is malformed")
    if observation.market_source != source_id:
        raise PriceSourceIdentityError(
            "observation market_source does not match the source"
        )
    if not _opaque(observation.source_observation_id):
        raise PriceObservationError("observation source_observation_id is malformed")
    observed_at = _utc(observation.observed_at, name="observed_at")
    if not isinstance(
        observation.maximum_age, timedelta
    ) or observation.maximum_age <= timedelta(0):
        raise PriceObservationError("observation maximum_age is invalid")

    facts = _validated_price_facts(observation.risk_prices)
    _canonical_price_order(facts)
    observed_symbols = {fact.symbol for fact in facts}
    if observed_symbols != required:
        raise PriceCoverageError(
            "observation symbol coverage does not match the required symbols"
        )
    if facts != checked_provenance.risk_prices:
        raise PriceProvenanceMismatchError(
            "observation risk prices do not match the provenance"
        )

    derived_observation_id = _observation_id(
        market_source=observation.market_source,
        source_observation_id=observation.source_observation_id,
        observed_at=observed_at,
        maximum_age=observation.maximum_age,
        risk_prices=facts,
    )
    if observation.observation_id != derived_observation_id:
        raise PriceObservationIdentityError("observation identity does not re-derive")

    if observed_at > moment:
        raise PriceFutureObservationError(
            "observation is later than the dispatch instant"
        )
    if moment - observed_at > observation.maximum_age:
        raise PriceStaleObservationError("observation age exceeds maximum_age")

    evidence_id = _evidence_id(
        market_source=observation.market_source,
        source_observation_id=observation.source_observation_id,
        observed_at=observed_at,
        maximum_age=observation.maximum_age,
        risk_prices=facts,
        provenance_id=checked_provenance.provenance_id,
        price_set_commitment=checked_provenance.price_set_commitment,
        submission_id=checked_target.submission_id,
    )
    try:
        return PriceFreshnessEvidence(
            schema="frontier-price-v1",
            evidence_id=evidence_id,
            market_source=observation.market_source,
            source_observation_id=observation.source_observation_id,
            provenance_id=checked_provenance.provenance_id,
            price_set_commitment=checked_provenance.price_set_commitment,
            risk_prices=checked_provenance.risk_prices,
            observed_at=observed_at,
            maximum_age=observation.maximum_age,
        )
    except FrontierError as exc:
        raise PriceEvidenceConstructionError(
            "frozen price freshness evidence construction failed"
        ) from exc


__all__ = ["produce_price_freshness_evidence"]
