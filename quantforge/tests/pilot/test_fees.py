from __future__ import annotations

from decimal import Decimal

import pytest

from quantforge.product.pilot.fees import (
    CN_FEE_PROFILES,
    CnFeeProfile,
    CnTradability,
    PilotRuleError,
    affordable_lots,
    buy_fillable,
    calculate_fees,
    limit_prices,
    sell_fillable,
)

STOCK = CN_FEE_PROFILES["stock-default"]
ETF = CN_FEE_PROFILES["etf-no-minimum"]
RULES = CnTradability()


def test_minimum_commission_applies_to_small_tickets_and_rate_above_it() -> None:
    small = calculate_fees("BUY", Decimal(100), Decimal("2.86"), STOCK)
    assert small["commission"] == Decimal("5.00")  # 0.29 < minimum, so minimum binds
    large = calculate_fees("BUY", Decimal(10000), Decimal("20.00"), STOCK)
    assert large["commission"] == Decimal("50.00")  # 0.25 bps rate binds
    assert large["total"] == Decimal("52.00")  # + 0.001% transfer on 200,000


def test_stamp_duty_is_sell_side_only_and_etf_profile_is_exempt() -> None:
    buy = calculate_fees("BUY", Decimal(100), Decimal("2.86"), STOCK)
    sell = calculate_fees("SELL", Decimal(100), Decimal("2.86"), STOCK)
    assert buy["stamp_duty"] == Decimal("0.00")
    assert sell["stamp_duty"] == Decimal("0.14")  # 0.05% of 286.00
    etf_sell = calculate_fees("SELL", Decimal(100), Decimal("0.90"), ETF)
    assert etf_sell["stamp_duty"] == Decimal("0.00")
    assert etf_sell["commission"] == Decimal("0.01")  # 1 bp of 90.00, no minimum


def test_component_rounding_is_half_up_to_cent() -> None:
    fees = calculate_fees("SELL", Decimal(1), Decimal("2.50"), STOCK)
    assert fees["commission"] == Decimal("5.00")
    assert fees["stamp_duty"] == Decimal("0.00")  # 0.00125 rounds to 0.00


def test_affordability_includes_fees_and_board_lot() -> None:
    assert affordable_lots(Decimal(100), Decimal("0.90"), STOCK, RULES) == 1
    # One lot at 1.00 plus the 5.00 minimum commission cannot fit in 100.00.
    assert affordable_lots(Decimal(100), Decimal("1.00"), STOCK, RULES) == 0
    assert affordable_lots(Decimal(100), Decimal("7.50"), STOCK, RULES) == 0
    assert affordable_lots(Decimal(500), Decimal("2.20"), STOCK, RULES) == 2
    assert affordable_lots(Decimal(500), Decimal("2.20"), STOCK, RULES, max_lots=1) == 1
    assert affordable_lots(Decimal(0), Decimal("2.20"), STOCK, RULES) == 0


def test_limit_prices_round_half_up_and_fillability_is_conservative() -> None:
    up, down = limit_prices(Decimal("2.86"), RULES)
    assert (up, down) == (Decimal("3.15"), Decimal("2.57"))
    assert buy_fillable(Decimal("3.14"), up) is True
    assert buy_fillable(Decimal("3.15"), up) is False
    assert sell_fillable(Decimal("2.58"), down) is True
    assert sell_fillable(Decimal("2.57"), down) is False


def test_invalid_declared_inputs_fail_closed() -> None:
    with pytest.raises(PilotRuleError):
        CnFeeProfile(
            name="bad",
            commission_bps="-1",
            min_commission=0,
            stamp_duty_bps=0,
            transfer_fee_bps=0,
        )
    with pytest.raises(PilotRuleError):
        CnFeeProfile(
            name="float",
            commission_bps=1.5,  # type: ignore[arg-type]
            min_commission=0,
            stamp_duty_bps=0,
            transfer_fee_bps=0,
        )
    with pytest.raises(PilotRuleError):
        CnTradability(price_limit_pct="1")
    with pytest.raises(PilotRuleError):
        calculate_fees("BUY", Decimal(0), Decimal("2.00"), STOCK)
    with pytest.raises(PilotRuleError):
        limit_prices(Decimal(0), RULES)


def test_unconfirmed_broker_etf_profile_keeps_the_statutory_minimum() -> None:
    profile = CN_FEE_PROFILES["etf-min-5"]
    fees = calculate_fees("BUY", Decimal(100), Decimal("0.34"), profile)
    assert fees["commission"] == Decimal("5.00")
    assert fees["stamp_duty"] == Decimal("0.00")
    assert fees["total"] == Decimal("5.00")
    sell = calculate_fees("SELL", Decimal(100), Decimal("0.34"), profile)
    assert sell["total"] == Decimal("5.00")  # ETF stamp duty stays zero on sells
