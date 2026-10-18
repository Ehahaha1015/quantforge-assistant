from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.data import ParquetDataCatalog
from quantforge.experiments import (
    ArtifactRef,
    DuplicateExperimentError,
    ExperimentIntegrityError,
    ExperimentNotFoundError,
    ExperimentPayloadError,
    ExperimentRecord,
    ExperimentStore,
    ExperimentStoreError,
    canonical_config_hash,
    canonical_json,
)

COMMIT = "23454c86f2b13efa38ae838eaa9b64d77e20bfee"
OTHER_COMMIT = "bff7fabf27d64557963a8f527f86f0ecee46ab82"
DATASET_HASH = "d4e5af19193071651d243efdadfefb1f1eca057a664eb5f43b26754ed5f9fd5f"
STARTED_AT = datetime(2025, 1, 4, 12, 0, tzinfo=UTC)
DEFAULT_CONFIG = {
    "initial_cash": 100_000.0,
    "fee_bps": 2.5,
    "periods_per_year": 252,
}
DEFAULT_METRICS = {
    "total_return": 0.0125,
    "annualized_volatility": 0.19,
    "sharpe": 1.5,
    "max_drawdown": -0.02,
    "turnover": 3.0,
    "samples": 42,
    "label": "unit-test",
}


def make_record(**overrides: object) -> ExperimentRecord:
    values: dict[str, object] = {
        "experiment_id": "exp-001",
        "git_commit": COMMIT,
        "dataset_id": "bars.001",
        "dataset_hash": DATASET_HASH,
        "backtest_config": DEFAULT_CONFIG,
        "random_seed": 7,
        "started_at": STARTED_AT,
        "metrics": DEFAULT_METRICS,
        "artifact_refs": (
            ArtifactRef(name="equity-curve", uri="artifacts/exp-001/equity.parquet"),
        ),
    }
    values.update(overrides)
    return ExperimentRecord.create(**values)  # type: ignore[arg-type]


def make_store(tmp_path: Path) -> ExperimentStore:
    return ExperimentStore(tmp_path / "experiments.sqlite")


def bar_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["MSFT", "AAPL", "AAPL"],
            "timestamp": [
                datetime(2025, 1, 2, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
                datetime(2025, 1, 1, tzinfo=UTC),
            ],
            "open": [20.0, 11.0, 10.0],
            "high": [22.0, 13.0, 12.0],
            "low": [19.0, 10.0, 9.0],
            "close": [21.0, 12.0, 11.0],
            "volume": [200.0, 110.0, 100.0],
        }
    )


def rewrite_column(tmp_path: Path, experiment_id: str, column: str, value: str) -> None:
    connection = sqlite3.connect(tmp_path / "experiments.sqlite")
    try:
        with connection:
            connection.execute("DROP TRIGGER experiments_append_only_update")
            connection.execute(
                f"UPDATE experiments SET {column} = ? WHERE experiment_id = ?",
                (value, experiment_id),
            )
    finally:
        connection.close()


def test_save_and_get_round_trip(tmp_path: Path) -> None:
    record = make_record()
    with make_store(tmp_path) as store:
        assert store.save(record) == record
        loaded = store.get("exp-001")
    assert loaded == record
    assert loaded.experiment_id == "exp-001"
    assert loaded.backtest_config.to_payload() == DEFAULT_CONFIG
    assert loaded.metrics.to_payload() == DEFAULT_METRICS
    assert loaded.artifact_refs == record.artifact_refs
    assert loaded.config_hash == canonical_config_hash(DEFAULT_CONFIG)


def test_records_survive_store_reopen(tmp_path: Path) -> None:
    record = make_record()
    store = make_store(tmp_path)
    store.save(record)
    store.close()

    reopened = make_store(tmp_path)
    try:
        assert reopened.get("exp-001") == record
    finally:
        reopened.close()


