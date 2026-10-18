from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError
from quantforge.data import BAR_SCHEMA, ParquetDataCatalog

from qf_lab.controller import RoundController, inspect_history
from qf_lab.gateway import (
    FAMILIES,
    SMA,
    FakeProvider,
    GatewayError,
    ModelGateway,
    ResearchDecision,
    canonical_strategy,
    strategy_identity,
)

PARAMS = {
    SMA: {"short_window": 5, "long_window": 15},
    "donchian_breakout": {"entry_window": 15, "exit_window": 5},
    "bollinger_reversion": {"window": 15, "k_pct": "200.0"},
    "time_series_momentum": {"lookback": 15, "threshold_pct": "0.0"},
}


def payload(family: str = SMA, **changes: Any) -> dict[str, Any]:
    return {
        "action": "propose",
        "family": family,
        **PARAMS[family],
        "rationale": "Bounded falsifiable engineering fixture",
        "evidence_refs": [],
        **changes,
    }


@pytest.mark.parametrize("family", FAMILIES)
def test_closed_family_union_and_deterministic_fake(
    family: str, tmp_path: Path
) -> None:
    decision = ResearchDecision.model_validate(payload(family))
    assert decision.parameters == PARAMS[family]
    assert set(decision.model_dump()) == {
        "action",
        "family",
        "rationale",
        "evidence_refs",
        *PARAMS[family],
    }
    gateway = ModelGateway(tmp_path / "calls.sqlite", FakeProvider(), max_calls=3)
    try:
        context = {
            "phase": "first",
            "max_long_window": 20,
            "allowed_families": [family],
            "evidence": [],
        }
        _, first = gateway.call(context)
        _, second = gateway.call(
            {
                **context,
                "phase": "next",
                "evidence": [
                    {"evidence_id": "a" * 64, "strategy": first.strategy},
                ],
            }
        )
        assert first.family == second.family == family
        assert first.parameters != second.parameters
        _, critic = gateway.call(
            {
                **context,
                "phase": "critic",
                "evidence": [
                    {"evidence_id": "a" * 64, "strategy": first.strategy},
                    {"evidence_id": "b" * 64, "strategy": second.strategy},
                ],
            }
        )
        assert critic.strategy == second.strategy and critic.action == "freeze"
        assert [row["phase"] for row in gateway.ledger()] == ["REQUEST", "RESPONSE"] * 3
        with pytest.raises(GatewayError, match="BUDGET"):
            gateway.call(context)
    finally:
        gateway.close()


