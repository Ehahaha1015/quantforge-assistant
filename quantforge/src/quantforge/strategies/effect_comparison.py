"""Observed replay effects, separate from noninferiority or release permission."""

from __future__ import annotations

from decimal import Context, Decimal, localcontext
from typing import Any


def compare_effect(parent: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    keys = ("price_return_after_assumed_costs", "max_drawdown")
    values = [[Decimal(str(row[key])) for key in keys] for row in (parent, candidate)]
    if any(not v.is_finite() for pair in values for v in pair):
        raise ValueError("finite economic metrics required")
    if parent["friction_bps_each_side"] != candidate["friction_bps_each_side"]:
        raise ValueError("paired effects require equal costs")
    with localcontext(Context(prec=50)):
        gain, risk = [values[1][i] - values[0][i] for i in range(2)]
    trades = ("entry_at", "exit_at", "quantity", "entry_price", "exit_price", "fees")
    left = [tuple(row[key] for key in trades) for row in parent["trades"]]
    right = [tuple(row[key] for key in trades) for row in candidate["trades"]]
    same = all(
        parent[key] == candidate[key]
        for key in (*keys, "equity_curve", "total_fees", "open_quantity")
    )
    effect = (
        "UNCHANGED"
        if same
        else "IMPROVED"
        if gain >= 0 and risk >= 0 and (gain > 0 or risk > 0)
        else "WORSE"
        if gain <= 0 and risk <= 0 and (gain < 0 or risk < 0)
        else "TRADEOFF"
    )
    enough = min(parent["closed_trades"], candidate["closed_trades"]) >= 20
    return {
        "schema_version": "replay-effect-v1",
        "effect": effect,
        "sample_status": "SUFFICIENT_FOR_DECLARED_COUNT" if enough else "INSUFFICIENT",
        "return_delta": str(gain),
        "drawdown_delta": str(risk),
        "economic_trade_changes": sum(a != b for a, b in zip(left, right))
        + abs(len(left) - len(right)),
        "noninferior_observed": gain >= 0 and risk >= 0,
        "interpretation": "通过非劣检查，但未观察到经济改善。"
        if same
        else "仅为所观察回放的变化，需另行独立验收。",
        "forecast_validated": False,
        "automatic_promotion": False,
    }
