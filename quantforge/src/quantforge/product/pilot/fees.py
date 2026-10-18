"""Declared CN A-share fee and tradability rules for the paper-only pilot.

Nothing here observes a broker, a clock or a network. Every number is an
explicit configuration assumption; the shipped profiles are documentation-derived
placeholders to be verified against the operator's actual broker before any
real order. All money arithmetic is Decimal inside an isolated 50-digit context,
and each fee component is rounded half-up to 0.01 CNY (分).
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, DecimalException, localcontext
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "CN_FEE_PROFILES",
    "CnFeeProfile",
    "CnTradability",
    "PilotRuleError",
    "affordable_lots",
    "buy_fillable",
    "calculate_fees",
    "limit_prices",
    "sell_fillable",
]

_CENT = Decimal("0.01")
_TEN_THOUSAND = Decimal(10000)
_BPS = Decimal(10000)
_PRECISION = 50


class PilotRuleError(ValueError):
    """Sanitized CN pilot market-rule or input precondition failure."""


def _decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int, str)):
        raise PilotRuleError(
            f"{name} must be an exact Decimal, int, or str, not {type(value).__name__}"
        )
    try:
        number = Decimal(value)
    except (DecimalException, ValueError) as exc:
        raise PilotRuleError(f"{name} must be a decimal number") from exc
    if not number.is_finite():
        raise PilotRuleError(f"{name} must be finite")
    return number


def _cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


class CnFeeProfile(BaseModel):
    """One declared fee schedule; not a broker quote and not a live entitlement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    commission_bps: Decimal
    min_commission: Decimal
    stamp_duty_bps: Decimal
    transfer_fee_bps: Decimal

    @field_validator(
        "commission_bps",
        "min_commission",
        "stamp_duty_bps",
        "transfer_fee_bps",
        mode="before",
    )
    @classmethod
    def _nonnegative(cls, value: Any, info: Any) -> Decimal:
        number = _decimal(value, name=str(info.field_name))
        if number < 0:
            raise PilotRuleError(f"fee field {info.field_name} must be nonnegative")
        return number

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise PilotRuleError(f"fee profile is invalid: {exc}") from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            return cls.model_validate_json(value)
        except ValidationError as exc:
            raise PilotRuleError(f"fee profile is invalid: {exc}") from exc

    def to_json(self) -> dict[str, str]:
        return {
            "name": self.name,
            "commission_bps": str(self.commission_bps),
            "min_commission": str(self.min_commission),
            "stamp_duty_bps": str(self.stamp_duty_bps),
            "transfer_fee_bps": str(self.transfer_fee_bps),
        }


# Declared assumption profiles. The operator must confirm or override them
# against the actual broker before any real order; see docs/CN_PILOT.md.
CN_FEE_PROFILES: dict[str, CnFeeProfile] = {
    "stock-default": CnFeeProfile(
        name="stock-default",
        commission_bps=Decimal("2.5"),
        min_commission=Decimal("5.00"),
        stamp_duty_bps=Decimal(5),
        transfer_fee_bps=Decimal("0.1"),
    ),
    "etf-no-minimum": CnFeeProfile(
        name="etf-no-minimum",
        commission_bps=Decimal(1),
        min_commission=Decimal("0.00"),
        stamp_duty_bps=Decimal(0),
        transfer_fee_bps=Decimal(0),
    ),
    # Unconfirmed-broker ETF profile: the statutory default minimum commission
    # applies (a per-trade commission below 5 CNY is charged as 5 CNY). ETF
    # stamp duty and transfer fee are statutorily zero. Declared assumption.
    "etf-min-5": CnFeeProfile(
        name="etf-min-5",
        commission_bps=Decimal(1),
        min_commission=Decimal("5.00"),
        stamp_duty_bps=Decimal(0),
        transfer_fee_bps=Decimal(0),
    ),
}


