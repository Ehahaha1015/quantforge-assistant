from __future__ import annotations

from decimal import Decimal

import pytest

from quantforge.product.realism import cash_lot_stress


def test_fixed_lot_fees_volume_and_cash_conservation_are_reproducible() -> None:
    rows = [
        {
            "timestamp": "2020-01-01T00:00:00+00:00",
            "open": 10,
            "close": 10,
            "volume": 1000000,
            "target": 1.0,
        },
        {
            "timestamp": "2020-01-02T00:00:00+00:00",
            "open": 11,
            "close": 11,
            "volume": 1000000,
            "target": 0.0,
        },
    ]
    first = cash_lot_stress(rows, evidence_id="fixture-evidence")
    assert first == cash_lot_stress(rows, evidence_id="fixture-evidence")
    base, lot, expensive = first["scenarios"]
    assert base["trade_count"] == 2
    assert Decimal(base["total_fees"]) > 0
    assert Decimal(expensive["final_equity"]) < Decimal(lot["final_equity"])
    for scenario in first["scenarios"]:
        assert all(
            Decimal(p["cash"]) >= 0
            and Decimal(p["quantity"]) % scenario["lot_size"] == 0
            for p in scenario["periods"]
        )
        assert scenario["periods"][-1]["quantity"] == "0"
    constrained = cash_lot_stress(
        [{**v, "volume": 0} for v in rows], evidence_id="fixture"
    )
    assert all(
        v["final_equity"] == "100000" and v["trade_count"] == 0
        for v in constrained["scenarios"]
    )
    assert first["validated_alpha"] is False
    assert first["live_eligible"] is False


def test_no_fractional_lot_or_fabricated_fill_when_cash_or_volume_insufficient() -> (
    None
):
    rows = [
        {
            "timestamp": "2020-01-01T00:00:00+00:00",
            "open": 2000,
            "close": 2000,
            "volume": 1000,
            "target": 1.0,
        }
    ]
    result = cash_lot_stress(rows, evidence_id="fixture")
    assert result["scenarios"][1]["trade_count"] == 0
    assert result["scenarios"][1]["unfilled_signal_count"] == 1
    with pytest.raises(ValueError, match="chronological"):
        cash_lot_stress(rows + rows, evidence_id="fixture")
    with pytest.raises(ValueError, match="market facts"):
        cash_lot_stress([{**rows[0], "open": float("nan")}], evidence_id="fixture")
