from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from ..data import validate_dataset_id

_EXPERIMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_IDENTIFIER_LENGTH = 128
_MAX_URI_LENGTH = 2048
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class ExperimentPayloadError(ValueError):
    """A configuration, metrics, or artifact payload is not canonical JSON data."""


def _plain_json(value: Any, *, name: str) -> Any:
    """Validate *value* as plain, finite JSON data and return a fresh copy."""
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str) or not key:
                raise ExperimentPayloadError(
                    f"{name} object keys must be non-empty strings"
                )
        return {key: _plain_json(item, name=name) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item, name=name) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExperimentPayloadError(
                f"{name} must not contain non-finite numbers (NaN or Infinity)"
            )
        return value
    raise ExperimentPayloadError(
        f"{name} must contain only JSON values, got {type(value).__name__}"
    )


def canonical_json(value: Any, *, name: str = "payload") -> str:
    """Serialize JSON data canonically.

    Keys are sorted, separators are compact, tuples become arrays, and
    non-finite numbers, non-string keys, and non-JSON values are rejected so
    that equivalent values always serialize to the same text.
    """
    plain = _plain_json(value, name=name)
    return json.dumps(
        plain, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


class FrozenPayload(Mapping[str, Any]):
    """Immutable canonical JSON object payload.

    The payload is validated and canonicalized on construction, and every
    accessor hands out a fresh copy so a stored record can never be mutated
    through an aliased dictionary or list.
    """

    __slots__ = ("_canonical", "_parsed")

    def __init__(self, canonical: str) -> None:
        if not isinstance(canonical, str):
            raise ExperimentPayloadError("payload must be canonical JSON text")
        try:
            parsed = json.loads(canonical)
        except json.JSONDecodeError as exc:
            raise ExperimentPayloadError("payload must be canonical JSON text") from exc
        if not isinstance(parsed, dict):
            raise ExperimentPayloadError("payload must be a JSON object")
        if canonical_json(parsed) != canonical:
            raise ExperimentPayloadError("payload must be canonically serialized JSON")
        self._canonical = canonical
        self._parsed = parsed

    @classmethod
    def from_payload(cls, value: Any, *, name: str) -> FrozenPayload:
        if isinstance(value, FrozenPayload):
            return value
        if not isinstance(value, Mapping):
            raise ExperimentPayloadError(f"{name} must be a JSON object")
        return cls(canonical_json(value, name=name))

    @property
    def canonical(self) -> str:
        """The canonical JSON text backing this payload."""
        return self._canonical

    def to_payload(self) -> dict[str, Any]:
        """Return a fresh mutable copy of the payload."""
        return copy.deepcopy(self._parsed)

    def __getitem__(self, key: str) -> Any:
        return copy.deepcopy(self._parsed[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._parsed)

    def __len__(self) -> int:
        return len(self._parsed)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FrozenPayload):
            return self._canonical == other._canonical
        if isinstance(other, Mapping):
            try:
                normalized = _plain_json(dict(other), name="payload")
            except ExperimentPayloadError:
                return NotImplemented
            return self._parsed == normalized
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._canonical)

    def __repr__(self) -> str:
        return f"FrozenPayload({self._canonical})"


def canonical_config_hash(config: Mapping[str, Any] | FrozenPayload) -> str:
    """Derive the deterministic SHA-256 digest of a canonical config payload."""
    if isinstance(config, FrozenPayload):
        canonical = config.canonical
    else:
        canonical = canonical_json(config, name="backtest_config")
    return sha256(canonical.encode("utf-8")).hexdigest()


