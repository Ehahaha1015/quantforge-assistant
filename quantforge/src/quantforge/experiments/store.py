from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from pydantic import ValidationError

from .models import (
    ArtifactRef,
    ExperimentPayloadError,
    ExperimentRecord,
    canonical_json,
)

_COLUMNS = (
    "experiment_id",
    "git_commit",
    "dataset_id",
    "dataset_hash",
    "backtest_config",
    "config_hash",
    "random_seed",
    "started_at",
    "metrics",
    "artifact_refs",
)
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM experiments"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    git_commit TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    dataset_hash TEXT NOT NULL,
    backtest_config TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    random_seed INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    metrics TEXT NOT NULL,
    artifact_refs TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS experiments_append_only_update
BEFORE UPDATE ON experiments
BEGIN
    SELECT RAISE(ABORT, 'experiment records are append-only');
END;
CREATE TRIGGER IF NOT EXISTS experiments_append_only_delete
BEFORE DELETE ON experiments
BEGIN
    SELECT RAISE(ABORT, 'experiment records are append-only');
END;
"""
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


class ExperimentStoreError(Exception):
    """Base class for experiment store failures."""


class DuplicateExperimentError(ExperimentStoreError):
    """An experiment with this identity is already stored."""


class ExperimentNotFoundError(ExperimentStoreError):
    """No stored experiment has this identity."""


class ExperimentIntegrityError(ExperimentStoreError):
    """A stored row is not the canonical record that was persisted."""


def _format_timestamp(value: datetime) -> str:
    return value.strftime(_TIMESTAMP_FORMAT)


def _load_canonical_payload(text: Any, *, name: str) -> Any:
    if not isinstance(text, str):
        raise ExperimentIntegrityError(f"stored {name} is not JSON text")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExperimentIntegrityError(f"stored {name} is not valid JSON") from exc
    try:
        canonical = canonical_json(parsed, name=name)
    except ExperimentPayloadError as exc:
        raise ExperimentIntegrityError(f"stored {name} is not JSON data") from exc
    if canonical != text:
        raise ExperimentIntegrityError(f"stored {name} is not canonically serialized")
    return parsed


def _encode(record: ExperimentRecord) -> tuple[Any, ...]:
    artifact_refs = canonical_json(
        [{"name": reference.name, "uri": reference.uri} for reference in record.artifact_refs],
        name="artifact_refs",
    )
    return (
        record.experiment_id,
        record.git_commit,
        record.dataset_id,
        record.dataset_hash,
        record.backtest_config.canonical,
        record.config_hash,
        record.random_seed,
        _format_timestamp(record.started_at),
        record.metrics.canonical,
        artifact_refs,
    )


def _decode(row: tuple[Any, ...]) -> ExperimentRecord:
    (
        experiment_id,
        git_commit,
        dataset_id,
        dataset_hash,
        config_text,
        config_hash,
        random_seed,
        started_text,
        metrics_text,
        artifact_text,
    ) = row
    config = _load_canonical_payload(config_text, name="backtest_config")
    metrics = _load_canonical_payload(metrics_text, name="metrics")
    artifact_refs = _load_canonical_payload(artifact_text, name="artifact_refs")
    if not isinstance(artifact_refs, list):
        raise ExperimentIntegrityError("stored artifact_refs is not a JSON array")
    try:
        record = ExperimentRecord(
            experiment_id=experiment_id,
            git_commit=git_commit,
            dataset_id=dataset_id,
            dataset_hash=dataset_hash,
            backtest_config=config,
            config_hash=config_hash,
            random_seed=random_seed,
            started_at=started_text,
            metrics=metrics,
            artifact_refs=tuple(
                ArtifactRef.model_validate(entry) for entry in artifact_refs
            ),
        )
    except ValidationError as exc:
        raise ExperimentIntegrityError(
            f"stored experiment is invalid: {experiment_id!r}"
        ) from exc
    except (ExperimentPayloadError, ValueError) as exc:
        raise ExperimentIntegrityError(
            f"stored experiment is invalid: {experiment_id!r}"
        ) from exc
    if _format_timestamp(record.started_at) != started_text:
        raise ExperimentIntegrityError("stored started_at is not canonically formatted")
    return record


class ExperimentStore:
    """Minimal append-only SQLite store for experiment records.

    ``save`` inserts a new immutable record, ``get`` returns one record by
    identity, and ``list`` returns every record in deterministic order.  The
    store exposes no update, overwrite, or delete operation, and the schema
    additionally rejects them so research history cannot be rewritten.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            self.path = str(Path(self.path).expanduser())
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.executescript(_SCHEMA)
        self._connection: sqlite3.Connection | None = connection

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise ExperimentStoreError("experiment store is closed")
        return self._connection

    def save(self, record: ExperimentRecord) -> ExperimentRecord:
        """Persist *record*; a duplicate ``experiment_id`` is rejected."""
        if not isinstance(record, ExperimentRecord):
            raise TypeError("record must be an ExperimentRecord")
        connection = self._require_connection()
        placeholders = ", ".join("?" for _ in _COLUMNS)
        try:
            with connection:
                connection.execute(
                    f"INSERT INTO experiments ({', '.join(_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    _encode(record),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateExperimentError(
                f"experiment already stored: {record.experiment_id}"
            ) from exc
        return record

    def get(self, experiment_id: str) -> ExperimentRecord:
        """Return the stored record for *experiment_id*."""
        if not isinstance(experiment_id, str) or not experiment_id:
            raise ValueError("experiment_id must be a non-empty string")
        connection = self._require_connection()
        row = connection.execute(
            f"{_SELECT} WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        if row is None:
            raise ExperimentNotFoundError(f"experiment not found: {experiment_id}")
        return _decode(row)

    def list(self) -> tuple[ExperimentRecord, ...]:
        """Return every stored record ordered by start time, then identity."""
        connection = self._require_connection()
        rows = connection.execute(
            f"{_SELECT} ORDER BY started_at ASC, experiment_id ASC"
        ).fetchall()
        return tuple(_decode(row) for row in rows)

    def close(self) -> None:
        """Close the underlying connection; further operations fail loudly."""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def __enter__(self) -> Self:
        self._require_connection()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
