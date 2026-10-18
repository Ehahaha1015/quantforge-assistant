"""Strict discriminated v2 strategy schemas and bounded deterministic dispatch."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

import polars as pl
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from ._common import MAX_WINDOW
from .bollinger_reversion import (
    BollingerReversionConfig,
    generate_bollinger_reversion_signals,
)
from .donchian_breakout import (
    DonchianBreakoutConfig,
    generate_donchian_breakout_signals,
)
from .moving_average import (
    MovingAverageCrossoverConfig,
    generate_moving_average_signals,
)
from .time_series_momentum import (
    TimeSeriesMomentumConfig,
    generate_time_series_momentum_signals,
)

StrategyFamily = Literal[
    "moving_average_crossover",
    "donchian_breakout",
    "bollinger_reversion",
    "time_series_momentum",
]
type StrategyConfig = (
    MovingAverageCrossoverConfig
    | DonchianBreakoutConfig
    | BollingerReversionConfig
    | TimeSeriesMomentumConfig
)
STRATEGY_CONFIG_TYPES = (
    MovingAverageCrossoverConfig,
    DonchianBreakoutConfig,
    BollingerReversionConfig,
    TimeSeriesMomentumConfig,
)
POSITION_RULES: dict[str, str] = {
    "moving_average_crossover": "long_when_short_above_long_else_flat",
    "donchian_breakout": "long_on_prior_high_breakout_flat_on_prior_low_breakdown",
    "bollinger_reversion": "long_at_or_below_lower_band_flat_at_or_above_mean",
    "time_series_momentum": "long_when_return_above_threshold_else_flat",
}
_PERCENT_TEXT = re.compile(r"(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,4})?\Z")
_Window = Annotated[int, Field(strict=True, ge=2, le=MAX_WINDOW)]


class _StrategySpec(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always"
    )


class MovingAverageStrategySpec(_StrategySpec):
    name: Literal["moving_average_crossover"]
    short_window: Annotated[int, Field(strict=True, ge=1, le=MAX_WINDOW)]
    long_window: _Window
    position_rule: Literal["long_when_short_above_long_else_flat"]

    @model_validator(mode="after")
    def _pair(self) -> Self:
        self.to_config()
        return self

    def to_config(self) -> MovingAverageCrossoverConfig:
        return MovingAverageCrossoverConfig(self.short_window, self.long_window)


class DonchianStrategySpec(_StrategySpec):
    name: Literal["donchian_breakout"]
    entry_window: _Window
    exit_window: Annotated[int, Field(strict=True, ge=1, le=MAX_WINDOW)]
    position_rule: Literal["long_on_prior_high_breakout_flat_on_prior_low_breakdown"]

    @model_validator(mode="after")
    def _pair(self) -> Self:
        self.to_config()
        return self

    def to_config(self) -> DonchianBreakoutConfig:
        return DonchianBreakoutConfig(self.entry_window, self.exit_window)


class BollingerStrategySpec(_StrategySpec):
    name: Literal["bollinger_reversion"]
    window: _Window
    k_pct: Annotated[Decimal, Field(gt=0, le=1000, max_digits=8, decimal_places=4)]
    position_rule: Literal["long_at_or_below_lower_band_flat_at_or_above_mean"]

    @field_validator("k_pct", mode="before")
    @classmethod
    def _percent(cls, value: Any) -> Decimal:
        return _decimal_text(value)

    def to_config(self) -> BollingerReversionConfig:
        return BollingerReversionConfig(self.window, self.k_pct)


class MomentumStrategySpec(_StrategySpec):
    name: Literal["time_series_momentum"]
    lookback: _Window
    threshold_pct: Annotated[
        Decimal, Field(ge=0, le=100, max_digits=7, decimal_places=4)
    ]
    position_rule: Literal["long_when_return_above_threshold_else_flat"]

    @field_validator("threshold_pct", mode="before")
    @classmethod
    def _percent(cls, value: Any) -> Decimal:
        return _decimal_text(value)

    def to_config(self) -> TimeSeriesMomentumConfig:
        return TimeSeriesMomentumConfig(self.lookback, self.threshold_pct)


def _decimal_text(value: Any) -> Decimal:
    if not isinstance(value, str) or _PERCENT_TEXT.fullmatch(value) is None:
        raise ValueError(
            "percent must be an unsigned fixed-point decimal string with at most four fractional digits"
        )
    return Decimal(value)


type StrategySpec = Annotated[
    MovingAverageStrategySpec
    | DonchianStrategySpec
    | BollingerStrategySpec
    | MomentumStrategySpec,
    Field(discriminator="name"),
]
STRATEGY_ADAPTER: TypeAdapter[StrategySpec] = TypeAdapter(StrategySpec)


def parse_strategy(payload: Mapping[str, Any]) -> StrategySpec:
    return STRATEGY_ADAPTER.validate_python(dict(payload))


def strategy_from_parameters(
    family: str, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    if family not in POSITION_RULES:
        raise ValueError("unsupported strategy family")
    if "name" in parameters or "position_rule" in parameters:
        raise ValueError("family parameters cannot contain strategy identity")
    return parse_strategy(
        {"name": family, **parameters, "position_rule": POSITION_RULES[family]}
    ).model_dump(mode="json")


def config_from_strategy(
    payload: Mapping[str, Any], *, legacy: bool = False
) -> StrategyConfig:
    if legacy:
        # Legacy admission remains authoritative and retains its unbounded SMA windows.
        return MovingAverageCrossoverConfig(
            payload["short_window"], payload["long_window"]
        )
    return parse_strategy(payload).to_config()


def required_observations(config: StrategyConfig) -> int:
    if isinstance(config, MovingAverageCrossoverConfig):
        return config.long_window
    if isinstance(config, DonchianBreakoutConfig):
        return config.entry_window + 1
    if isinstance(config, BollingerReversionConfig):
        return config.window
    if isinstance(config, TimeSeriesMomentumConfig):
        return config.lookback + 1
    raise TypeError("unsupported strategy configuration")


def generate_strategy_signals(
    bars: pl.DataFrame, config: StrategyConfig
) -> pl.DataFrame:
    if isinstance(config, MovingAverageCrossoverConfig):
        return generate_moving_average_signals(bars, config)
    if isinstance(config, DonchianBreakoutConfig):
        return generate_donchian_breakout_signals(bars, config)
    if isinstance(config, BollingerReversionConfig):
        return generate_bollinger_reversion_signals(bars, config)
    if isinstance(config, TimeSeriesMomentumConfig):
        return generate_time_series_momentum_signals(bars, config)
    raise TypeError("unsupported strategy configuration")


def family_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    strategy = parse_strategy(payload)
    parameters = strategy.model_dump(mode="json", exclude={"name", "position_rule"})
    conventions = {
        "moving_average_crossover": "trailing_close_means_include_current_bar",
        "donchian_breakout": "prior_ohlc_high_low_windows_exclude_current_bar_close_trigger",
        "bollinger_reversion": "trailing_close_population_variance_include_current_bar_percent_sigma",
        "time_series_momentum": "close_to_lagged_close_strict_percent_return_threshold",
    }
    bounds = {
        "moving_average_crossover": "1 <= short_window < long_window <= 252",
        "donchian_breakout": "1 <= exit_window < entry_window <= 252",
        "bollinger_reversion": "2 <= window <= 252; 0 < k_pct <= 1000",
        "time_series_momentum": "2 <= lookback <= 252; 0 <= threshold_pct <= 100",
    }
    return {
        "schema_version": "strategy-family-metadata-v1",
        "strategy_family": strategy.name,
        "parameters": parameters,
        "position_rule": strategy.position_rule,
        "position_values": [0, 1],
        "maximum_parameter_window": MAX_WINDOW,
        "required_observations": required_observations(strategy.to_config()),
        "parameter_domain": bounds[strategy.name],
        "calculation_convention": conventions[strategy.name],
        "information_boundary": "canonical_bars_at_or_before_signal_timestamp",
        "execution_timing": "next_bar_open",
        "validated_alpha": False,
    }
