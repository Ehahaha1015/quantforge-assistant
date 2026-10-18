"""Retained-root-local OOS admission; no strategy or operational execution.

Seals conservatively spend exposure even before scoring. Host-owned files and
one serial writer are required; checksums are consistency, not authentication.
Consumers must re-enter the gate rather than trust a copied admission receipt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from itertools import combinations, pairwise
from pathlib import Path
from typing import Any, Literal

from quantforge.data import (
    BAR_SCHEMA,
    DatasetSnapshot,
    ParquetDataCatalog,
    logical_content_hash,
    prepare_bars,
)
from quantforge.experiments import ExperimentStore, FrozenPayload, canonical_json

from .models import ExperimentSpec

__all__ = ["ResearchSealError", "seal_oos_test_use", "verify_oos_test_use"]

_SEAL_VERSION = "research-oos-seal-v1"
_GUARD_VERSION = "research-oos-guard-v1"
_DECLARATION = "none_known_outside_factory"
_SPENT = "VALIDATION_TEST_OPPORTUNITY_SPENT"
_SEAL_NAME = "oos-seal.json"
_REF_NAME = "research_oos_seal"
_MAX_COMPONENT_BYTES = 255
_WINDOWS_MAX_PATH = 260
_OUTCOMES = {
    "RESEARCH_SCREEN_PASS",
    "RESEARCH_SCREEN_FAIL",
    "INVALID_RESEARCH",
    "INCOMPLETE",
    "UNSUPPORTED_REQUEST",
}


class ResearchSealError(ValueError):
    """Retained data/history cannot establish the requested OOS authority."""


def _require(condition: bool) -> None:
    if not condition:
        raise ResearchSealError("invalid retained data or OOS history")


def _instant(value: datetime) -> str:
    _require(isinstance(value, datetime))
    _require(value.tzinfo is not None and value.utcoffset() is not None)
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _spec(value: ExperimentSpec) -> ExperimentSpec:
    _require(isinstance(value, ExperimentSpec))
    # Even model_construct/model_copy instances must cross the validated boundary.
    return ExperimentSpec.from_json(value.to_json())


def _payload(value: Mapping[str, Any] | FrozenPayload) -> dict[str, Any]:
    return FrozenPayload.from_payload(value, name="recovery code context").to_payload()


def _not_redirected(path: Path) -> os.stat_result:
    info = path.lstat()
    _require(not stat.S_ISLNK(info.st_mode))
    _require(
        not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    )
    return info


def _local(path: Path, root: Path, *, directory: bool) -> None:
    info = _not_redirected(path)
    _require(path.resolve(strict=True).is_relative_to(root))
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))


def _root(value: str | Path) -> Path:
    _require(isinstance(value, (str, Path)))
    _require(not str(value).startswith(("\\\\", "//")) and "://" not in str(value))
    original = Path(value)
    info = _not_redirected(original)
    _require(stat.S_ISDIR(info.st_mode))
    return original.resolve(strict=True)


def _run_path(root: Path, experiment_id: str) -> Path:
    encoded = (
        base64.b32encode(experiment_id.encode("ascii"))
        .decode("ascii")
        .rstrip("=")
        .lower()
    )
    path = root / ("run-" + encoded)
    _require(len(os.fsencode(path.name)) <= _MAX_COMPONENT_BYTES)
    if os.name == "nt":
        _require(len(str(path / _SEAL_NAME)) < _WINDOWS_MAX_PATH)
    _require(path.parent == root)
    return path


def _snapshot_payload(snapshot: DatasetSnapshot) -> dict[str, Any]:
    result = snapshot.model_dump(mode="json", warnings="error")
    for name in ("min_timestamp", "max_timestamp", "as_of"):
        value = getattr(snapshot, name)
        result[name] = None if value is None else _instant(value)
    return result


def _partitions(spec: ExperimentSpec) -> dict[str, Any]:
    recipe = spec.recipe.to_payload()
    split, times = recipe["split"], recipe["split_timestamps"]
    d, v, n, w = (
        split[k]
        for k in (
            "development_bars",
            "validation_bars",
            "observations",
            "validation_window_bars",
        )
    )
    return {
        "development": {
            "row_start": 0,
            "row_stop": d,
            "start": times["development_start"],
            "end": times["development_end"],
        },
        "validation": {
            "row_start": d,
            "row_stop": d + v,
            "start": times["validation_start"],
            "end": times["validation_end"],
        },
        "test": {
            "row_start": d + v,
            "row_stop": n,
            "start": times["test_start"],
            "end": times["test_end"],
        },
        "validation_windows": [
            {"row_start": start, "row_stop": start + w} for start in range(d, d + v, w)
        ],
    }


def _data_paths(catalog: ParquetDataCatalog, dataset_id: str) -> None:
    root = _root(catalog.root)
    # DATA allows Windows case aliases; the native snapshot owns exact casing.
    directory = root / dataset_id
    _local(directory, root, directory=True)
    for name in ("manifest.json", "bars.parquet"):
        _local(directory / name, root, directory=False)


def _bind_data(spec: ExperimentSpec, catalog: ParquetDataCatalog) -> dict[str, Any]:
    _require(isinstance(catalog, ParquetDataCatalog))
    expected = spec.recipe["dataset_snapshot"]
    dataset_id = expected["dataset_id"]
    _data_paths(catalog, dataset_id)
    snapshot = catalog.verify(dataset_id)
    _require(_snapshot_payload(snapshot) == expected)
    frame = catalog.read(dataset_id)
    _require(frame.schema == BAR_SCHEMA)
    frame = prepare_bars(frame, as_of=snapshot.as_of)
    _require(frame.height == snapshot.row_count)
    _require(logical_content_hash(frame) == snapshot.content_hash)
    _require(frame["symbol"].unique().to_list() == [spec.request.symbol])
    timestamps = frame["timestamp"].to_list()
    _require(all(a < b for a, b in pairwise(timestamps)))
    _require(len({value.date() for value in timestamps}) == len(timestamps))
    partitions = _partitions(spec)
    for name in ("development", "validation", "test"):
        part = partitions[name]
        _require(_instant(timestamps[part["row_start"]]) == part["start"])
        _require(_instant(timestamps[part["row_stop"] - 1]) == part["end"])
    _data_paths(catalog, dataset_id)
    _require(_snapshot_payload(catalog.verify(dataset_id)) == expected)
    _data_paths(catalog, dataset_id)
    return partitions


def _seal(spec: ExperimentSpec, admitted_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": _SEAL_VERSION,
        "guard_version": _GUARD_VERSION,
        "experiment_id": spec.experiment_id,
        "spec": json.loads(spec.to_json()),
        "dataset_snapshot": spec.recipe["dataset_snapshot"],
        "partitions": _partitions(spec),
        "prior_exposure": _DECLARATION,
        "admitted_at": _instant(admitted_at),
        "exposure_state": _SPENT,
    }


def _read_seal(path: Path, root: Path) -> dict[str, Any]:
    _local(path.parent, root, directory=True)
    names = [
        p.name
        for p in path.parent.iterdir()
        if p.name.casefold() == _SEAL_NAME.casefold()
    ]
    _require(names == [_SEAL_NAME])
    _local(path, root, directory=False)
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    _require(isinstance(value, dict))
    spec = ExperimentSpec.from_json(canonical_json(value["spec"]))
    admitted_at = datetime.fromisoformat(value["admitted_at"])
    expected = _seal(spec, admitted_at)
    _require(value == expected and raw == canonical_json(expected).encode("utf-8"))
    expected_path = _run_path(root, spec.experiment_id)
    _require(path.parent == expected_path and path.parent.name == expected_path.name)
    return expected


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ar, br = a["spec"]["recipe"], b["spec"]["recipe"]
    if (ar["symbol"], ar["frequency"]) != (br["symbol"], br["frequency"]):
        return False
    # Only inclusive final-test endpoints participate, never development dates.
    at, bt = a["partitions"]["test"], b["partitions"]["test"]
    return max(at["start"], bt["start"]) <= min(at["end"], bt["end"])


def _history(root: Path) -> dict[str, dict[str, Any]]:
    entries = sorted(
        (p for p in root.iterdir() if p.name.casefold().startswith("run-")),
        key=lambda p: p.name,
    )
    _require(len({p.name.casefold() for p in entries}) == len(entries))
    result: dict[str, dict[str, Any]] = {}
    for directory in entries:
        seal = _read_seal(directory / _SEAL_NAME, root)
        experiment_id = seal["experiment_id"]
        _require(experiment_id not in result)
        result[experiment_id] = seal
    _require(not any(_overlap(a, b) for a, b in combinations(result.values(), 2)))
    return result


def _terminal_history(
    store: ExperimentStore, root: Path, history: dict[str, dict[str, Any]]
) -> tuple[set[str], dict[str, str]]:
    _require(isinstance(store, ExperimentStore))
    ids: set[str] = set()
    outcomes: dict[str, str] = {}
    for record in store.list():
        ids.add(record.experiment_id)
        config, metrics = (
            record.backtest_config.to_payload(),
            record.metrics.to_payload(),
        )
        if (
            config.get("schema_version")
            not in ("experiment-spec-v1", "experiment-spec-v2")
            and "research_factory" not in metrics
        ):
            continue
        spec = ExperimentSpec.from_json(
            canonical_json({**config, "config_hash": record.config_hash})
        )
        seal = history[record.experiment_id]
        recipe = spec.recipe.to_payload()
        _require(
            spec.experiment_id == record.experiment_id
            and seal["spec"] == json.loads(spec.to_json())
        )
        _require(record.git_commit == recipe["code_context"]["git_commit"])
        _require(record.dataset_id == recipe["dataset_snapshot"]["dataset_id"])
        _require(record.dataset_hash == recipe["dataset_snapshot"]["content_hash"])
        _require(record.random_seed == recipe["random_seed"])
        seal_path = _run_path(root, record.experiment_id) / _SEAL_NAME
        refs = [ref for ref in record.artifact_refs if ref.name == _REF_NAME]
        _require(len(refs) == 1 and refs[0].uri == seal_path.as_uri())
        facts = metrics["research_factory"]
        _require(isinstance(facts, dict) and facts["outcome"] in _OUTCOMES)
        _require(
            facts["artifact_integrity"][_REF_NAME]
            == hashlib.sha256(canonical_json(seal).encode("utf-8")).hexdigest()
        )
        outcomes[record.experiment_id] = facts["outcome"]
    return ids, outcomes


def _sync_directory(directory: Path) -> None:
    # Windows has no portable stdlib directory fsync; do not claim that guarantee.
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _publish(path: Path, seal: dict[str, Any]) -> None:
    path.parent.mkdir()  # exclusive reservation; partial evidence is never deleted
    with path.open("xb") as stream:
        raw = canonical_json(seal).encode("utf-8")
        _require(stream.write(raw) == len(raw))
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)
    _sync_directory(path.parent.parent)


def _sync_existing(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)
    _sync_directory(path.parent.parent)


def _receipt(
    seal: dict[str, Any],
    state: str,
    *,
    allowed: bool,
    fresh: bool,
    outcome: str | None = None,
) -> FrozenPayload:
    return FrozenPayload.from_payload(
        {
            "schema_version": "research-oos-admission-v1",
            "experiment_id": seal["experiment_id"],
            "config_hash": seal["spec"]["config_hash"],
            "seal": seal,
            "state": state,
            "test_use_allowed": allowed,
            "fresh_within_retained_history": fresh,
            "terminal_outcome": outcome,
        },
        name="OOS admission receipt",
    )


def _inputs(
    spec: ExperimentSpec,
    catalog: ParquetDataCatalog,
    experiment_store: ExperimentStore,
    artifact_root: str | Path,
    code_context: Mapping[str, Any] | FrozenPayload,
) -> tuple[ExperimentSpec, Path, dict[str, dict[str, Any]], set[str], dict[str, str]]:
    spec = _spec(spec)
    _require(_payload(code_context) == spec.recipe["code_context"])
    root = _root(artifact_root)
    _run_path(root, spec.experiment_id)
    history = _history(root)
    ids, outcomes = _terminal_history(experiment_store, root, history)
    _bind_data(spec, catalog)
    return spec, root, history, ids, outcomes


def seal_oos_test_use(
    spec: ExperimentSpec,
    *,
    catalog: ParquetDataCatalog,
    experiment_store: ExperimentStore,
    artifact_root: str | Path,
    admitted_at: datetime,
    prior_exposure: Literal["none_known_outside_factory"],
    code_context: Mapping[str, Any] | FrozenPayload,
    mode: Literal["first_use", "recovery"],
) -> FrozenPayload:
    """Reserve first exposure or explicitly recover the identical experiment.

    Recovery never creates new freshness. A terminal row prevents reevaluation.
    Interrupted evidence is retained; malformed history requires external repair.
    """
    try:
        _require(prior_exposure == _DECLARATION and mode in ("first_use", "recovery"))
        spec, root, history, ids, outcomes = _inputs(
            spec, catalog, experiment_store, artifact_root, code_context
        )
        expected = _seal(spec, admitted_at)
        path = _run_path(root, spec.experiment_id) / _SEAL_NAME
        if mode == "first_use":
            _require(
                spec.experiment_id not in history and spec.experiment_id not in ids
            )
            _require(not any(_overlap(expected, old) for old in history.values()))
            _publish(path, expected)
            _require(_read_seal(path, root) == expected)
            return _receipt(expected, "FIRST_USE_SEALED", allowed=True, fresh=True)
        _require(
            spec.experiment_id in history and history[spec.experiment_id] == expected
        )
        if spec.experiment_id in ids:
            _require(spec.experiment_id in outcomes)
            return _receipt(
                expected,
                "TERMINAL_NO_TEST_PERMISSION",
                allowed=False,
                fresh=False,
                outcome=outcomes[spec.experiment_id],
            )
        _sync_existing(path)
        _require(_read_seal(path, root) == expected)
        return _receipt(expected, "SAME_EXPERIMENT_RECOVERY", allowed=True, fresh=False)
    except Exception:  # noqa: BLE001 -- Public fail-closed boundary sanitizes dependency errors.
        raise ResearchSealError("OOS test-use admission denied") from None


def verify_oos_test_use(
    spec: ExperimentSpec,
    *,
    catalog: ParquetDataCatalog,
    experiment_store: ExperimentStore,
    artifact_root: str | Path,
    code_context: Mapping[str, Any] | FrozenPayload,
) -> FrozenPayload:
    """Read and validate retained authority; this inspection grants no test use."""
    try:
        spec, _checked_root, history, _, outcomes = _inputs(
            spec, catalog, experiment_store, artifact_root, code_context
        )
        seal = history[spec.experiment_id]
        _require(seal["spec"] == json.loads(spec.to_json()))
        return _receipt(
            seal,
            "VERIFIED_NO_TEST_PERMISSION",
            allowed=False,
            fresh=False,
            outcome=outcomes.get(spec.experiment_id),
        )
    except Exception:  # noqa: BLE001 -- Public fail-closed boundary sanitizes dependency errors.
        raise ResearchSealError("OOS seal verification denied") from None
