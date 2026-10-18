"""Pure, immutable research proposals and admitted recipes; no OOS authority."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, Literal, Self
from unicodedata import category

from pydantic import (
    BaseModel,
    ConfigDict,
    field_serializer,
    field_validator,
    model_validator,
)

from quantforge.backtest import BacktestConfig
from quantforge.data import DatasetSnapshot
from quantforge.experiments import FrozenPayload, canonical_config_hash, canonical_json
from quantforge.strategies import MovingAverageCrossoverConfig
from quantforge.strategies.models import parse_strategy, required_observations
from quantforge.validation import ChronologicalHoldoutConfig, WalkForwardConfig

# The existing EXPERIMENT-001 identifier grammar, not a new identity scheme.
_EXPERIMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_IDENTIFIER_LENGTH = 128
_DAILY_PERIODS_PER_YEAR = 252
_BASIS_POINTS_PER_UNIT = 10000
_MINIMUM_PYTHON_VERSION = (3, 12)
_TIMESTAMP_KEYS = (
    "development_start",
    "development_end",
    "validation_start",
    "validation_end",
    "test_start",
    "test_end",
)


class ResearchAdmissionError(ValueError):
    """A proposal or interchange artifact is not an admissible research recipe."""


def _object(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if isinstance(value, FrozenPayload):
        value = value.to_payload()
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")  # noqa: TRY004 -- Pydantic wraps ValueError.
    copied = FrozenPayload.from_payload(dict(value), name=name).to_payload()
    if set(copied) != keys:
        raise ValueError(f"{name} has missing or unsupported fields")
    return copied


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty trimmed text")
    if any(category(c) == "Cc" for c in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be a strict integer")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite real number")  # noqa: TRY004 -- Pydantic wraps ValueError.
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be finite")
    return normalized


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("timestamp must be an aware ISO datetime") from exc
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _instant(value: Any) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _digest(value: Any, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} has invalid canonical identity syntax")
    return value


class ResearchRequest(BaseModel):
    """Uninterpreted proposal text and explicit requested research constraints."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
        revalidate_instances="always",
    )

    schema_version: Literal["research-request-v1"]
    experiment_id: str
    goal_text: str
    submitted_at: datetime
    symbol: str
    frequency: Literal["daily"]
    constraints: FrozenPayload
    provenance: FrozenPayload

    @field_validator("experiment_id", mode="before")
    @classmethod
    def _identifier(cls, value: Any) -> str:
        if (
            not isinstance(value, str)
            or len(value) > _MAX_IDENTIFIER_LENGTH
            or _EXPERIMENT_ID.fullmatch(value) is None
        ):
            raise ValueError(
                "experiment_id must follow EXPERIMENT-001 identifier syntax"
            )
        return value

    @field_validator("goal_text", "symbol", mode="before")
    @classmethod
    def _proposal_text(cls, value: Any) -> str:
        return _text(value, "request text")

    @field_validator("submitted_at", mode="before")
    @classmethod
    def _submitted_at(cls, value: Any) -> datetime:
        return _utc(value)

    @field_validator("constraints", mode="before")
    @classmethod
    def _constraints(cls, value: Any) -> FrozenPayload:
        payload = _object(value, {"max_drawdown_loss_magnitude"}, "constraints")
        limit = _number(
            payload["max_drawdown_loss_magnitude"], "drawdown loss magnitude"
        )
        if not 0 < limit <= 1:
            raise ValueError("drawdown loss magnitude must be in (0, 1]")
        return FrozenPayload.from_payload(
            {"max_drawdown_loss_magnitude": limit}, name="constraints"
        )

    @field_validator("provenance", mode="before")
    @classmethod
    def _provenance(cls, value: Any) -> FrozenPayload:
        payload = _object(value, {"source", "reference"}, "proposal provenance")
        if payload["source"] not in ("user", "llm", "scripted"):
            raise ValueError("unsupported proposal source")
        payload["reference"] = _text(payload["reference"], "proposal reference")
        return FrozenPayload.from_payload(payload, name="provenance")

    @field_serializer("constraints", "provenance")
    def _payload(self, value: FrozenPayload) -> dict[str, Any]:
        return value.to_payload()