def test_list_is_deterministic_and_chronological(tmp_path: Path) -> None:
    late = make_record(experiment_id="exp-late", started_at=STARTED_AT + timedelta(hours=2))
    early = make_record(experiment_id="exp-early", started_at=STARTED_AT)
    middle = make_record(experiment_id="exp-middle", started_at=STARTED_AT + timedelta(hours=1))

    store = make_store(tmp_path)
    for record in (late, middle, early):
        store.save(record)
    assert [item.experiment_id for item in store.list()] == [
        "exp-early",
        "exp-middle",
        "exp-late",
    ]
    assert store.list() == store.list()
    store.close()

    reopened = make_store(tmp_path)
    try:
        assert [item.experiment_id for item in reopened.list()] == [
            "exp-early",
            "exp-middle",
            "exp-late",
        ]
    finally:
        reopened.close()


def test_list_ties_break_on_experiment_id(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    for experiment_id in ("exp-c", "exp-a", "exp-b"):
        store.save(make_record(experiment_id=experiment_id))
    assert [item.experiment_id for item in store.list()] == ["exp-a", "exp-b", "exp-c"]
    store.close()


def test_duplicate_experiment_id_is_rejected(tmp_path: Path) -> None:
    original = make_record()
    with make_store(tmp_path) as store:
        store.save(original)
        duplicate = make_record(metrics={"total_return": 9.75})
        with pytest.raises(DuplicateExperimentError, match="exp-001"):
            store.save(duplicate)
        assert store.get("exp-001") == original
        assert len(store.list()) == 1


def test_config_mapping_order_does_not_change_hash() -> None:
    first = {"b": {"y": 2, "x": 1}, "a": 1, "c": [1, 2, 3]}
    second = {"c": (1, 2, 3), "a": 1, "b": {"x": 1, "y": 2}}
    assert canonical_json(first) == canonical_json(second)
    assert canonical_config_hash(first) == canonical_config_hash(second)
    left = make_record(experiment_id="exp-left", backtest_config=first)
    right = make_record(experiment_id="exp-right", backtest_config=second)
    assert left.config_hash == right.config_hash


@pytest.mark.parametrize(
    "changed",
    [
        {"initial_cash": 100_001.0, "fee_bps": 2.5, "periods_per_year": 252},
        {"initial_cash": 100_000.0, "fee_bps": 2.5, "periods_per_year": 365},
        {"initial_cash": 100_000.0, "fee_bps": 2.5, "periods_per_year": 252, "extra": 1},
    ],
)
def test_config_change_changes_hash(changed: dict[str, object]) -> None:
    assert canonical_config_hash(DEFAULT_CONFIG) != canonical_config_hash(changed)
    record = make_record(backtest_config=changed)
    assert record.config_hash != make_record().config_hash


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_config_is_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        make_record(backtest_config={"threshold": value})
    with pytest.raises(ValueError):
        make_record(backtest_config={"nested": {"values": [1.0, value]}})
    with pytest.raises(ValueError):
        canonical_config_hash({"threshold": value})


def test_non_finite_json_text_is_rejected() -> None:
    # Python's json module parses NaN/Infinity by default; the payload must
    # still be rejected before it can be persisted.
    payload = json.loads('{"threshold": Infinity, "other": NaN}')
    with pytest.raises(ValueError):
        make_record(backtest_config=payload)
    with pytest.raises(ValueError):
        make_record(metrics=payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_metrics_are_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        make_record(metrics={"sharpe": value})
    with pytest.raises(ValueError):
        make_record(metrics={"nested": [{"value": value}]})


@pytest.mark.parametrize(
    "value",
    [
        {"when": datetime(2025, 1, 4, tzinfo=UTC)},
        {"items": {1, 2}},
        {"items": b"bytes"},
        {1: "non-string key"},
    ],
)
def test_non_json_payloads_are_rejected(value: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        make_record(backtest_config=value)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_record(started_at=STARTED_AT.replace(tzinfo=None))


def test_timezone_aware_timestamps_are_normalized_to_utc(tmp_path: Path) -> None:
    local_zone = timezone(timedelta(hours=8))
    record = make_record(started_at=datetime(2025, 1, 4, 20, 0, tzinfo=local_zone))
    assert record.started_at == STARTED_AT
    assert record.started_at.tzinfo is UTC
    with make_store(tmp_path) as store:
        store.save(record)
        loaded = store.get("exp-001")
    assert loaded.started_at == STARTED_AT
    assert loaded.started_at.utcoffset() == timedelta(0)


def test_metrics_round_trip_preserves_nested_values(tmp_path: Path) -> None:
    metrics = {
        "total_return": -0.0042,
        "counts": [1, 2, 3],
        "nested": {"ok": True, "missing": None, "note": "unicode-\u00b1"},
        "large": 2**53 + 1,
    }
    with make_store(tmp_path) as store:
        store.save(make_record(metrics=metrics))
        assert store.get("exp-001").metrics.to_payload() == metrics


def test_artifact_refs_round_trip_preserves_order(tmp_path: Path) -> None:
    refs = (
        ArtifactRef(name="equity-curve", uri="artifacts/exp-001/equity.parquet"),
        ArtifactRef(name="trades", uri="artifacts/exp-001/trades.csv"),
        {"name": "report", "uri": "artifacts/exp-001/report.md"},
    )
    with make_store(tmp_path) as store:
        store.save(make_record(artifact_refs=refs))
        loaded = store.get("exp-001")
    assert [reference.name for reference in loaded.artifact_refs] == [
        "equity-curve",
        "trades",
        "report",
    ]
    assert loaded.artifact_refs[0].uri == "artifacts/exp-001/equity.parquet"


def test_artifact_refs_validation() -> None:
    assert make_record(artifact_refs=()).artifact_refs == ()
    with pytest.raises(ValidationError, match="repeat"):
        make_record(
            artifact_refs=[
                {"name": "trades", "uri": "artifacts/trades.csv"},
                {"name": "trades", "uri": "artifacts/trades-again.csv"},
            ]
        )
    with pytest.raises(ValidationError, match="artifact name"):
        make_record(artifact_refs=[{"name": "../escape", "uri": "artifacts/trades.csv"}])
    with pytest.raises(ValidationError, match="trimmed"):
        make_record(artifact_refs=[{"name": "trades", "uri": " artifacts/trades.csv "}])


def test_git_commit_is_preserved_exactly(tmp_path: Path) -> None:
    record = make_record(git_commit=OTHER_COMMIT)
    with make_store(tmp_path) as store:
        store.save(record)
        assert store.get("exp-001").git_commit == OTHER_COMMIT
        assert store.get("exp-001").git_commit != COMMIT
    with make_store(tmp_path) as store:
        assert store.get("exp-001").git_commit == OTHER_COMMIT


def test_git_commit_must_be_a_full_lowercase_sha() -> None:
    for invalid in ("abc1234", OTHER_COMMIT.upper(), COMMIT[:-1], ""):
        with pytest.raises(ValidationError, match="git_commit"):
            make_record(git_commit=invalid)


def test_dataset_provenance_is_preserved(tmp_path: Path) -> None:
    record = make_record(dataset_id="bars.MSFT-1d", dataset_hash=DATASET_HASH)
    with make_store(tmp_path) as store:
        store.save(record)
        loaded = store.get("exp-001")
    assert loaded.dataset_id == "bars.MSFT-1d"
    assert loaded.dataset_hash == DATASET_HASH


def test_dataset_identity_validation_reuses_data_rules() -> None:
    for invalid_id in ("..", "CON", "nul.txt", "a/b", "bad id"):
        with pytest.raises(ValidationError):
            make_record(dataset_id=invalid_id)
    for invalid_hash in ("", "ABC", DATASET_HASH.upper(), DATASET_HASH[:-1]):
        with pytest.raises(ValidationError, match="dataset_hash"):
            make_record(dataset_hash=invalid_hash)


def test_distinct_experiments_remain_isolated(tmp_path: Path) -> None:
    first = make_record()
    second = make_record(
        experiment_id="exp-002",
        dataset_id="bars.002",
        git_commit=OTHER_COMMIT,
        backtest_config={**DEFAULT_CONFIG, "fee_bps": 5.0},
        metrics={"total_return": 0.5},
        random_seed=11,
        started_at=STARTED_AT + timedelta(days=1),
    )
    with make_store(tmp_path) as store:
        store.save(first)
        store.save(second)
        assert store.get("exp-001") == first
        assert store.get("exp-002") == second
        assert [item.experiment_id for item in store.list()] == ["exp-001", "exp-002"]


def test_reopen_does_not_mutate_records(tmp_path: Path) -> None:
    record = make_record()
    store = make_store(tmp_path)
    store.save(record)
    store.close()
    database = tmp_path / "experiments.sqlite"
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    reopened = make_store(tmp_path)
    try:
        assert reopened.get("exp-001") == record
        assert reopened.list() == (record,)
    finally:
        reopened.close()

    after = hashlib.sha256(database.read_bytes()).hexdigest()
    assert after == before
    with make_store(tmp_path) as verified:
        assert verified.get("exp-001") == record


def test_stored_records_cannot_be_mutated_through_payload_aliases(tmp_path: Path) -> None:
    with make_store(tmp_path) as store:
        record = make_record()
        store.save(record)
        loaded = store.get("exp-001")
        payload = loaded.backtest_config.to_payload()
        payload["initial_cash"] = 1.0
        payload["nested"] = {"injected": True}
        assert store.get("exp-001").backtest_config.to_payload() == DEFAULT_CONFIG
        with pytest.raises(TypeError):
            loaded.backtest_config["initial_cash"] = 1.0  # type: ignore[index]
        with pytest.raises(ValidationError):
            loaded.metrics = "replaced"  # type: ignore[assignment]


def test_frozen_record_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ExperimentRecord(
            experiment_id="exp-001",
            git_commit=COMMIT,
            dataset_id="bars.001",
            dataset_hash=DATASET_HASH,
            backtest_config=DEFAULT_CONFIG,
            config_hash=canonical_config_hash(DEFAULT_CONFIG),
            random_seed=7,
            started_at=STARTED_AT,
            metrics=DEFAULT_METRICS,
            unexpected="field",
        )


def test_config_hash_must_match_the_canonical_config() -> None:
    with pytest.raises(ValidationError, match="config_hash"):
        ExperimentRecord(
            experiment_id="exp-001",
            git_commit=COMMIT,
            dataset_id="bars.001",
            dataset_hash=DATASET_HASH,
            backtest_config=DEFAULT_CONFIG,
            config_hash="0" * 64,
            random_seed=7,
            started_at=STARTED_AT,
            metrics=DEFAULT_METRICS,
        )
    with pytest.raises(ExperimentPayloadError, match="config_hash"):
        make_record(config_hash="1" * 64)


@pytest.mark.parametrize("invalid", [True, False, "7", 2.0, 2**63, -(2**63) - 1])
def test_random_seed_validation(invalid: object) -> None:
    with pytest.raises(ValidationError):
        make_record(random_seed=invalid)
    assert make_record(random_seed=-(2**63)).random_seed == -(2**63)
    assert make_record(random_seed=2**63 - 1).random_seed == 2**63 - 1


def test_record_round_trips_through_pydantic_json() -> None:
    record = make_record()
    payload = record.model_dump_json()
    assert json.loads(payload)["backtest_config"] == DEFAULT_CONFIG
    assert ExperimentRecord.model_validate_json(payload) == record


def test_missing_experiment_raises_not_found(tmp_path: Path) -> None:
    with make_store(tmp_path) as store, pytest.raises(ExperimentNotFoundError, match="missing"):
        store.get("missing")


def test_store_is_append_only_at_the_storage_layer(tmp_path: Path) -> None:
    with make_store(tmp_path) as store:
        store.save(make_record())
        for name in ("update", "overwrite", "delete", "remove"):
            assert not hasattr(store, name)

    connection = sqlite3.connect(tmp_path / "experiments.sqlite")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE experiments SET config_hash = 'tampered'")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM experiments")
        connection.rollback()
        count = connection.execute("SELECT COUNT(*) FROM experiments").fetchone()
    finally:
        connection.close()
    assert count == (1,)


def test_closed_store_rejects_further_operations(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save(make_record())
    store.close()
    store.close()
    with pytest.raises(ExperimentStoreError, match="closed"):
        store.get("exp-001")
    with pytest.raises(ExperimentStoreError, match="closed"):
        store.list()
    with pytest.raises(ExperimentStoreError, match="closed"):
        store.save(make_record(experiment_id="exp-002"))


def test_corrupted_rows_are_rejected(tmp_path: Path) -> None:
    with make_store(tmp_path) as store:
        store.save(make_record())

    rewrite_column(tmp_path, "exp-001", "config_hash", "0" * 64)
    with make_store(tmp_path) as store, pytest.raises(ExperimentIntegrityError):
        store.get("exp-001")

    rewrite_column(
        tmp_path, "exp-001", "backtest_config", '{"fee_bps": 2.5, "initial_cash": 1}'
    )
    with make_store(tmp_path) as store, pytest.raises(ExperimentIntegrityError):
        store.get("exp-001")

    rewrite_column(tmp_path, "exp-001", "metrics", '{"sharpe": NaN}')
    with make_store(tmp_path) as store, pytest.raises(ExperimentIntegrityError):
        store.get("exp-001")


def test_replay_descriptor_exposes_reproducibility_provenance(tmp_path: Path) -> None:
    record = make_record()
    with make_store(tmp_path) as store:
        store.save(record)
        descriptor = store.get("exp-001").replay_descriptor()
    assert descriptor == {
        "experiment_id": "exp-001",
        "git_commit": COMMIT,
        "dataset_id": "bars.001",
        "dataset_hash": DATASET_HASH,
        "backtest_config": DEFAULT_CONFIG,
        "config_hash": canonical_config_hash(DEFAULT_CONFIG),
        "random_seed": 7,
    }


def test_in_memory_store_supports_the_same_contract() -> None:
    with ExperimentStore(":memory:") as store:
        record = make_record()
        store.save(record)
        assert store.get("exp-001") == record
        assert store.list() == (record,)


def test_record_from_dataset_snapshot_keeps_data_hash_semantics(tmp_path: Path) -> None:
    catalog = ParquetDataCatalog(tmp_path / "catalog")
    snapshot = catalog.write(
        "bars.001",
        bar_frame(),
        source="unit-test",
        schema_version="1",
        as_of=datetime(2025, 1, 4, tzinfo=UTC),
    )
    record = make_record(dataset_id=snapshot.dataset_id, dataset_hash=snapshot.content_hash)
    with make_store(tmp_path) as store:
        store.save(record)
        loaded = store.get("exp-001")
    assert loaded.dataset_id == snapshot.dataset_id
    assert loaded.dataset_hash == snapshot.content_hash
    assert loaded.replay_descriptor()["dataset_hash"] == snapshot.content_hash


def test_record_from_backtest_result_round_trips(tmp_path: Path) -> None:
    config = BacktestConfig(initial_cash=1_000.0, fee_bps=10.0)
    bars = bar_frame().filter(pl.col("symbol") == "AAPL")
    signals = pl.DataFrame(
        {
            "symbol": ["AAPL", "AAPL"],
            "timestamp": [
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
            ],
            "value": [0.0, 1.0],
        }
    ).with_columns(
        pl.col("timestamp").cast(pl.Datetime("us", "UTC")),
        pl.col("value").cast(pl.Float64),
    )
    result = run_vectorized_backtest(bars, signals, config)
    metrics = dataclasses.asdict(result.metrics)
    record = make_record(
        backtest_config=dataclasses.asdict(config),
        metrics=metrics,
    )
    assert record.config_hash == canonical_config_hash(dataclasses.asdict(config))
    with make_store(tmp_path) as store:
        store.save(record)
        loaded = store.get("exp-001")
    assert loaded.metrics.to_payload() == metrics
    assert loaded.backtest_config.to_payload() == dataclasses.asdict(config)
