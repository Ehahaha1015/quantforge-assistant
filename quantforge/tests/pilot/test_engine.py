from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import polars as pl
import pytest

from quantforge.data import BAR_SCHEMA, prepare_bars
from quantforge.product.pilot.engine import (
    PilotConfig,
    PilotEngineError,
    pilot_binding,
    pilot_step,
)
from quantforge.product.pilot.fees import CN_FEE_PROFILES, CnTradability
from quantforge.risk import RiskLimits

SYMBOL = "TEST"
START = date(2024, 1, 2)
AS_OF = datetime(2025, 1, 1, tzinfo=UTC)


def make_frame(closes: list[str], opens: list[str] | None = None) -> pl.DataFrame:
    selected_opens = opens if opens is not None else closes
    rows = []
    for index, (open_text, close_text) in enumerate(
        zip(selected_opens, closes, strict=True)
    ):
        open_price, close_price = Decimal(open_text), Decimal(close_text)
        high = max(open_price, close_price) + Decimal("0.05")
        low = min(open_price, close_price) - Decimal("0.05")
        rows.append(
            (
                SYMBOL,
                datetime.combine(START + timedelta(days=index), time.min, tzinfo=UTC),
                float(open_price),
                float(high),
                float(low),
                float(close_price),
                1000.0,
            )
        )
    frame = pl.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "timestamp": [row[1] for row in rows],
            "open": [row[2] for row in rows],
            "high": [row[3] for row in rows],
            "low": [row[4] for row in rows],
            "close": [row[5] for row in rows],
            "volume": [row[6] for row in rows],
        },
        schema=BAR_SCHEMA,
    )
    return prepare_bars(frame, as_of=AS_OF)


def make_config(**overrides: Any) -> PilotConfig:
    base: dict[str, Any] = {
        "symbol": SYMBOL,
        "short_window": 1,
        "long_window": 2,
        "initial_cash": "500",
        "sizing": "all_in",
        "allocation_fraction": "1",
        "max_lots_per_order": None,
        "fee_profile": CN_FEE_PROFILES["stock-default"],
        "tradability": CnTradability(),
        "risk_limits": RiskLimits(
            max_order_notional="500",
            max_position_notional="500",
            max_gross_exposure="500",
        ),
    }
    base.update(overrides)
    return PilotConfig(**base)


def drive(frame: pl.DataFrame, config: PilotConfig, bars: int) -> list[dict[str, Any]]:
    state: dict[str, Any] = {}
    events = []
    for cursor in range(bars):
        event = pilot_step(frame, config, cursor, state)
        state = json.loads(json.dumps(event["state"]))  # fresh-process durable JSON
        events.append(event)
    return events


def test_buy_hold_sell_chain_with_exact_decimal_accounting() -> None:
    frame = make_frame(
        [
            "2.00",
            "2.00",
            "2.10",
            "2.20",
            "2.30",
            "2.40",
            "2.50",
            "2.60",
            "1.90",
            "1.80",
            "1.70",
        ]
    )
    config = make_config()
    events = drive(frame, config, 11)

    assert events[2]["ticket"] is None  # flat and still below the signal
    buy = events[3]
    assert buy["ticket"]["status"] == "PAPER_FILLED"
    assert buy["ticket"]["side"] == "BUY"
    assert Decimal(buy["fill"]["quantity"]) == Decimal(200)  # two board lots
    assert Decimal(buy["fill"]["price"]) == Decimal("2.2")
    assert Decimal(buy["fill"]["fee"]) == Decimal("5.00")  # minimum commission binds
    assert Decimal(buy["state"]["portfolio"]["cash"]) == Decimal("55.00")
    positions = buy["state"]["portfolio"]["positions"]
    assert len(positions) == 1
    assert positions[0]["symbol"] == SYMBOL
    assert Decimal(positions[0]["quantity"]) == Decimal(200)
    assert Decimal(positions[0]["average_price"]) == Decimal("2.20")
    assert buy["risk"]["approved"] is True

    held = events[8]
    assert held["direction"] == 1
    assert held["fill"] is None and held["ticket"] is None
    assert Decimal(held["state"]["portfolio"]["positions"][0]["quantity"]) == Decimal(
        200
    )

    sell = events[9]
    assert sell["ticket"]["status"] == "PAPER_FILLED"
    assert sell["fill"]["side"] == "SELL"
    assert Decimal(sell["fill"]["fee"]) == Decimal("5.18")  # 5.00 + 0.18 stamp
    assert Decimal(sell["state"]["portfolio"]["cash"]) == Decimal("409.82")
    assert sell["state"]["portfolio"]["positions"] == []

    assert events[10]["fill"] is None
    assert sell["state"]["processed_order_ids"] == [
        fill["order_id"] for event in events for fill in [event["fill"]] if fill
    ]


