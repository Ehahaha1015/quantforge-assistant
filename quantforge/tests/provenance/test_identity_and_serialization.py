from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from _fixtures import (
    LIVE_FINGERPRINT,
    PINNED_PAYLOAD,
    PORTFOLIO_COMMITMENT,
    PRICE_COMMITMENT,
    PROVENANCE_ID,
    SUBMISSION_ID,
    equivalent_offset_artifact,
    make_artifact,
)

from quantforge.provenance import FreshRiskProvenance, RiskProvenanceError


def test_oracle_a_exact_pinned_fixture_and_independent_bytes() -> None:
    first = make_artifact()
    second = make_artifact()
    outer = json.loads(first.model_dump_json())
    actual_payload = json.dumps(
        outer["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert first.pre_dispatch_decision.envelope.permission.order_fingerprint == (
        LIVE_FINGERPRINT
    )
    assert first.pre_dispatch_decision.envelope.submission_id == SUBMISSION_ID
    assert first.price_set_commitment == PRICE_COMMITMENT
    assert first.portfolio_commitment == PORTFOLIO_COMMITMENT
    assert first.provenance_id == PROVENANCE_ID
    assert actual_payload == PINNED_PAYLOAD
    assert len(PINNED_PAYLOAD.encode("utf-8")) == 2033
    assert "riskprov1-" + hashlib.sha256(
        PINNED_PAYLOAD.encode("utf-8")
    ).hexdigest() == PROVENANCE_ID


@pytest.mark.parametrize("seed", ["random", "0", "12345"])
def test_oracle_b_fresh_process_hash_seed_stability(seed: str) -> None:
    script = Path(__file__).with_name("hash_seed_probe.py")
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = seed
    completed = subprocess.run(
        [sys.executable, str(script)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
    )
    result = json.loads(completed.stdout.strip())
    assert result == {
        "payload_sha256": PROVENANCE_ID.removeprefix("riskprov1-"),
        "portfolio": PORTFOLIO_COMMITMENT,
        "price": PRICE_COMMITMENT,
        "provenance": PROVENANCE_ID,
    }


def test_oracle_c_decimal_equivalence_and_literal_tokens() -> None:
    baseline = make_artifact()
    equivalent = make_artifact(
        prices={"AAPL": Decimal("1E+2"), "MSFT": Decimal("5E+1")},
        fee=Decimal("10E-1"),
    )
    payload = json.loads(baseline.model_dump_json())["payload"]

    assert equivalent == baseline
    assert payload["envelope"]["order"]["quantity"] == {
        "coefficient": "5",
        "exponent": 0,
        "sign": 0,
    }
    assert payload["risk_prices"][0]["price"] == {
        "coefficient": "1",
        "exponent": 2,
        "sign": 0,
    }
    assert payload["portfolio"]["cash"] == {
        "coefficient": "1",
        "exponent": 3,
        "sign": 0,
    }
    assert payload["limits"]["max_gross_exposure"] == {
        "coefficient": "2",
        "exponent": 3,
        "sign": 0,
    }


def test_oracle_m_round_trip_and_timezone_equivalence() -> None:
    artifact = make_artifact()
    serialized = artifact.model_dump_json()
    restored = FreshRiskProvenance.from_json(serialized)

    assert restored == artifact
    assert restored.model_dump_json() == serialized
    assert equivalent_offset_artifact() == artifact
    assert equivalent_offset_artifact().model_dump_json() == serialized
    assert serialized.startswith('{"payload":')
    assert not serialized.startswith("\ufeff")
    assert not serialized.endswith("\n")


def test_prov_001_extreme_decimal_exponent_uses_public_error_boundary() -> None:
    document = json.loads(make_artifact().model_dump_json())
    document["payload"]["estimated_fee"] = {
        "coefficient": "1",
        "exponent": 2**63,
        "sign": 0,
    }

    with pytest.raises(RiskProvenanceError) as error:
        FreshRiskProvenance.from_json(json.dumps(document))

    assert isinstance(error.value.__cause__, OverflowError)


@pytest.mark.parametrize(
    "path",
    [
        ("risk_prices", 0, "price"),
        ("portfolio", "cash"),
        ("limits", "max_order_notional"),
        ("envelope", "order", "quantity"),
    ],
)
def test_prov_001_sibling_decimal_fields_use_public_error_boundary(
    path: tuple[str | int, ...],
) -> None:
    document = json.loads(make_artifact().model_dump_json())
    container: Any = document["payload"]
    for part in path[:-1]:
        container = container[part]
    container[path[-1]] = {
        "coefficient": "1",
        "exponent": 2**63,
        "sign": 0,
    }

    with pytest.raises(RiskProvenanceError):
        FreshRiskProvenance.from_json(json.dumps(document))


@pytest.mark.parametrize("exponent", [-(2**63), -(2**63) - 1])
def test_prov_001_negative_extreme_exponents_use_public_error_boundary(
    exponent: int,
) -> None:
    document = json.loads(make_artifact().model_dump_json())
    document["payload"]["estimated_fee"] = {
        "coefficient": "1",
        "exponent": exponent,
        "sign": 0,
    }

    with pytest.raises(RiskProvenanceError):
        FreshRiskProvenance.from_json(json.dumps(document))


def test_prov_001_fix_preserves_valid_canonical_round_trip() -> None:
    artifact = make_artifact()
    serialized = artifact.model_dump_json()
    restored = FreshRiskProvenance.from_json(serialized)

    assert restored == artifact
    assert restored.model_dump_json() == serialized
    assert restored.provenance_id == PROVENANCE_ID
    assert restored.price_set_commitment == PRICE_COMMITMENT
    assert restored.portfolio_commitment == PORTFOLIO_COMMITMENT


def test_serialization_rejects_options_that_could_change_canonical_bytes() -> None:
    artifact = make_artifact()
    with pytest.raises(TypeError):
        artifact.model_dump_json(indent=2)


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        "null",
        "{}",
        '{"payload":{},"provenance_id":"x","extra":1}',
        '{"payload":{},"payload":{},"provenance_id":"x"}',
    ],
)
def test_oracle_n_malformed_outer_json_fails_closed(payload: str) -> None:
    with pytest.raises(RiskProvenanceError):
        FreshRiskProvenance.from_json(payload)
