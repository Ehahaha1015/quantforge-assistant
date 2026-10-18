from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from _fixtures import (
    EVALUATED_AT,
    LAST_FILL_AT,
    make_artifact,
    make_envelope,
    make_limits,
    make_order,
    make_portfolio,
)

from quantforge.domain import Position
from quantforge.portfolio import PortfolioState
from quantforge.predispatch import (
    PreDispatchDecision,
    PreDispatchReason,
    evaluate_pre_dispatch,
)
from quantforge.provenance import FreshRiskProvenance, RiskProvenanceError
from quantforge.risk import RiskDecision, RiskLimits, RiskReason


def _direct_values(artifact: FreshRiskProvenance) -> dict[str, Any]:
    return {
        "schema": artifact.schema,
        "provenance_id": artifact.provenance_id,
        "price_set_commitment": artifact.price_set_commitment,
        "portfolio_commitment": artifact.portfolio_commitment,
        "pre_dispatch_decision": artifact.pre_dispatch_decision,
        "portfolio": artifact.portfolio,
        "risk_prices": artifact.risk_prices,
        "estimated_fee": artifact.estimated_fee,
        "limits": artifact.limits,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strategy_id", "beta"),
        ("symbol", "MSFT"),
        ("timestamp", EVALUATED_AT - timedelta(minutes=1, seconds=59)),
        ("side", "SELL"),
        ("quantity", Decimal(6)),
    ],
)
def test_oracle_d_each_order_field_mutation_changes_identity(
    field: str, value: Any
) -> None:
    baseline = make_artifact()
    order = make_order(**{field: value})
    prices = (
        {"MSFT": Decimal(50)}
        if field == "symbol"
        else {"MSFT": Decimal(50), "AAPL": Decimal(100)}
    )
    mutated = make_artifact(order=order, prices=prices)

    assert order != baseline.pre_dispatch_decision.envelope.order
    assert mutated.provenance_id != baseline.provenance_id


@pytest.mark.parametrize(
    "portfolio",
    [
        make_portfolio(initial_cash=Decimal(1001)),
        make_portfolio(cash=Decimal(1001)),
        make_portfolio(
            positions=(
                Position(
                    symbol="MSFT", quantity=Decimal(3), average_price=Decimal(50)
                ),
            )
        ),
        make_portfolio(
            positions=(
                Position(
                    symbol="MSFT", quantity=Decimal(2), average_price=Decimal(51)
                ),
            )
        ),
        make_portfolio(cumulative_fees=Decimal(1)),
        make_portfolio(last_fill_timestamp=LAST_FILL_AT - timedelta(minutes=1)),
    ],
)
def test_oracle_e_each_portfolio_field_changes_both_commitments(
    portfolio: PortfolioState,
) -> None:
    baseline = make_artifact()
    mutated = make_artifact(portfolio=portfolio)

    assert portfolio != baseline.portfolio
    assert mutated.portfolio_commitment != baseline.portfolio_commitment
    assert mutated.provenance_id != baseline.provenance_id


def test_oracle_f_price_mutation_and_mapping_order() -> None:
    baseline = make_artifact()
    reordered = make_artifact(
        prices={"AAPL": Decimal(100), "MSFT": Decimal(50)}
    )
    mutated = make_artifact(
        prices={"MSFT": Decimal(50), "AAPL": Decimal("100.0001")}
    )

    assert reordered == baseline
    assert mutated.price_set_commitment != baseline.price_set_commitment
    assert mutated.provenance_id != baseline.provenance_id


def test_oracle_g_fee_mutation_changes_identity_and_replays() -> None:
    baseline = make_artifact()
    mutated = make_artifact(fee=Decimal("1.01"))
    assert mutated.provenance_id != baseline.provenance_id
    assert mutated.pre_dispatch_decision.risk_decision.approved is True


@pytest.mark.parametrize(
    "limits",
    [
        make_limits(max_order_notional=Decimal(1001)),
        make_limits(max_position_notional=Decimal(1001)),
        make_limits(max_gross_exposure=Decimal(2001)),
    ],
)
def test_oracle_h_each_limit_changes_identity(limits: RiskLimits) -> None:
    baseline = make_artifact()
    mutated = make_artifact(limits=limits)
    assert limits != baseline.limits
    assert mutated.provenance_id != baseline.provenance_id


def test_oracle_i_foreign_decision_rejected_by_direct_replay() -> None:
    artifact = make_artifact()
    foreign_risk = RiskDecision(
        approved=False,
        reasons=(RiskReason.ORDER_NOTIONAL_LIMIT,),
        detail="",
    )
    foreign_pre_dispatch = PreDispatchDecision(
        envelope=artifact.pre_dispatch_decision.envelope,
        history=artifact.pre_dispatch_decision.history,
        policy=artifact.pre_dispatch_decision.policy,
        evaluation_timestamp=artifact.pre_dispatch_decision.evaluation_timestamp,
        risk_decision=foreign_risk,
        eligible=False,
        reasons=(PreDispatchReason.FRESH_RISK_REJECTED,),
    )
    values = _direct_values(artifact)
    values["pre_dispatch_decision"] = foreign_pre_dispatch

    with pytest.raises(RiskProvenanceError, match="frozen risk replay"):
        FreshRiskProvenance(**values)