def test_replay_is_deterministic_and_restart_safe() -> None:
    frame = make_frame(
        [
            "2.00",
            "2.00",
            "2.10",
            "2.20",
            "2.30",
            "2.40",
            "2.50",
            "2.60",
            "1.90",
            "1.80",
            "1.70",
        ]
    )
    config = make_config()
    uninterrupted = drive(frame, config, 11)
    early = drive(frame, config, 5)
    state = json.loads(json.dumps(early[-1]["state"]))
    late = []
    for cursor in range(5, 11):
        event = pilot_step(frame, config, cursor, state)
        state = json.loads(json.dumps(event["state"]))
        late.append(event)
    assert early + late == uninterrupted


def test_t_plus_one_guard_blocks_a_same_session_sell_and_the_flag_releases_it() -> None:
    frame = make_frame(
        [
            "2.00",
            "2.00",
            "2.10",
            "2.20",
            "2.30",
            "2.40",
            "2.50",
            "2.60",
            "1.90",
            "1.80",
            "1.70",
        ]
    )
    config = make_config()
    events = drive(frame, config, 9)
    session_nine = frame["timestamp"][9].date().isoformat()
    forged = {
        **events[8]["state"],
        "last_buy": {"date": session_nine, "quantity": "200"},
    }
    blocked = pilot_step(frame, config, 9, forged)
    assert blocked["ticket"]["status"] == "T_PLUS_ONE_BLOCKED"
    assert blocked["fill"] is None
    assert Decimal(
        blocked["state"]["portfolio"]["positions"][0]["quantity"]
    ) == Decimal(200)

    relaxed = make_config(tradability=CnTradability(t_plus_one=False))
    forged_relaxed = {**forged, "binding": pilot_binding(relaxed)}
    released = pilot_step(frame, relaxed, 9, forged_relaxed)
    assert released["ticket"]["status"] == "PAPER_FILLED"
    assert released["fill"]["side"] == "SELL"


def test_price_limits_block_buy_at_limit_up_and_sell_at_limit_down() -> None:
    buy_frame = make_frame(
        ["2.00", "2.00", "2.10", "2.20"], ["2.00", "2.00", "2.10", "2.50"]
    )
    config = make_config()
    events = drive(buy_frame, config, 4)
    blocked_buy = events[3]
    assert blocked_buy["direction"] == 1
    assert blocked_buy["ticket"]["status"] == "UNFILLABLE_PRICE_LIMIT"
    assert blocked_buy["fill"] is None
    assert Decimal(blocked_buy["state"]["portfolio"]["cash"]) == Decimal(500)

    sell_frame = make_frame(
        ["2.00", "2.00", "2.10", "2.20", "1.90", "1.80"],
        ["2.00", "2.00", "2.10", "2.20", "1.90", "1.60"],
    )
    events = drive(sell_frame, config, 6)
    assert events[3]["fill"]["side"] == "BUY"
    blocked_sell = events[5]
    assert blocked_sell["direction"] == 0
    assert blocked_sell["ticket"]["status"] == "UNFILLABLE_PRICE_LIMIT"
    assert blocked_sell["fill"] is None
    assert Decimal(
        blocked_sell["state"]["portfolio"]["positions"][0]["quantity"]
    ) == Decimal(200)


def test_order_notional_cap_declared_at_capital_blocks_an_appreciated_exit() -> None:
    """The frozen risk module applies the order cap to sells too.

    A declaration that sets ``max_order_notional`` equal to the capital blocks
    selling an appreciated position; the pilot default therefore declares a
    wider fat-finger order bound while the position/gross caps stay at capital.
    """
    rising = [
        "2.00",
        "2.00",
        "2.10",
        "2.20",
        "2.40",
        "2.62",
        "2.85",
        "3.11",
        "3.39",
        "3.70",
        "4.03",
        "4.39",
        "4.79",
        "5.22",
        "5.10",
        "5.05",
    ]
    frame = make_frame(rising)
    tight = make_config()
    events = drive(frame, tight, 16)
    assert events[3]["fill"]["side"] == "BUY"  # 100 shares at 2.20, within limits
    exit_ticket = events[15]["ticket"]
    assert exit_ticket["status"] == "RISK_REJECTED"
    assert "ORDER_NOTIONAL_LIMIT" in exit_ticket["note"]
    assert events[15]["fill"] is None

    wide = make_config(
        risk_limits=RiskLimits(
            max_order_notional="5000",
            max_position_notional="500",
            max_gross_exposure="500",
        )
    )
    released = drive(frame, wide, 16)
    assert released[15]["ticket"]["status"] == "PAPER_FILLED"
    assert released[15]["fill"]["side"] == "SELL"
    assert Decimal(released[15]["state"]["portfolio"]["cash"]) > Decimal(500)


