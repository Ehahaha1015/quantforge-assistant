from __future__ import annotations

import inspect
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from decimal import (
    ROUND_DOWN,
    Clamped,
    Decimal,
    Inexact,
    Overflow,
    Rounded,
    Underflow,
    getcontext,
    localcontext,
)
from pathlib import Path
from typing import Any

import pytest
from _fixtures import (
    EVALUATED_AT,
    make_artifact,
    make_envelope,
    make_limits,
    make_portfolio,
)
from pydantic import ValidationError

import quantforge.predispatch as predispatch_api
import quantforge.provenance as provenance_api
import quantforge.risk as risk_api
from quantforge.predispatch import (
    AuthorizationFreshnessPolicy,
    SubmissionHistoryState,
    evaluate_pre_dispatch,
)
from quantforge.provenance import (
    FreshRiskProvenance,
    RiskPriceFact,
    RiskProvenanceError,
    evaluate_pre_dispatch_with_provenance,
)
from quantforge.risk import RiskError


class CountingMapping(Mapping[str, Decimal]):
    def __init__(self, pairs: list[tuple[str, Decimal]]) -> None:
        self._pairs = pairs
        self.iterations = 0

    def __getitem__(self, key: str) -> Decimal:
        for symbol, value in self._pairs:
            if symbol == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        self.iterations += 1
        return iter(symbol for symbol, _value in self._pairs)

    def __len__(self) -> int:
        return len(self._pairs)


class DuplicateItemsMapping(CountingMapping):
    def items(self) -> Any:
        self.iterations += 1
        return iter(self._pairs)


def _compose_with_prices(prices: Mapping[str, Any]) -> FreshRiskProvenance:
    envelope = make_envelope()
    return evaluate_pre_dispatch_with_provenance(
        envelope,
        SubmissionHistoryState(
            submission_id=envelope.submission_id, state="NEVER_DISPATCHED"
        ),
        AuthorizationFreshnessPolicy(max_authorization_age=timedelta(minutes=5)),
        EVALUATED_AT,
        make_portfolio(),
        prices,
        Decimal(1),
        make_limits(),
    )


def test_oracle_r_one_traversal_and_exact_snapshot_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_risk = risk_api.evaluate_order
    original_pre_dispatch = predispatch_api.evaluate_pre_dispatch
    risk_calls: list[tuple[Any, ...]] = []
    pre_dispatch_calls: list[tuple[Any, ...]] = []
    risk_results: list[Any] = []
    pre_dispatch_results: list[Any] = []

    def traced_risk(*args: Any, **kwargs: Any) -> Any:
        assert not kwargs
        risk_calls.append(args)
        result = original_risk(*args)
        risk_results.append(result)
        return result

    def traced_pre_dispatch(*args: Any, **kwargs: Any) -> Any:
        assert not kwargs
        pre_dispatch_calls.append(args)
        result = original_pre_dispatch(*args)
        pre_dispatch_results.append(result)
        return result

    monkeypatch.setattr(risk_api, "evaluate_order", traced_risk)
    monkeypatch.setattr(
        predispatch_api, "evaluate_pre_dispatch", traced_pre_dispatch
    )
    prices = CountingMapping(
        [("MSFT", Decimal(50)), ("AAPL", Decimal(100))]
    )
    artifact = _compose_with_prices(prices)

    assert prices.iterations == 1
    assert len(risk_calls) == 2
    assert len(pre_dispatch_calls) == 2
    for risk_args, pre_dispatch_args in zip(risk_calls, pre_dispatch_calls, strict=True):
        assert risk_args[0] == pre_dispatch_args[0].order
        assert risk_args[1] is pre_dispatch_args[4]
        assert risk_args[2] is pre_dispatch_args[5]
        assert risk_args[3] is pre_dispatch_args[6]
        assert risk_args[4] is pre_dispatch_args[7]
    assert all(
        risk_result == pre_dispatch_result.risk_decision
        for risk_result, pre_dispatch_result in zip(
            risk_results, pre_dispatch_results, strict=True
        )
    )
    assert artifact.pre_dispatch_decision == pre_dispatch_results[0]