def _normalize_recipe(
    request: ResearchRequest,
    proposal: Any,
    dataset_snapshot: Any,
    data_conventions: Any,
    code_context: Any,
    split_timestamps: Any,
) -> FrozenPayload:
    p = _object(
        proposal,
        {
            "schema_version",
            "hypothesis",
            "strategy",
            "execution",
            "backtest",
            "costs",
            "split",
            "parameter_optimization",
            "random_seed",
        },
        "proposal",
    )
    if (
        p["schema_version"] not in ("sma-proposal-v1", "strategy-proposal-v2")
        or p["parameter_optimization"] != "none"
    ):
        raise ValueError("unsupported proposal version or parameter optimization")
    if _integer(p["random_seed"], "random_seed") != 0:
        raise ValueError("the deterministic recipe requires the unused seed 0")
    hypothesis = _object(
        p["hypothesis"], {"statement", "rationale", "falsification"}, "hypothesis"
    )
    for key in hypothesis:
        hypothesis[key] = _text(hypothesis[key], key)
    hypothesis.update(
        schema_version="research-hypothesis-v1",
        experiment_id=request.experiment_id,
        provenance=request.provenance.to_payload(),
    )

    legacy = p["schema_version"] == "sma-proposal-v1"
    if legacy:
        # Preserve the original v1 whitelist, normalization and unlimited windows.
        strategy = _object(
            p["strategy"],
            {"name", "short_window", "long_window", "position_rule"},
            "strategy",
        )
        if (
            strategy["name"] != "moving_average_crossover"
            or strategy["position_rule"] != "long_when_short_above_long_else_flat"
        ):
            raise ValueError("unsupported strategy or position rule")
        sma = MovingAverageCrossoverConfig(
            short_window=_integer(strategy["short_window"], "short_window"),
            long_window=_integer(strategy["long_window"], "long_window"),
        )
        strategy.update(asdict(sma))
        minimum_history = sma.long_window
    else:
        if not isinstance(p["strategy"], Mapping):
            raise ValueError("strategy must be a JSON object")
        typed_strategy = parse_strategy(p["strategy"])
        strategy = typed_strategy.model_dump(mode="json")
        minimum_history = required_observations(typed_strategy.to_config())
    execution = _object(p["execution"], {"timing", "terminal_liquidation"}, "execution")
    if (
        execution["timing"] != "next_bar_open"
        or execution["terminal_liquidation"] is not False
    ):
        raise ValueError("only next-bar-open without terminal liquidation is supported")
    backtest = _object(
        p["backtest"], {"initial_cash", "fee_bps", "periods_per_year"}, "backtest"
    )
    config = BacktestConfig(
        initial_cash=_number(backtest["initial_cash"], "initial_cash"),
        fee_bps=_number(backtest["fee_bps"], "fee_bps"),
        periods_per_year=_integer(backtest["periods_per_year"], "periods_per_year"),
    )
    if (
        not 0 < config.fee_bps < _BASIS_POINTS_PER_UNIT
        or config.periods_per_year != _DAILY_PERIODS_PER_YEAR
    ):
        raise ValueError(
            "daily research requires explicit nonzero fees below 10000 bps and 252 periods/year"
        )
    costs = _object(p["costs"], {"model", "slippage", "dividends"}, "costs")
    if costs != {
        "model": "position_switch_fee_bps",
        "slippage": "not_modelled",
        "dividends": "not_modelled",
    }:
        raise ValueError("unsupported or ambiguous cost assumptions")

    split = _object(
        p["split"],
        {
            "observations",
            "warmup_bars",
            "development_bars",
            "validation_bars",
            "test_bars",
            "validation_window_bars",
        },
        "split",
    )
    for key in split:
        split[key] = _integer(split[key], key)
        if split[key] <= 0:
            raise ValueError("split counts must be positive")
    h, d, v, t, w = (
        split[k]
        for k in (
            "warmup_bars",
            "development_bars",
            "validation_bars",
            "test_bars",
            "validation_window_bars",
        )
    )
    if not minimum_history <= h < d or split["observations"] != d + v + t or v % w:
        raise ValueError("inconsistent warmup or chronological split")
    ChronologicalHoldoutConfig(history_bars=h)
    WalkForwardConfig(initial_history_bars=d, oos_window_bars=w)
    ChronologicalHoldoutConfig(history_bars=d + v)

    if isinstance(dataset_snapshot, DatasetSnapshot):
        dataset_snapshot = dataset_snapshot.model_dump(mode="python", warnings="error")
    if not isinstance(dataset_snapshot, Mapping):
        raise ValueError(  # noqa: TRY004 -- Pydantic wraps ValueError.
            "dataset_snapshot must be a native snapshot or its validated payload"
        )
    if type(dataset_snapshot.get("row_count")) is not int:
        raise ValueError("snapshot row_count must be a strict integer")
    snapshot = DatasetSnapshot.model_validate(dict(dataset_snapshot))
    if (
        snapshot.row_count != split["observations"]
        or snapshot.min_timestamp is None
        or snapshot.max_timestamp is None
        or not snapshot.min_timestamp < snapshot.max_timestamp <= snapshot.as_of
    ):
        raise ValueError("snapshot coverage does not match the recipe")
    _text(snapshot.source, "snapshot source")
    _text(snapshot.schema_version, "snapshot schema_version")
    snapshot_payload = snapshot.model_dump(mode="json")
    for key in ("min_timestamp", "max_timestamp", "as_of"):
        snapshot_payload[key] = _instant(snapshot_payload[key])

    conventions = _object(
        data_conventions,
        {"timestamp", "session", "adjustment", "vintage"},
        "data conventions",
    )
    for key in conventions:
        conventions[key] = _text(conventions[key], key)
    code = _object(
        code_context, {"git_commit", "lockfile_sha256", "runtime"}, "code context"
    )
    code["git_commit"] = _digest(code["git_commit"], _GIT_COMMIT, "git_commit")
    code["lockfile_sha256"] = _digest(
        code["lockfile_sha256"], _SHA256, "lockfile_sha256"
    )
    runtime = _object(
        code["runtime"], {"python", "polars", "pydantic", "duckdb"}, "runtime"
    )
    for key in runtime:
        runtime[key] = _text(runtime[key], key)
    version = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", runtime["python"])
    if version is None or (int(version[1]), int(version[2])) < _MINIMUM_PYTHON_VERSION:
        raise ValueError("Python runtime must be 3.12 or newer")
    code["runtime"] = runtime
    timestamps = _object(split_timestamps, set(_TIMESTAMP_KEYS), "split timestamps")
    if any(not isinstance(timestamps[k], str) for k in _TIMESTAMP_KEYS):
        raise ValueError("split timestamps must be aware ISO strings")
    instants = [_utc(timestamps[k]) for k in _TIMESTAMP_KEYS]
    if (
        any(a >= b for a, b in pairwise(instants))
        or instants[0] != snapshot.min_timestamp
        or instants[-1] != snapshot.max_timestamp
    ):
        raise ValueError(
            "split timestamps must be ordered and bound to snapshot endpoints"
        )
    timestamps = {key: _instant(timestamps[key]) for key in _TIMESTAMP_KEYS}
    recipe = {
        "schema_version": "research-recipe-v1" if legacy else "research-recipe-v2",
        "hypothesis": hypothesis,
        "symbol": request.symbol,
        "frequency": request.frequency,
        "strategy": strategy,
        "execution": execution,
        "backtest": asdict(config),
        "costs": costs,
        "split": split,
        "parameter_optimization": "none",
        "random_seed": 0,
        "dataset_snapshot": snapshot_payload,
        "data_conventions": conventions,
        "code_context": code,
        "split_timestamps": timestamps,
        "screen": {
            "policy_version": "research-screen-v1",
            "max_drawdown_loss_magnitude": request.constraints[
                "max_drawdown_loss_magnitude"
            ],
        },
        "oos_boundary": "UNSEALED_NO_TEST_PERMISSION",
    }
    return FrozenPayload.from_payload(recipe, name="recipe")