class ArtifactRef(BaseModel):
    """Reference to one artifact produced by an experiment run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    uri: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) > _MAX_IDENTIFIER_LENGTH
            or _ARTIFACT_NAME.fullmatch(value) is None
        ):
            raise ValueError(
                "artifact name must be ASCII letters, digits, '.', '_' or '-'"
            )
        return value

    @field_validator("uri")
    @classmethod
    def _uri(cls, value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > _MAX_URI_LENGTH
            or value != value.strip()
        ):
            raise ValueError("artifact uri must be a non-empty, trimmed string")
        if any(ord(character) < 32 for character in value):
            raise ValueError("artifact uri must not contain control characters")
        return value


class ExperimentRecord(BaseModel):
    """Canonical immutable record of one research run.

    The record identifies the exact code commit, dataset snapshot, backtest
    configuration, random seed, result metrics, and artifact references that
    belong to one experiment.  ``config_hash`` is always the deterministic
    digest of the canonical ``backtest_config`` payload.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, arbitrary_types_allowed=True
    )

    experiment_id: str
    git_commit: str
    dataset_id: str
    dataset_hash: str
    backtest_config: FrozenPayload
    config_hash: str
    random_seed: int = Field(strict=True)
    started_at: datetime
    metrics: FrozenPayload
    artifact_refs: tuple[ArtifactRef, ...] = ()

    @field_validator("experiment_id")
    @classmethod
    def _experiment_id(cls, value: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) > _MAX_IDENTIFIER_LENGTH
            or _EXPERIMENT_ID.fullmatch(value) is None
        ):
            raise ValueError(
                "experiment_id must use ASCII letters, digits, '.', '_' or '-'"
            )
        return value

    @field_validator("git_commit")
    @classmethod
    def _git_commit(cls, value: str) -> str:
        if not isinstance(value, str) or _GIT_COMMIT.fullmatch(value) is None:
            raise ValueError(
                "git_commit must be a full lowercase 40-character git SHA"
            )
        return value

    @field_validator("dataset_id")
    @classmethod
    def _dataset_id(cls, value: str) -> str:
        return validate_dataset_id(value)

    @field_validator("dataset_hash")
    @classmethod
    def _dataset_hash(cls, value: str) -> str:
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ValueError(
                "dataset_hash must be a lowercase SHA-256 hexadecimal digest"
            )
        return value

    @field_validator("config_hash")
    @classmethod
    def _config_hash(cls, value: str) -> str:
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ValueError(
                "config_hash must be a lowercase SHA-256 hexadecimal digest"
            )
        return value

    @field_validator("random_seed")
    @classmethod
    def _random_seed(cls, value: int) -> int:
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise ValueError("random_seed must fit a signed 64-bit integer")
        return value

    @field_validator("started_at")
    @classmethod
    def _started_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        try:
            return value.astimezone(UTC)
        except (OverflowError, ValueError) as exc:
            raise ValueError("started_at is outside the representable UTC range") from exc

    @field_validator("backtest_config", mode="before")
    @classmethod
    def _backtest_config(cls, value: Any) -> FrozenPayload:
        return FrozenPayload.from_payload(value, name="backtest_config")

    @field_validator("metrics", mode="before")
    @classmethod
    def _metrics(cls, value: Any) -> FrozenPayload:
        return FrozenPayload.from_payload(value, name="metrics")

    @field_validator("artifact_refs", mode="after")
    @classmethod
    def _artifact_refs(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        names = [reference.name for reference in value]
        if len(set(names)) != len(names):
            raise ValueError("artifact_refs must not repeat an artifact name")
        return value

    @field_serializer("backtest_config", "metrics")
    def _serialize_payload(self, value: FrozenPayload) -> dict[str, Any]:
        return value.to_payload()

    @model_validator(mode="after")
    def _verify_config_hash(self) -> ExperimentRecord:
        if self.config_hash != canonical_config_hash(self.backtest_config):
            raise ValueError(
                "config_hash does not match the canonical backtest_config payload"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        experiment_id: str,
        git_commit: str,
        dataset_id: str,
        dataset_hash: str,
        backtest_config: Mapping[str, Any] | FrozenPayload,
        random_seed: int,
        started_at: datetime,
        metrics: Mapping[str, Any] | FrozenPayload,
        artifact_refs: Iterable[ArtifactRef | Mapping[str, Any]] = (),
        config_hash: str | None = None,
    ) -> ExperimentRecord:
        """Build a record, deriving ``config_hash`` from the canonical config."""
        payload = FrozenPayload.from_payload(backtest_config, name="backtest_config")
        derived = canonical_config_hash(payload)
        if config_hash is not None and config_hash != derived:
            raise ExperimentPayloadError(
                "config_hash does not match the canonical backtest_config payload"
            )
        return cls(
            experiment_id=experiment_id,
            git_commit=git_commit,
            dataset_id=dataset_id,
            dataset_hash=dataset_hash,
            backtest_config=payload,
            config_hash=derived,
            random_seed=random_seed,
            started_at=started_at,
            metrics=FrozenPayload.from_payload(metrics, name="metrics"),
            artifact_refs=tuple(
                ArtifactRef.model_validate(reference) for reference in artifact_refs
            ),
        )

    def replay_descriptor(self) -> dict[str, Any]:
        """Provenance sufficient to build a future replay request.

        EXPERIMENT-001 records this descriptor only; it never executes a
        replay and makes no claim that the described run can be re-executed
        until the strategy/run layer exists.
        """
        return {
            "experiment_id": self.experiment_id,
            "git_commit": self.git_commit,
            "dataset_id": self.dataset_id,
            "dataset_hash": self.dataset_hash,
            "backtest_config": self.backtest_config.to_payload(),
            "config_hash": self.config_hash,
            "random_seed": self.random_seed,
        }