@pytest.mark.parametrize(
    "family,changes",
    [
        (SMA, {"short_window": True}),
        (SMA, {"long_window": 15.0}),
        (SMA, {"long_window": 253}),
        (SMA, {"entry_window": 15}),
        ("donchian_breakout", {"exit_window": True}),
        ("donchian_breakout", {"entry_window": 5}),
        ("donchian_breakout", {"short_window": 5}),
        ("bollinger_reversion", {"k_pct": 200.0}),
        ("bollinger_reversion", {"k_pct": "1e2"}),
        ("bollinger_reversion", {"k_pct": "0.0"}),
        ("bollinger_reversion", {"k_pct": "1000.0001"}),
        ("bollinger_reversion", {"k_pct": "2.00000"}),
        ("bollinger_reversion", {"k_pct": "0002.0"}),
        ("time_series_momentum", {"threshold_pct": "00.0"}),
        ("bollinger_reversion", {"window": False}),
        ("time_series_momentum", {"threshold_pct": -1.0}),
        ("time_series_momentum", {"threshold_pct": "-1"}),
        ("time_series_momentum", {"threshold_pct": "100.0001"}),
        ("time_series_momentum", {"window": 15}),
        (SMA, {"family": "unsupported"}),
        (SMA, {"sharpe": 99}),
        (SMA, {"FACT": {"screen_pass": True}}),
        (SMA, {"tools": []}),
        (SMA, {"execute": "live"}),
        (SMA, {"long_window": None}),
    ],
)
def test_decisions_reject_cross_family_types_bounds_and_authority(
    family: str,
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ResearchDecision.model_validate({**payload(family), **changes})


def test_legacy_strategy_and_decimal_identity_are_canonical() -> None:
    legacy = {
        "name": SMA,
        **PARAMS[SMA],
        "position_rule": "long_when_short_above_long_else_flat",
    }
    assert canonical_strategy(legacy) == {"family": SMA, "parameters": PARAMS[SMA]}
    boll = {
        "family": "bollinger_reversion",
        "parameters": PARAMS["bollinger_reversion"],
    }
    assert strategy_identity(boll) == strategy_identity(
        {
            "family": "bollinger_reversion",
            "parameters": {"window": 15, "k_pct": "200"},
        }
    )
    assert strategy_identity(legacy) != strategy_identity(
        {
            "family": "donchian_breakout",
            "parameters": PARAMS["donchian_breakout"],
        }
    )
    with pytest.raises(GatewayError, match="STRATEGY"):
        canonical_strategy(
            {**boll, "parameters": {**boll["parameters"], "family": SMA}}
        )


@pytest.mark.parametrize("wrap", ["array", "multiple", "markdown"])
def test_decision_requires_one_json_object(wrap: str) -> None:
    body = json.dumps(payload())
    raw = {
        "array": "[" + body + "]",
        "multiple": body + body,
        "markdown": "```json\n" + body + "\n```",
    }[wrap]
    with pytest.raises(ValidationError):
        ResearchDecision.model_validate_json(raw)


@pytest.mark.parametrize("family", ("donchian_breakout", "time_series_momentum"))
def test_gateway_dynamic_bound_includes_prior_bar_warmup(
    family: str, tmp_path: Path
) -> None:
    class BeyondWarmup(FakeProvider):
        def complete(self, context: dict[str, Any]) -> Any:
            field = "entry_window" if family == "donchian_breakout" else "lookback"
            return ResearchDecision.model_validate(payload(family, **{field: 20})), {}

    gateway = ModelGateway(tmp_path / "calls.sqlite", BeyondWarmup())
    try:
        with pytest.raises(GatewayError, match="ALLOWLIST"):
            gateway.call(
                {
                    "phase": "first",
                    "max_long_window": 20,
                    "allowed_families": [family],
                    "evidence": [],
                }
            )
        assert gateway.ledger()[-1]["phase"] == "ERROR"
    finally:
        gateway.close()


@pytest.fixture
def dataset(tmp_path: Path) -> tuple[Path, str]:
    core = tmp_path / "core"
    core.mkdir()
    start = datetime(2020, 1, 1, tzinfo=UTC)
    prices = [100.0 + 0.2 * i for i in range(100)]
    frame = pl.DataFrame(
        {
            "symbol": ["IBM"] * 100,
            "timestamp": [start + timedelta(days=i) for i in range(100)],
            "open": prices,
            "high": [v + 2 for v in prices],
            "low": [v - 2 for v in prices],
            "close": [v + 1 for v in prices],
            "volume": [1000.0] * 100,
        },
        schema=BAR_SCHEMA,
    )
    snapshot = ParquetDataCatalog(core / "catalog").write(
        "breadth-fixture",
        frame,
        source="synthetic-fixture",
        schema_version="bars-v1",
        as_of=start + timedelta(days=100),
    )
    return core, snapshot.dataset_id


@pytest.mark.parametrize(
    "families",
    [
        ("donchian_breakout",),
        ("bollinger_reversion",),
        ("time_series_momentum",),
        (SMA, "donchian_breakout"),
        ("bollinger_reversion", "time_series_momentum"),
    ],
)
def test_new_family_rounds_reproduce_and_freeze_full_identity(
    dataset: tuple[Path, str],
    tmp_path: Path,
    families: tuple[str, ...],
) -> None:
    core, dataset_id = dataset
    result = RoundController(core, tmp_path / "lab", FakeProvider()).run(
        "Bounded causal family fixture",
        dataset_id=dataset_id,
        program_id="breadth",
        allowed_families=families,
    )
    assert result["rounds_completed"] == 2 and not result["real_calls_proven"]
    assert result["frozen"]["run_id"] == "breadth-r2"
    history = inspect_history(tmp_path / "lab", "breadth")
    proposals = [row for row in history if row["kind"] == "PROPOSAL"]
    assert proposals[0]["family"] == families[0]
    assert proposals[1]["family"] == families[-1]
    assert all(
        row["reproduce"]["result"] == "EXACT_MATCH"
        for row in history
        if row["kind"] == "EVIDENCE_REFERENCE"
    )
    with sqlite3.connect(result["call_ledger"]) as connection:
        requests = [
            json.loads(raw)
            for (raw,) in connection.execute(
                "SELECT payload FROM calls WHERE phase='REQUEST' ORDER BY seq",
            )
        ]
    assert len(requests) == 3
    assert requests[1]["context"]["require_family_change"] is (len(families) > 1)
    assert all(
        item["FACT"]["final_lockbox_scored"] is False
        for item in requests[-1]["context"]["evidence"]
    )


@pytest.mark.parametrize("action", ["freeze", "reject"])
def test_critic_cannot_invent_unrun_family_parameters(
    dataset: tuple[Path, str],
    tmp_path: Path,
    action: str,
) -> None:
    class InventingCritic(FakeProvider):
        def complete(self, context: dict[str, Any]) -> Any:
            decision, metadata = super().complete(context)
            if context["phase"] == "critic":
                decision = ResearchDecision.model_validate(
                    payload(
                        "bollinger_reversion",
                        window=12,
                        action=action,
                        evidence_refs=[
                            item["evidence_id"] for item in context["evidence"]
                        ],
                    )
                )
            return decision, metadata

    core, dataset_id = dataset
    with pytest.raises(GatewayError, match="UNRUN"):
        RoundController(core, tmp_path / "lab", InventingCritic()).run(
            "Bounded critic fixture",
            dataset_id=dataset_id,
            program_id="invent",
            allowed_families=("bollinger_reversion", "time_series_momentum"),
        )
    assert inspect_history(tmp_path / "lab", "invent")[-1]["kind"] == "FAILED"


def test_multiple_allowed_families_require_actual_family_change(
    dataset: tuple[Path, str],
    tmp_path: Path,
) -> None:
    class SameFamily(FakeProvider):
        def complete(self, context: dict[str, Any]) -> Any:
            return super().complete({**context, "allowed_families": [SMA]})

    core, dataset_id = dataset
    with pytest.raises(GatewayError, match="CHANGE_FAMILY"):
        RoundController(core, tmp_path / "lab", SameFamily()).run(
            "Bounded family change fixture",
            dataset_id=dataset_id,
            program_id="same",
            allowed_families=(SMA, "donchian_breakout"),
        )


def test_independent_critic_uses_third_shared_call_and_existing_cross_family_candidate(
    dataset: tuple[Path, str],
    tmp_path: Path,
) -> None:
    class Critic(FakeProvider):
        name = "independent-critic-fixture"
        model = "critic-fixture-v1"

        def complete(self, context: dict[str, Any]) -> Any:
            assert context["phase"] == "critic" and len(context["evidence"]) == 2
            return super().complete(context)

    core, dataset_id = dataset
    result = RoundController(
        core, tmp_path / "lab", FakeProvider(), critic_provider=Critic()
    ).run(
        "Independent critic engineering fixture",
        dataset_id=dataset_id,
        program_id="critic",
        allowed_families=("donchian_breakout", "bollinger_reversion"),
    )
    assert result["critic_independent"] is True and result["real_calls_proven"] is False
    assert result["critic_provider"] == "independent-critic-fixture"
    assert result["frozen"]["run_id"] == "critic-r2"
    with sqlite3.connect(result["call_ledger"]) as connection:
        requests = [
            json.loads(raw)
            for (raw,) in connection.execute(
                "SELECT payload FROM calls WHERE phase='REQUEST' ORDER BY seq",
            )
        ]
    assert len(requests) == 3
    assert [row["provider"] for row in requests] == [
        "deterministic-fake",
        "deterministic-fake",
        "independent-critic-fixture",
    ]