class ExperimentSpec(BaseModel):
    """Validated recipe identity; no durable admission or test-use permission."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
        revalidate_instances="always",
    )

    schema_version: Literal["experiment-spec-v1", "experiment-spec-v2"]
    request: ResearchRequest
    recipe: FrozenPayload
    config_hash: str

    @field_validator("recipe", mode="before")
    @classmethod
    def _recipe_copy(cls, value: Any) -> FrozenPayload:
        payload = value.to_payload() if isinstance(value, FrozenPayload) else value
        return FrozenPayload.from_payload(payload, name="recipe")

    @field_validator("config_hash", mode="before")
    @classmethod
    def _config_hash(cls, value: Any) -> str:
        return _digest(value, _SHA256, "config_hash")

    @field_serializer("recipe")
    def _serialize_recipe(self, value: FrozenPayload) -> dict[str, Any]:
        return value.to_payload()

    @model_validator(mode="after")
    def _validate_admitted_body(self) -> Self:
        r = _object(
            self.recipe,
            {
                "schema_version",
                "hypothesis",
                "symbol",
                "frequency",
                "strategy",
                "execution",
                "backtest",
                "costs",
                "split",
                "parameter_optimization",
                "random_seed",
                "dataset_snapshot",
                "data_conventions",
                "code_context",
                "split_timestamps",
                "screen",
                "oos_boundary",
            },
            "recipe",
        )
        expected_recipe = (
            "research-recipe-v1"
            if self.schema_version == "experiment-spec-v1"
            else "research-recipe-v2"
        )
        if r["schema_version"] != expected_recipe:
            raise ValueError("specification and recipe versions must agree")
        hypothesis = _object(
            r["hypothesis"],
            {
                "schema_version",
                "experiment_id",
                "provenance",
                "statement",
                "rationale",
                "falsification",
            },
            "admitted hypothesis",
        )
        proposal = {
            key: r[key]
            for key in (
                "strategy",
                "execution",
                "backtest",
                "costs",
                "split",
                "parameter_optimization",
                "random_seed",
            )
        }
        proposal.update(
            schema_version=(
                "sma-proposal-v1"
                if self.schema_version == "experiment-spec-v1"
                else "strategy-proposal-v2"
            ),
            hypothesis={
                k: hypothesis[k] for k in ("statement", "rationale", "falsification")
            },
        )
        normalized = _normalize_recipe(
            self.request,
            proposal,
            r["dataset_snapshot"],
            r["data_conventions"],
            r["code_context"],
            r["split_timestamps"],
        )
        if normalized.canonical != self.recipe.canonical:
            raise ValueError(
                "recipe is inconsistent with admitted request or canonical rules"
            )
        if self.config_hash != canonical_config_hash(self.to_config()):
            raise ValueError("config_hash does not match the complete admitted recipe")
        return self

    @property
    def experiment_id(self) -> str:
        return self.request.experiment_id

    def to_config(self) -> FrozenPayload:
        """Full replay recipe, suitable for later ExperimentRecord.backtest_config."""
        return FrozenPayload.from_payload(
            {
                "schema_version": self.schema_version,
                "request": self.request.model_dump(mode="json"),
                "recipe": self.recipe.to_payload(),
            },
            name="research configuration",
        )

    def to_json(self) -> str:
        return canonical_json(
            {**self.to_config().to_payload(), "config_hash": self.config_hash}
        )

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            parsed = json.loads(value)
            if canonical_json(parsed) != value:
                raise ValueError("spec must be canonically serialized JSON")
            spec = cls.model_validate(parsed)
            if spec.to_json() != value:
                raise ValueError(
                    "spec interchange must use the canonical normalized body"
                )
            return spec
        except (ValueError, TypeError, OverflowError):
            raise ResearchAdmissionError("invalid frozen recipe interchange") from None