def test_oracle_j_foreign_valid_envelope_rejected() -> None:
    artifact = make_artifact()
    foreign_envelope = make_envelope(authorized_by="operator-8")
    foreign_decision = evaluate_pre_dispatch(
        foreign_envelope,
        artifact.pre_dispatch_decision.history,
        artifact.pre_dispatch_decision.policy,
        artifact.pre_dispatch_decision.evaluation_timestamp,
        artifact.portfolio,
        {fact.symbol: fact.price for fact in artifact.risk_prices},
        artifact.estimated_fee,
        artifact.limits,
    )
    values = _direct_values(artifact)
    values["pre_dispatch_decision"] = foreign_decision

    assert foreign_envelope != artifact.pre_dispatch_decision.envelope
    with pytest.raises(RiskProvenanceError):
        FreshRiskProvenance(**values)


def test_oracles_k_and_l_exact_future_reference_vocabulary() -> None:
    baseline = make_artifact()
    changed_price = make_artifact(
        prices={"MSFT": Decimal(50), "AAPL": Decimal("100.0001")}
    )
    changed_portfolio = make_artifact(portfolio=make_portfolio(cash=Decimal(999)))

    future_price_reference = baseline.price_set_commitment
    future_portfolio_reference = baseline.portfolio_commitment
    assert future_price_reference == baseline.price_set_commitment
    assert future_price_reference != changed_price.price_set_commitment
    assert future_portfolio_reference == baseline.portfolio_commitment
    assert future_portfolio_reference != changed_portfolio.portfolio_commitment


def _rehash_payload(document: dict[str, Any]) -> None:
    canonical = json.dumps(
        document["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    document["provenance_id"] = "riskprov1-" + hashlib.sha256(canonical).hexdigest()


def test_oracle_n_coordinated_decision_commitment_and_identity_tamper_fails() -> None:
    document = json.loads(make_artifact().model_dump_json())
    document["payload"]["pre_dispatch"]["risk_decision"] = {
        "approved": False,
        "detail": "",
        "reasons": ["ORDER_NOTIONAL_LIMIT"],
    }
    document["payload"]["pre_dispatch"]["eligible"] = False
    document["payload"]["pre_dispatch"]["reasons"] = ["FRESH_RISK_REJECTED"]
    _rehash_payload(document)

    with pytest.raises(RiskProvenanceError, match="frozen risk replay"):
        FreshRiskProvenance.from_json(json.dumps(document))


def test_oracle_n_coordinated_fact_commitment_and_identity_tamper_fails() -> None:
    document = json.loads(make_artifact().model_dump_json())
    document["payload"]["risk_prices"][0]["price"] = {
        "coefficient": "3",
        "exponent": 2,
        "sign": 0,
    }
    price_payload = {
        "risk_prices": document["payload"]["risk_prices"],
        "schema": "riskprice1",
    }
    price_bytes = json.dumps(
        price_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    document["payload"]["price_set_commitment"] = (
        "riskprice1-" + hashlib.sha256(price_bytes).hexdigest()
    )
    _rehash_payload(document)

    with pytest.raises(RiskProvenanceError, match="frozen risk replay"):
        FreshRiskProvenance.from_json(json.dumps(document))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["payload"].update({"schema": "riskprov2"}),
        lambda doc: doc["payload"].update({"extra": 1}),
        lambda doc: doc["payload"].pop("estimated_fee"),
        lambda doc: doc.update({"provenance_id": "riskprov1-" + "0" * 64}),
        lambda doc: doc["payload"].update(
            {"price_set_commitment": "riskprice1-" + "0" * 64}
        ),
        lambda doc: doc["payload"].update(
            {"portfolio_commitment": "riskportfolio1-" + "0" * 64}
        ),
        lambda doc: doc["payload"].update(
            {"evaluation_timestamp": "2030-01-01T12:02:00+00:00"}
        ),
        lambda doc: doc["payload"]["risk_prices"].reverse(),
        lambda doc: doc["payload"]["limits"]["max_order_notional"].update(
            {"coefficient": "010"}
        ),
        lambda doc: doc["payload"]["pre_dispatch"].update(
            {"reasons": ["UNKNOWN"]}
        ),
        lambda doc: doc["payload"]["envelope"].update(
            {"submission_id": "broker1-" + "0" * 32}
        ),
    ],
)
def test_oracle_n_serialized_tampering_fails_closed(mutate: Any) -> None:
    document = copy.deepcopy(json.loads(make_artifact().model_dump_json()))
    mutate(document)
    with pytest.raises(RiskProvenanceError):
        FreshRiskProvenance.from_json(json.dumps(document))