class CnTradability(BaseModel):
    """Declared CN execution frictions; conservative simplifications are explicit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lot_size: int = Field(default=100, ge=1)
    price_limit_pct: Decimal = Field(default=Decimal("0.10"))
    t_plus_one: bool = True

    @field_validator("price_limit_pct", mode="before")
    @classmethod
    def _pct(cls, value: Any, info: Any) -> Decimal:
        number = _decimal(value, name="price_limit_pct")
        if not 0 < number < 1:
            raise PilotRuleError("price_limit_pct must be within (0, 1)")
        return number

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise PilotRuleError(f"tradability rules are invalid: {exc}") from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            return cls.model_validate_json(value)
        except ValidationError as exc:
            raise PilotRuleError(f"tradability rules are invalid: {exc}") from exc

    def to_json(self) -> dict[str, Any]:
        return {
            "lot_size": self.lot_size,
            "price_limit_pct": str(self.price_limit_pct),
            "t_plus_one": self.t_plus_one,
        }


def calculate_fees(
    side: Literal["BUY", "SELL"],
    quantity: Decimal,
    price: Decimal,
    profile: CnFeeProfile,
) -> dict[str, Decimal]:
    """Exact component fees for one paper order; stamp duty applies to sells only."""
    if side not in ("BUY", "SELL"):
        raise PilotRuleError("side must be BUY or SELL")
    if not isinstance(profile, CnFeeProfile):
        raise PilotRuleError("profile must be a CnFeeProfile")
    quantity = _decimal(quantity, name="quantity")
    price = _decimal(price, name="price")
    if quantity <= 0 or price <= 0:
        raise PilotRuleError("quantity and price must be positive")
    with localcontext() as arithmetic:
        arithmetic.prec = _PRECISION
        amount = quantity * price
        commission = max(profile.min_commission, amount * profile.commission_bps / _BPS)
        stamp = amount * profile.stamp_duty_bps / _BPS if side == "SELL" else Decimal(0)
        transfer = amount * profile.transfer_fee_bps / _BPS
        commission, stamp, transfer = (
            _cent(commission),
            _cent(stamp),
            _cent(transfer),
        )
        return {
            "amount": amount,
            "commission": commission,
            "stamp_duty": stamp,
            "transfer_fee": transfer,
            "total": commission + stamp + transfer,
        }


def affordable_lots(
    cash: Decimal,
    price: Decimal,
    profile: CnFeeProfile,
    tradability: CnTradability,
    *,
    max_lots: int | None = None,
) -> int:
    """Largest declared-affordable board-lot count; 0 when one lot does not fit.

    Fees (including any minimum commission) are part of affordability, so an
    account that cannot cover one lot plus fees gets an honest zero.
    """
    cash = _decimal(cash, name="cash")
    price = _decimal(price, name="price")
    if not isinstance(profile, CnFeeProfile) or not isinstance(
        tradability, CnTradability
    ):
        raise PilotRuleError("profile and tradability must be declared models")
    if cash <= 0 or price <= 0:
        return 0
    if max_lots is not None and max_lots < 0:
        raise PilotRuleError("max_lots must be nonnegative")
    lot = Decimal(tradability.lot_size)
    rate = (profile.commission_bps + profile.transfer_fee_bps) / _BPS
    with localcontext() as arithmetic:
        arithmetic.prec = _PRECISION
        bound = int((cash / (price * lot * (1 + rate))).to_integral_value(ROUND_FLOOR))
        if max_lots is not None:
            bound = min(bound, max_lots)
        while bound >= 1:
            fees = calculate_fees("BUY", lot * bound, price, profile)
            if lot * bound * price + fees["total"] <= cash:
                return bound
            bound -= 1
        return 0


def limit_prices(
    previous_close: Decimal, tradability: CnTradability
) -> tuple[Decimal, Decimal]:
    """Two-decimal limit-up / limit-down from the previous close (half-up)."""
    previous_close = _decimal(previous_close, name="previous_close")
    if previous_close <= 0:
        raise PilotRuleError("previous_close must be positive")
    with localcontext() as arithmetic:
        arithmetic.prec = _PRECISION
        up = _cent(previous_close * (1 + tradability.price_limit_pct))
        down = _cent(previous_close * (1 - tradability.price_limit_pct))
    return up, down


def buy_fillable(open_price: Decimal, limit_up: Decimal) -> bool:
    """Conservative: a buy is never assumed to fill at or above limit-up."""
    return _decimal(open_price, name="open_price") < _decimal(limit_up, name="limit_up")


def sell_fillable(open_price: Decimal, limit_down: Decimal) -> bool:
    """Conservative: a sell is never assumed to fill at or below limit-down."""
    return _decimal(open_price, name="open_price") > _decimal(
        limit_down, name="limit_down"
    )