def test_oracle_r_stored_result_equals_independent_frozen_replay() -> None:
    artifact = make_artifact()
    prices = {fact.symbol: fact.price for fact in artifact.risk_prices}
    direct = risk_api.evaluate_order(
        artifact.pre_dispatch_decision.envelope.order,
        artifact.portfolio,
        prices,
        artifact.estimated_fee,
        artifact.limits,
    )
    replayed = evaluate_pre_dispatch(
        artifact.pre_dispatch_decision.envelope,
        artifact.pre_dispatch_decision.history,
        artifact.pre_dispatch_decision.policy,
        artifact.pre_dispatch_decision.evaluation_timestamp,
        artifact.portfolio,
        prices,
        artifact.estimated_fee,
        artifact.limits,
    )
    assert direct == artifact.pre_dispatch_decision.risk_decision
    assert replayed == artifact.pre_dispatch_decision


@pytest.mark.parametrize(
    "prices",
    [
        {"AAPL": Decimal(100)},
        {"AAPL": Decimal(100), "MSFT": Decimal(50), "TSLA": Decimal(1)},
        {"AAPL": Decimal(0), "MSFT": Decimal(50)},
        {"AAPL": Decimal(-1), "MSFT": Decimal(50)},
        {"AAPL": Decimal("NaN"), "MSFT": Decimal(50)},
        {"AAPL": True, "MSFT": Decimal(50)},
        {"AAPL": 100.0, "MSFT": Decimal(50)},
        {"": Decimal(100), "MSFT": Decimal(50)},
    ],
)
def test_invalid_missing_extra_and_nonpositive_prices_fail_closed(
    prices: Mapping[str, Any],
) -> None:
    with pytest.raises(RiskProvenanceError):
        _compose_with_prices(prices)


def test_duplicate_yielded_price_key_fails_closed() -> None:
    prices = DuplicateItemsMapping(
        [
            ("AAPL", Decimal(100)),
            ("AAPL", Decimal(101)),
            ("MSFT", Decimal(50)),
        ]
    )
    with pytest.raises(RiskProvenanceError, match="duplicate symbol"):
        _compose_with_prices(prices)
    assert prices.iterations == 1


def test_symbol_without_utf8_representation_fails_at_public_boundary() -> None:
    with pytest.raises(RiskProvenanceError, match="UTF-8"):
        _compose_with_prices(
            {"AAPL": Decimal(100), "MSFT": Decimal(50), "\ud800": Decimal(1)}
        )


@pytest.mark.parametrize(
    "fee", [True, 1.0, Decimal("NaN"), Decimal("Infinity"), Decimal(-1)]
)
def test_invalid_fee_fails_closed(fee: Any) -> None:
    artifact = make_artifact()
    with pytest.raises(RiskProvenanceError):
        evaluate_pre_dispatch_with_provenance(
            artifact.pre_dispatch_decision.envelope,
            artifact.pre_dispatch_decision.history,
            artifact.pre_dispatch_decision.policy,
            EVALUATED_AT,
            artifact.portfolio,
            {fact.symbol: fact.price for fact in artifact.risk_prices},
            fee,
            artifact.limits,
        )


@pytest.mark.parametrize(
    ("position", "value"),
    [
        (0, None),
        (1, None),
        (2, None),
        (4, None),
        (5, []),
        (7, None),
    ],
)
def test_wrong_top_level_composition_types_raise_type_error(
    position: int, value: Any
) -> None:
    artifact = make_artifact()
    args: list[Any] = [
        artifact.pre_dispatch_decision.envelope,
        artifact.pre_dispatch_decision.history,
        artifact.pre_dispatch_decision.policy,
        EVALUATED_AT,
        artifact.portfolio,
        {fact.symbol: fact.price for fact in artifact.risk_prices},
        artifact.estimated_fee,
        artifact.limits,
    ]
    args[position] = value
    with pytest.raises(TypeError):
        evaluate_pre_dispatch_with_provenance(*args)


def test_naive_evaluation_timestamp_is_provenance_error() -> None:
    artifact = make_artifact()
    with pytest.raises(RiskProvenanceError):
        evaluate_pre_dispatch_with_provenance(
            artifact.pre_dispatch_decision.envelope,
            artifact.pre_dispatch_decision.history,
            artifact.pre_dispatch_decision.policy,
            datetime(2030, 1, 1),  # noqa: DTZ001 - deliberately invalid input
            artifact.portfolio,
            {fact.symbol: fact.price for fact in artifact.risk_prices},
            artifact.estimated_fee,
            artifact.limits,
        )


def _context_signature() -> tuple[Any, ...]:
    context = getcontext()
    return (
        context.prec,
        context.rounding,
        context.Emax,
        context.Emin,
        context.capitals,
        context.clamp,
        tuple((signal.__name__, state) for signal, state in context.traps.items()),
        tuple((signal.__name__, state) for signal, state in context.flags.items()),
    )


