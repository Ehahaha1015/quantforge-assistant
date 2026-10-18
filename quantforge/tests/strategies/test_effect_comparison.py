from copy import deepcopy
from decimal import localcontext

import pytest

from quantforge.strategies.effect_comparison import compare_effect


def result():
    return {
        "price_return_after_assumed_costs": "0.0123456",
        "max_drawdown": "-0.04",
        "friction_bps_each_side": 30,
        "closed_trades": 20,
        "trades": [],
        "equity_curve": [{"equity": "100"}],
        "total_fees": "1",
        "open_quantity": 0,
    }


def test_noninferiority_is_not_an_observed_improvement():
    parent = result()
    unchanged = compare_effect(parent, deepcopy(parent))
    assert unchanged["noninferior_observed"] and unchanged["effect"] == "UNCHANGED"
    assert (
        unchanged["economic_trade_changes"] == 0
        and not unchanged["automatic_promotion"]
    )
    candidate = {**parent, "price_return_after_assumed_costs": "0.0123457"}
    with localcontext() as context:
        context.prec = 3
        improved = compare_effect(parent, candidate)
    assert improved["effect"] == "IMPROVED" and improved["return_delta"] == "1E-7"
    worse = {**parent, "price_return_after_assumed_costs": "0", "closed_trades": 1}
    assert compare_effect(parent, worse)["effect"] == "WORSE"
    assert compare_effect(parent, worse)["sample_status"] == "INSUFFICIENT"
    tradeoff = {
        **parent,
        "price_return_after_assumed_costs": "0.1",
        "max_drawdown": "-0.06",
    }
    assert compare_effect(parent, tradeoff)["effect"] == "TRADEOFF"
    with pytest.raises(ValueError):
        compare_effect(parent, {**parent, "friction_bps_each_side": 10})
