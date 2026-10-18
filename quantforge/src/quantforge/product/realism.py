"""Deterministic cash/lot/slippage stress of Core validation decisions only.

These declared scenarios supplement the admitted vector screen. They cannot
replace its EvidencePack, establish an exchange rule or grant live eligibility.
"""

from __future__ import annotations

import hashlib
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any

from quantforge.experiments import canonical_json

from .pilot.fees import CnFeeProfile, CnTradability, affordable_lots, calculate_fees


def cash_lot_stress(rows: list[dict[str, Any]], *, evidence_id: str) -> dict[str, Any]:
    if (
        not rows
        or len(rows) > 100000
        or any(
            set(v) != {"timestamp", "open", "close", "volume", "target"} for v in rows
        )
    ):
        raise ValueError("complete validation rows required")
    if any(
        v["target"] not in (0.0, 1.0)
        or not all(
            Decimal(str(v[k])).is_finite() and Decimal(str(v[k])) > 0
            for k in ("open", "close")
        )
        or not Decimal(str(v["volume"])).is_finite()
        or Decimal(str(v["volume"])) < 0
        for v in rows
    ):
        raise ValueError("invalid validation market facts")
    if [v["timestamp"] for v in rows] != sorted({v["timestamp"] for v in rows}):
        raise ValueError("unique chronological validation required")
    scenarios = []
    with localcontext() as arithmetic:
        arithmetic.prec = 50
        for label, lot, commission, minimum, slippage in (
            ("INTEGER_UNIT_BASE", 1, "10", "0", "0"),
            ("LOT_100_MINIMUM_FEE_STRESS", 100, "10", "5", "5"),
            ("LOT_100_HIGH_COST_STRESS", 100, "25", "5", "20"),
        ):
            profile = CnFeeProfile(
                name=label,
                commission_bps=commission,
                min_commission=minimum,
                stamp_duty_bps="0",
                transfer_fee_bps="0",
            )
            rules = CnTradability(lot_size=lot, price_limit_pct="0.1", t_plus_one=True)
            cash, quantity, initial, total_fees = (
                Decimal(100000),
                Decimal(0),
                Decimal(100000),
                Decimal(0),
            )
            peak, drawdown = initial, Decimal(0)
            trades, skipped, partial = 0, 0, 0
            periods = []
            for row in rows:
                opening, close, volume = (
                    Decimal(str(row[k])) for k in ("open", "close", "volume")
                )
                max_quantity = (volume * Decimal("0.1") / lot).to_integral_value(
                    rounding=ROUND_FLOOR
                ) * lot
                target = row["target"]
                changed = False
                # Signals are prior-close decisions from Core. Positions are
                # carried across rows; no within-session sell of a fresh buy.
                if target == 1.0 and quantity == 0:
                    price = opening * (1 + Decimal(slippage) / 10000)
                    lots = affordable_lots(cash, price, profile, rules)
                    wanted = Decimal(lots * lot)
                    bought = min(wanted, max_quantity)
                    if bought > 0:
                        fees = calculate_fees("BUY", bought, price, profile)["total"]
                        cash -= bought * price + fees
                        quantity = bought
                        total_fees += fees
                        trades += 1
                        changed = True
                        partial += int(bought < wanted)
                    else:
                        skipped += 1
                elif target == 0.0 and quantity > 0:
                    price = opening * (1 - Decimal(slippage) / 10000)
                    sold = min(quantity, max_quantity)
                    if sold > 0:
                        fees = calculate_fees("SELL", sold, price, profile)["total"]
                        cash += sold * price - fees
                        quantity -= sold
                        total_fees += fees
                        trades += 1
                        changed = True
                        partial += int(quantity > 0)
                    else:
                        skipped += 1
                equity = cash + quantity * close
                if cash < 0 or quantity < 0:
                    raise ValueError("cash or position conservation failed")
                peak = max(peak, equity)
                drawdown = min(drawdown, equity / peak - 1)
                periods.append(
                    {
                        "timestamp": row["timestamp"],
                        "cash": str(cash),
                        "quantity": str(quantity),
                        "equity": str(equity),
                        "traded": changed,
                    }
                )
            scenarios.append(
                {
                    "scenario": label,
                    "lot_size": lot,
                    "fee_profile": profile.to_json(),
                    "slippage_bps": slippage,
                    "volume_fraction_cap": "0.1",
                    "initial_cash": str(initial),
                    "final_equity": periods[-1]["equity"],
                    "return": str(Decimal(periods[-1]["equity"]) / initial - 1),
                    "max_drawdown": str(drawdown),
                    "total_fees": str(total_fees),
                    "trade_count": trades,
                    "unfilled_signal_count": skipped,
                    "partial_fill_count": partial,
                    "periods": periods,
                }
            )
    body = {
        "schema_version": "validation-execution-stress-v1",
        "evidence_id": evidence_id,
        "evaluation_stage": "VALIDATION_ONLY",
        "scenarios": scenarios,
        "limitations": [
            "Declared cash/lot/fee/slippage scenarios; not authenticated broker or exchange rules.",
            "Daily volume participation is a proxy, not order-book fill evidence.",
            "A volume-limited partial buy is not topped up while the target stays long; exposure can remain below target.",
            "No dividend/corporate-action series: price-return stress, not total return.",
            "No spread/order book, suspension/calendar or price-limit observations: live eligibility denied.",
            "Daily next-open simulation cannot prove intraday T+1/session compliance.",
            "100-unit stress on foreign or synthetic data is hypothetical, not A-share qualification.",
        ],
        "validated_alpha": False,
        "live_eligible": False,
    }
    return {
        **body,
        "diagnostic_hash": hashlib.sha256(canonical_json(body).encode()).hexdigest(),
    }