def test_one_hundred_yuan_cannot_afford_one_lot_at_ordinary_prices() -> None:
    frame = make_frame(["2.00", "2.00", "2.10", "2.20"])
    config = make_config(
        initial_cash="100",
        risk_limits=RiskLimits(
            max_order_notional="100",
            max_position_notional="100",
            max_gross_exposure="100",
        ),
    )
    events = drive(frame, config, 4)
    ticket = events[3]["ticket"]
    assert ticket["status"] == "INSUFFICIENT_CASH_FOR_ONE_LOT"
    assert ticket["quantity"] is None
    assert ticket["estimated_fees"] is None
    assert events[3]["fill"] is None
    assert Decimal(events[3]["state"]["portfolio"]["cash"]) == Decimal(100)


def test_declared_risk_limit_rejects_an_otherwise_affordable_order() -> None:
    frame = make_frame(["2.00", "2.00", "2.10", "2.20"])
    config = make_config(
        risk_limits=RiskLimits(
            max_order_notional="100",
            max_position_notional="500",
            max_gross_exposure="500",
        )
    )
    events = drive(frame, config, 4)
    assert events[3]["ticket"]["status"] == "RISK_REJECTED"
    assert events[3]["risk"]["approved"] is False
    assert "ORDER_NOTIONAL_LIMIT" in events[3]["ticket"]["note"]


def test_engine_refuses_mismatched_frames_bindings_and_states() -> None:
    frame = make_frame(["2.00", "2.00", "2.10"])
    other = make_frame(["2.00", "2.00", "2.10"]).with_columns(
        pl.lit("OTHER").alias("symbol")
    )
    config = make_config()
    with pytest.raises(PilotEngineError, match="PILOT_FRAME_SYMBOL_MISMATCH"):
        pilot_step(other, config, 0, {})
    with pytest.raises(PilotEngineError, match="PILOT_DATA_EXHAUSTED"):
        pilot_step(frame, config, 3, {})
    with pytest.raises(PilotEngineError, match="PILOT_INITIAL_CURSOR"):
        pilot_step(frame, config, 1, {})
    events = drive(frame, config, 2)
    changed = make_config(initial_cash="600")
    with pytest.raises(PilotEngineError, match="PILOT_RECIPE_CHANGED"):
        pilot_step(frame, changed, 2, events[-1]["state"])
    broken = dict(events[-1]["state"])
    broken["processed_order_ids"] = ["x", "x"]
    with pytest.raises(PilotEngineError, match="PILOT_DUPLICATE_ORDER_HISTORY"):
        pilot_step(frame, config, 2, broken)


def test_fractional_sizing_never_exceeds_the_declared_budget() -> None:
    frame = make_frame(["2.00", "2.00", "2.10", "2.20"])
    config = make_config(sizing="fraction", allocation_fraction="0.2")
    events = drive(frame, config, 4)
    # 20% of 500 = 100 budget; one lot at 2.20 plus fees does not fit inside it.
    assert events[3]["ticket"]["status"] == "INSUFFICIENT_CASH_FOR_ONE_LOT"


def test_canary_hold_buys_exactly_one_lot_and_never_sells() -> None:
    frame = make_frame(["2.00", "2.10", "1.50", "3.00", "2.80", "2.60", "2.40", "2.20"])
    config = make_config(strategy="canary_hold", sizing="one_lot")
    events = drive(frame, config, 8)
    assert events[0]["ticket"]["status"] == "PAPER_FILLED"
    assert Decimal(events[0]["fill"]["quantity"]) == Decimal(100)  # one lot, not all-in
    assert Decimal(events[0]["fill"]["fee"]) == Decimal("5.00")
    assert Decimal(events[0]["state"]["portfolio"]["cash"]) == Decimal("295.00")
    assert all(event["fill"] is None for event in events[1:])
    assert [event["ticket"] is not None for event in events] == [True] + [False] * 7
    assert Decimal(
        events[-1]["state"]["portfolio"]["positions"][0]["quantity"]
    ) == Decimal(100)


def test_canary_waits_until_the_first_affordable_session() -> None:
    frame = make_frame(["2.00", "2.00", "0.30", "0.31", "0.32"])
    config = make_config(
        strategy="canary_hold",
        sizing="one_lot",
        initial_cash="100",
        risk_limits=RiskLimits(
            max_order_notional="1000",
            max_position_notional="1000",
            max_gross_exposure="1000",
        ),
    )
    events = drive(frame, config, 5)
    assert [
        event["ticket"]["status"] if event["ticket"] else None for event in events
    ] == [
        "INSUFFICIENT_CASH_FOR_ONE_LOT",
        "INSUFFICIENT_CASH_FOR_ONE_LOT",
        "PAPER_FILLED",
        None,
        None,
    ]
    assert Decimal(events[2]["fill"]["price"]) == Decimal("0.3")
    assert Decimal(events[4]["state"]["portfolio"]["cash"]) == Decimal(65)


def test_canary_requires_one_lot_sizing() -> None:
    with pytest.raises(PilotEngineError, match="canary_hold requires one_lot"):
        make_config(strategy="canary_hold")