def test_oracle_o_hostile_decimal_context_is_unchanged_on_success_and_failure() -> None:
    baseline = make_artifact()
    hostile = getcontext().copy()
    hostile.prec = 2
    hostile.rounding = ROUND_DOWN
    hostile.Emax = 9
    hostile.Emin = -9
    for signal in (Clamped, Inexact, Overflow, Rounded, Underflow):
        hostile.traps[signal] = True
    with localcontext(hostile):
        before = _context_signature()
        assert make_artifact() == baseline
        assert _context_signature() == before
        with pytest.raises(RiskProvenanceError):
            _compose_with_prices(
                {"AAPL": Decimal("NaN"), "MSFT": Decimal(50)}
            )
        assert _context_signature() == before


def test_error_cause_from_frozen_risk_is_preserved() -> None:
    with pytest.raises(RiskProvenanceError) as raised:
        _compose_with_prices({"AAPL": Decimal(100)})
    assert isinstance(raised.value.__cause__, RiskError)


def test_oracle_p_inputs_are_immutable_and_production_has_no_capabilities() -> None:
    artifact = make_artifact()
    envelope_before = artifact.pre_dispatch_decision.envelope.model_copy(deep=True)
    history_before = artifact.pre_dispatch_decision.history.model_copy(deep=True)
    policy_before = artifact.pre_dispatch_decision.policy.model_copy(deep=True)
    portfolio_before = artifact.portfolio.model_copy(deep=True)
    limits_before = artifact.limits.model_copy(deep=True)
    prices = {"MSFT": Decimal(50), "AAPL": Decimal(100)}
    prices_before = prices.copy()
    evaluate_pre_dispatch_with_provenance(
        artifact.pre_dispatch_decision.envelope,
        artifact.pre_dispatch_decision.history,
        artifact.pre_dispatch_decision.policy,
        EVALUATED_AT,
        artifact.portfolio,
        prices,
        artifact.estimated_fee,
        artifact.limits,
    )
    assert artifact.pre_dispatch_decision.envelope == envelope_before
    assert artifact.pre_dispatch_decision.history == history_before
    assert artifact.pre_dispatch_decision.policy == policy_before
    assert artifact.portfolio == portfolio_before
    assert artifact.limits == limits_before
    assert prices == prices_before

    source_root = Path(inspect.getfile(FreshRiskProvenance)).parent
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.py"))
    forbidden = [
        "datetime.now(",
        "datetime.utcnow(",
        "time.time(",
        "os.environ",
        "subprocess",
        "requests",
        "httpx",
        "aiohttp",
        "websocket",
        "socket.",
        "uuid",
        "random.",
        "open(",
        "Fill(",
        "apply_fill",
        "execute_paper_order",
        "place_order",
        "submit_order",
        "cancel_order",
        "replace_order",
    ]
    assert all(token not in source for token in forbidden)


def test_oracle_q_public_surface_and_no_external_truth_claims() -> None:
    assert provenance_api.__all__ == [
        "FreshRiskProvenance",
        "RiskPriceFact",
        "RiskProvenanceError",
        "evaluate_pre_dispatch_with_provenance",
    ]
    assert tuple(inspect.signature(evaluate_pre_dispatch_with_provenance).parameters) == (
        "envelope",
        "history",
        "policy",
        "evaluation_timestamp",
        "portfolio",
        "risk_prices",
        "estimated_fee",
        "limits",
    )
    artifact = make_artifact()
    forbidden_claims = (
        "authentic",
        "durable",
        "reconciled",
        "submitted",
        "accepted",
        "safe_to_retry",
    )
    names = {name.lower() for name in dir(artifact)}
    assert all(not any(claim in name for claim in forbidden_claims) for name in names)
    assert not hasattr(artifact, "price_is_fresh")
    assert not hasattr(artifact, "account_is_fresh")


def test_risk_price_fact_is_frozen_strict_and_extra_forbidden() -> None:
    fact = RiskPriceFact(symbol="AAPL", price="100")
    assert fact.price == Decimal(100)
    with pytest.raises(ValidationError):
        fact.price = Decimal(101)  # type: ignore[misc]
    with pytest.raises(RiskProvenanceError):
        RiskPriceFact(symbol="AAPL", price=100.0)
    with pytest.raises(RiskProvenanceError):
        RiskPriceFact(symbol="", price=Decimal(100))
    with pytest.raises(RiskProvenanceError):
        RiskPriceFact(symbol="AAPL", price=Decimal(100), extra=True)
