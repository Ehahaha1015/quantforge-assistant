"""One-shot operator runner for the authenticated MARKETDATA-002 smoke."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal, NoReturn, cast

from quantforge.broker import create_broker_submission_envelope
from quantforge.domain import OrderIntent, Position
from quantforge.frontier import ExecutionTargetBinding, FrontierError
from quantforge.live import authorize_live_order, create_live_authorization
from quantforge.marketdata import (
    PriceArgumentError,
    PriceCoverageError,
    PriceEvidenceConstructionError,
    PriceEvidenceError,
    PriceFutureObservationError,
    PriceObservationError,
    PriceObservationIdentityError,
    PriceObservationRequest,
    PriceProvenanceMismatchError,
    PriceSourceError,
    PriceSourceIdentityError,
    PriceStaleObservationError,
    PriceTargetMismatchError,
    PriceTimestampError,
    VerifiedPriceObservation,
    produce_price_freshness_evidence,
)
from quantforge.portfolio import PortfolioState
from quantforge.predispatch import AuthorizationFreshnessPolicy, SubmissionHistoryState
from quantforge.provenance import (
    FreshRiskProvenance,
    RiskProvenanceError,
    evaluate_pre_dispatch_with_provenance,
)
from quantforge.risk import RiskDecision, RiskLimits

from .marketdata_alpaca import build_alpaca_iex_source

FROZEN_SYMBOLS = ("AAPL", "MSFT", "SPY")
_TARGET_BROKER = "alpaca"
_TARGET_ENVIRONMENT: Literal["PRODUCTION"] = "PRODUCTION"
_TARGET_ACCOUNT = "alpaca-account-001"
_TARGET_ADAPTER = "marketdata-alpaca-iex-v1"
_TARGET_ADAPTER_VERSION = "v1"

ReasonCode = Literal[
    "DISPATCH_TIMESTAMP_INVALID",
    "SOURCE_FAILURE",
    "SYMBOL_COVERAGE_INVALID",
    "OBSERVATION_INVALID",
    "PRICE_CORRESPONDENCE_MISMATCH",
    "OBSERVATION_IN_FUTURE",
    "OBSERVATION_STALE",
    "PRICE_EVIDENCE_INVALID",
]

_REASON_CODES: Final[tuple[ReasonCode, ...]] = (
    "DISPATCH_TIMESTAMP_INVALID",
    "SOURCE_FAILURE",
    "SYMBOL_COVERAGE_INVALID",
    "OBSERVATION_INVALID",
    "PRICE_CORRESPONDENCE_MISMATCH",
    "OBSERVATION_IN_FUTURE",
    "OBSERVATION_STALE",
    "PRICE_EVIDENCE_INVALID",
)

_RunnerStage = Literal[
    "PRE_OBSERVE",
    "OBSERVE",
    "POST_OBSERVE_BINDING",
    "PRICE_REPLAY_VALIDATION",
]
_PRE_OBSERVE: _RunnerStage = "PRE_OBSERVE"
_OBSERVE: _RunnerStage = "OBSERVE"
_POST_OBSERVE_BINDING: _RunnerStage = "POST_OBSERVE_BINDING"
_PRICE_REPLAY_VALIDATION: _RunnerStage = "PRICE_REPLAY_VALIDATION"
_STAGES: Final[frozenset[_RunnerStage]] = frozenset(
    {_PRE_OBSERVE, _OBSERVE, _POST_OBSERVE_BINDING, _PRICE_REPLAY_VALIDATION}
)


@contextmanager
def _diagnostic_stage(stage: _RunnerStage) -> Iterator[None]:
    """Attach private runner stage context without changing domain errors."""
    try:
        yield
    except PriceEvidenceError as exc:
        _mark_stage(exc, stage)
        raise


def _canonical_instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _duration_microseconds(value: timedelta) -> str:
    return str(
        value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
    )


def _invalid_dispatch_timestamp() -> NoReturn:
    error = PriceTimestampError("dispatch timestamp must be a canonical UTC datetime")
    _mark_stage(error, _PRE_OBSERVE)
    raise error


def _mark_stage(error: PriceEvidenceError, stage: _RunnerStage) -> None:
    cast(Any, error)._smoke_stage = stage


def _parse_dispatch_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _invalid_dispatch_timestamp()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        error = PriceTimestampError(
            "dispatch timestamp must be a canonical UTC datetime"
        )
        _mark_stage(error, _PRE_OBSERVE)
        raise error from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _invalid_dispatch_timestamp()
    normalized = parsed.astimezone(UTC)
    if _canonical_instant(normalized) != value:
        _invalid_dispatch_timestamp()
    return normalized


class _ReplaySource:
    """Replay one captured observation without another source or credential call."""

    __slots__ = ("_observation", "_used")

    def __init__(self, observation: VerifiedPriceObservation) -> None:
        self._observation = observation
        self._used = False

    @property
    def source_id(self) -> str:
        return self._observation.market_source

    def observe(self, request: PriceObservationRequest) -> VerifiedPriceObservation:
        if request.symbols != FROZEN_SYMBOLS:
            raise PriceCoverageError(
                "replay request symbols do not match the frozen smoke symbols"
            )
        if self._used:
            raise PriceEvidenceError(
                "captured observation was requested more than once"
            )
        self._used = True
        return self._observation


def _capture_once(
    source: Any,
    request: PriceObservationRequest,
    *,
    dispatch_timestamp: datetime,
) -> tuple[VerifiedPriceObservation, Any]:
    failure: PriceSourceError | None = None
    captured: Any = None
    try:
        captured = source._capture_smoke_observation(
            request,
            dispatch_timestamp=dispatch_timestamp,
        )
    except PriceEvidenceError:
        raise
    except Exception:  # noqa: BLE001 - frozen source failure boundary
        failure = PriceSourceError("price observation source failed")
    if failure is not None:
        raise failure
    observation: object = None
    diagnostic: object = None
    capture_shape_failure = False
    try:
        observation = captured.observation
        diagnostic = captured.diagnostic
    except Exception:  # noqa: BLE001 - frozen capture shape boundary
        capture_shape_failure = True
    if capture_shape_failure:
        raise PriceObservationError(
            "source must return a captured smoke observation"
        )
    if type(observation) is not VerifiedPriceObservation:
        raise PriceObservationError("source must return a VerifiedPriceObservation")
    return observation, diagnostic


def _build_provenance(
    *,
    dispatch_timestamp: datetime,
    observation: VerifiedPriceObservation,
) -> FreshRiskProvenance:
    order = OrderIntent(
        strategy_id="marketdata-smoke-001",
        symbol="SPY",
        timestamp=dispatch_timestamp - timedelta(minutes=2),
        side="BUY",
        quantity=Decimal(1),
    )
    authorization = create_live_authorization(
        order,
        authorized_by="marketdata-smoke-operator",
        authorized_at=dispatch_timestamp - timedelta(minutes=1),
    )
    permission = authorize_live_order(order, RiskDecision(approved=True), authorization)
    envelope = create_broker_submission_envelope(order, permission)
    portfolio = PortfolioState(
        initial_cash=Decimal(100000000),
        cash=Decimal(100000000),
        positions=(
            Position(symbol="AAPL", quantity=Decimal(1), average_price=Decimal(100)),
            Position(symbol="MSFT", quantity=Decimal(1), average_price=Decimal(100)),
        ),
        cumulative_fees=Decimal(0),
        last_fill_timestamp=dispatch_timestamp - timedelta(minutes=3),
    )
    limits = RiskLimits(
        max_order_notional=Decimal(1000000000),
        max_position_notional=Decimal(1000000000),
        max_gross_exposure=Decimal(1000000000),
    )
    history = SubmissionHistoryState(
        submission_id=envelope.submission_id,
        state="NEVER_DISPATCHED",
    )
    prices = {fact.symbol: fact.price for fact in observation.risk_prices}
    return evaluate_pre_dispatch_with_provenance(
        envelope,
        history,
        AuthorizationFreshnessPolicy(max_authorization_age=timedelta(minutes=5)),
        dispatch_timestamp,
        portfolio,
        prices,
        Decimal(1),
        limits,
    )


def _build_target(provenance: FreshRiskProvenance) -> ExecutionTargetBinding:
    return ExecutionTargetBinding(
        schema="frontier-target-v1",
        broker=_TARGET_BROKER,
        environment=_TARGET_ENVIRONMENT,
        account_reference=_TARGET_ACCOUNT,
        adapter_id=_TARGET_ADAPTER,
        adapter_version=_TARGET_ADAPTER_VERSION,
        submission_id=provenance.pre_dispatch_decision.envelope.submission_id,
    )


def _safe_result(
    *,
    dispatch_timestamp: datetime,
    provenance: FreshRiskProvenance,
    observation: VerifiedPriceObservation,
    evidence: Any,
) -> dict[str, object]:
    prices = {fact.symbol: str(fact.price) for fact in evidence.risk_prices}
    return {
        "status": "SUCCESS",
        "dispatch_timestamp": _canonical_instant(dispatch_timestamp),
        "symbols": list(FROZEN_SYMBOLS),
        "prices": prices,
        "observed_at": _canonical_instant(evidence.observed_at),
        "source_id": evidence.market_source,
        "market_source": evidence.market_source,
        "source_observation_id": evidence.source_observation_id,
        "observation_id": observation.observation_id,
        "maximum_age": _duration_microseconds(evidence.maximum_age),
        "freshness_success": True,
        "evidence_id": evidence.evidence_id,
        "provenance_id": provenance.provenance_id,
    }


def run_smoke(*, dispatch_timestamp: datetime) -> dict[str, object]:
    """Run one authenticated observation through the closed APIs."""
    source_failure: PriceSourceError | None = None
    try:
        with _diagnostic_stage(_OBSERVE):
            source = build_alpaca_iex_source()
    except PriceEvidenceError:
        raise
    except Exception:  # noqa: BLE001 - frozen source-construction boundary
        source_failure = PriceSourceError("price observation source failed")
    if source_failure is not None:
        _mark_stage(source_failure, _OBSERVE)
        raise source_failure
    request = PriceObservationRequest(
        schema="marketdata-request-v1",
        symbols=FROZEN_SYMBOLS,
    )
    with _diagnostic_stage(_OBSERVE):
        observation, diagnostic = _capture_once(
            source,
            request,
            dispatch_timestamp=dispatch_timestamp,
        )
    with _diagnostic_stage(_POST_OBSERVE_BINDING):
        if observation.market_source != source.source_id:
            raise PriceSourceIdentityError(
                "observation source identity does not match the source"
            )
        if tuple(fact.symbol for fact in observation.risk_prices) != FROZEN_SYMBOLS:
            raise PriceCoverageError(
                "observation risk prices do not cover the frozen smoke symbols"
            )

        provenance = _build_provenance(
            dispatch_timestamp=dispatch_timestamp,
            observation=observation,
        )
        target = _build_target(provenance)
    try:
        with _diagnostic_stage(_PRICE_REPLAY_VALIDATION):
            evidence = produce_price_freshness_evidence(
                provenance=provenance,
                target=target,
                dispatch_timestamp=dispatch_timestamp,
                source=_ReplaySource(observation),
            )
    except PriceStaleObservationError as exc:
        cast(Any, exc)._smoke_diagnostic = diagnostic
        raise
    return _safe_result(
        dispatch_timestamp=dispatch_timestamp,
        provenance=provenance,
        observation=observation,
        evidence=evidence,
    )


def _json_output(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _price_reason(exc: PriceEvidenceError) -> ReasonCode:
    stage_value = getattr(exc, "_smoke_stage", None)
    stage: _RunnerStage | None = (
        cast(_RunnerStage, stage_value)
        if isinstance(stage_value, str) and stage_value in _STAGES
        else None
    )

    if isinstance(exc, PriceFutureObservationError):
        return "OBSERVATION_IN_FUTURE"
    if isinstance(exc, PriceStaleObservationError):
        return "OBSERVATION_STALE"
    if stage == _PRE_OBSERVE and isinstance(exc, PriceTimestampError):
        return "DISPATCH_TIMESTAMP_INVALID"
    if stage == _OBSERVE:
        if isinstance(exc, (PriceArgumentError, PriceSourceError)):
            return "SOURCE_FAILURE"
        if isinstance(exc, PriceCoverageError):
            return "SYMBOL_COVERAGE_INVALID"
        if isinstance(exc, PriceObservationError):
            return "OBSERVATION_INVALID"
        if isinstance(exc, PriceTimestampError):
            return "OBSERVATION_INVALID"
    if stage == _POST_OBSERVE_BINDING:
        if isinstance(exc, PriceSourceError):
            return "SOURCE_FAILURE"
        if isinstance(exc, PriceCoverageError):
            return "PRICE_CORRESPONDENCE_MISMATCH"
        if isinstance(exc, PriceObservationIdentityError):
            return "PRICE_CORRESPONDENCE_MISMATCH"
        if isinstance(exc, PriceObservationError):
            return "OBSERVATION_INVALID"
        if isinstance(exc, PriceEvidenceError):
            return "PRICE_CORRESPONDENCE_MISMATCH"
    if stage == _PRICE_REPLAY_VALIDATION:
        if isinstance(exc, PriceSourceError):
            return "SOURCE_FAILURE"
        if isinstance(
            exc,
            (
                PriceCoverageError,
                PriceObservationIdentityError,
                PriceProvenanceMismatchError,
                PriceSourceIdentityError,
                PriceTargetMismatchError,
                PriceTimestampError,
            ),
        ):
            return "PRICE_CORRESPONDENCE_MISMATCH"
        if isinstance(exc, PriceObservationError):
            return "OBSERVATION_INVALID"
        if isinstance(exc, PriceEvidenceConstructionError):
            return "PRICE_EVIDENCE_INVALID"
    return "PRICE_EVIDENCE_INVALID"


def _serialize_diagnostic(diagnostic: Any) -> dict[str, object]:
    return {
        "aggregate_age_category": diagnostic.aggregate_age_category,
        "per_symbol_relation": {
            symbol: category
            for symbol, category in diagnostic.per_symbol_relation
        },
    }


def _safe_failure(exc: Exception) -> dict[str, object]:
    error: dict[str, object]
    if isinstance(exc, FrontierError):
        error = {"type": "FrontierError", "message": "frontier input is invalid"}
    elif isinstance(exc, PriceEvidenceError):
        reason = _price_reason(exc)
        error = {
            "type": "PriceEvidenceError",
            "message": "market data evidence is invalid",
            "reason": reason,
        }
        if reason == "OBSERVATION_STALE":
            try:
                diagnostic = cast(Any, exc)._smoke_diagnostic
            except AttributeError:
                diagnostic = None
            if diagnostic is not None:
                error["diagnostic"] = _serialize_diagnostic(diagnostic)
    elif isinstance(exc, RiskProvenanceError):
        error = {
            "type": "RiskProvenanceError",
            "message": "risk provenance is invalid",
        }
    else:
        error = {"type": "RunnerError", "message": "market data smoke failed"}
    return {"status": "FAILURE", "error": error}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quantforge-marketdata-smoke",
        description="Run one authenticated MARKETDATA-002 observation.",
    )
    parser.add_argument(
        "--dispatch-timestamp",
        required=True,
        help="Canonical UTC instant, for example 2030-01-01T12:02:00.000000Z",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_smoke(
            dispatch_timestamp=_parse_dispatch_timestamp(args.dispatch_timestamp),
        )
    except (
        FrontierError,
        PriceEvidenceError,
        RiskProvenanceError,
    ) as exc:
        result = _safe_failure(exc)
        print(_json_output(result))
        return 1
    print(_json_output(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
