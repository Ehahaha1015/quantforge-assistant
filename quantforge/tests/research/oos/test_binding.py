from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from quantforge.data import logical_content_hash
from quantforge.research.oos import ResearchSealError, seal_oos_test_use

from ._fixtures import (
    ADMITTED_AT,
    DECLARATION,
    Fixture,
    bars,
    retained_spec,
    revised_spec,
)


@pytest.fixture
def host(tmp_path: Path) -> Any:
    fixture = Fixture(tmp_path)
    yield fixture
    fixture.close()


def test_verified_specimen_uses_actual_retained_data_and_exact_partitions(
    host: Fixture,
) -> None:
    receipt = host.seal()
    assert receipt["state"] == "FIRST_USE_SEALED"
    assert receipt["test_use_allowed"] is True
    assert receipt["fresh_within_retained_history"] is True
    seal = receipt["seal"]
    assert seal["dataset_snapshot"] == host.spec.recipe["dataset_snapshot"]
    assert seal["dataset_snapshot"]["content_hash"] == logical_content_hash(
        host.catalog.read("retained-SPY")
    )
    assert seal["partitions"] == {
        "development": {
            "row_start": 0,
            "row_stop": 756,
            "start": "2020-01-01T14:30:00.000000Z",
            "end": "2022-01-25T14:30:00.000000Z",
        },
        "validation": {
            "row_start": 756,
            "row_stop": 1008,
            "start": "2022-01-26T14:30:00.000000Z",
            "end": "2022-10-04T14:30:00.000000Z",
        },
        "test": {
            "row_start": 1008,
            "row_stop": 1260,
            "start": "2022-10-05T14:30:00.000000Z",
            "end": "2023-06-13T14:30:00.000000Z",
        },
        "validation_windows": [
            {"row_start": 756, "row_stop": 819},
            {"row_start": 819, "row_stop": 882},
            {"row_start": 882, "row_stop": 945},
            {"row_start": 945, "row_stop": 1008},
        ],
    }
    assert host.store.list() == ()


def test_unsealed_spec_and_absent_recovery_grant_no_permission(host: Fixture) -> None:
    assert host.spec.recipe["oos_boundary"] == "UNSEALED_NO_TEST_PERMISSION"
    before = host.file_bytes()
    for operation in (host.verify, lambda: host.seal(mode="recovery")):
        with pytest.raises(ResearchSealError):
            operation()
        assert host.file_bytes() == before and list(host.root.iterdir()) == []


@pytest.mark.parametrize(
    "field",
    [
        "dataset_id",
        "source",
        "schema_version",
        "row_count",
        "min_timestamp",
        "max_timestamp",
        "as_of",
        "content_hash",
        "relative_path",
    ],
)
def test_every_native_snapshot_field_is_bound(host: Fixture, field: str) -> None:
    snapshot = host.spec.recipe["dataset_snapshot"]
    recipe: dict[str, Any] = {"dataset_snapshot": snapshot}
    if field in ("min_timestamp", "max_timestamp"):
        key = "development_start" if field == "min_timestamp" else "test_end"
        value = datetime.fromisoformat(snapshot[field]) + timedelta(seconds=1)
        snapshot[field] = value.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        endpoints = host.spec.recipe["split_timestamps"]
        endpoints[key] = snapshot[field]
        recipe["split_timestamps"] = endpoints
    elif field == "row_count":
        snapshot[field] += 1
        split = host.spec.recipe["split"]
        split["observations"] += 1
        split["test_bars"] += 1
        recipe["split"] = split
    else:
        snapshot[field] = {
            "dataset_id": "missing-retained",
            "source": "modified-source",
            "schema_version": "bars-v2",
            "as_of": "2026-10-09T00:00:00.000000Z",
            "content_hash": "0" * 64,
            "relative_path": "retained-SPY/other.parquet",
        }[field]
    spec = revised_spec(host.spec, recipe=recipe)
    before = host.file_bytes()
    with pytest.raises(ResearchSealError):
        host.seal(spec=spec)
    assert host.file_bytes() == before


@pytest.mark.parametrize(
    "key,shift",
    [
        ("development_end", -1),
        ("validation_start", 1),
        ("validation_end", -1),
        ("test_start", 1),
    ],
)
def test_declared_split_endpoints_must_match_actual_rows(
    host: Fixture, key: str, shift: int
) -> None:
    endpoints = host.spec.recipe["split_timestamps"]
    dt = datetime.fromisoformat(endpoints[key]) + timedelta(hours=shift)
    endpoints[key] = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
    spec = revised_spec(host.spec, recipe={"split_timestamps": endpoints})
    with pytest.raises(ResearchSealError):
        host.seal(spec=spec)
    assert not list(host.root.iterdir())


@pytest.mark.parametrize("target", ["manifest", "parquet", "replacement"])
@pytest.mark.parametrize("mode", ["first_use", "recovery"])
def test_retained_tamper_or_replacement_never_preserves_permission(
    host: Fixture, target: str, mode: str
) -> None:
    if mode == "recovery":
        host.seal()
    seal_bytes = {p.name: p.read_bytes() for p in host.root.rglob("*.json")}
    directory = host.catalog.root / "retained-SPY"
    if target == "manifest":
        (directory / "manifest.json").write_text('{"row_count":1260}', encoding="utf-8")
    elif target == "parquet":
        bars(price=200.0).write_parquet(directory / "bars.parquet")
    else:
        host.catalog.write(
            "retained-SPY",
            bars(price=200.0),
            source="local-synthetic",
            schema_version="bars-v1",
            as_of=datetime(2026, 10, 8, tzinfo=UTC),
            overwrite=True,
        )
    before = host.file_bytes()
    with pytest.raises(ResearchSealError):
        host.seal(mode=mode)
    assert host.file_bytes() == before
    assert {p.name: p.read_bytes() for p in host.root.rglob("*.json")} == seal_bytes


def test_symbol_declaration_is_not_actual_data_proof(host: Fixture) -> None:
    spec = revised_spec(host.spec, request={"symbol": "QQQ"}, recipe={"symbol": "QQQ"})
    with pytest.raises(ResearchSealError):
        host.seal(spec=spec)


def test_rehash_detects_data_changed_between_native_verify_and_read(
    host: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host.catalog, "read", lambda _: bars(price=200.0))
    with pytest.raises(ResearchSealError):
        host.seal()
    assert list(host.root.iterdir()) == []


def test_same_day_intraday_rows_cannot_pose_as_daily_observations(
    host: Fixture,
) -> None:
    frame = bars().with_columns(
        pl.col("timestamp")
        - pl.duration(days=pl.int_range(pl.len()), hours=0)
        + pl.duration(minutes=pl.int_range(pl.len()))
    )
    spec = retained_spec(
        host.catalog, dataset_id="intraday", experiment_id="intraday.001", frame=frame
    )
    with pytest.raises(ResearchSealError):
        host.seal(spec=spec)


@pytest.mark.parametrize(
    "exposure", [None, True, "unknown", "exposed", "llm_says_fresh", {"fresh": True}]
)
def test_missing_unknown_or_external_exposure_is_rejected(
    host: Fixture, exposure: Any
) -> None:
    with pytest.raises(ResearchSealError):
        host.seal(prior_exposure=exposure)
    assert list(host.root.iterdir()) == []


def test_unsafe_native_spec_is_revalidated(host: Fixture) -> None:
    forged = host.spec.model_copy(update={"config_hash": "a" * 64})
    with pytest.raises(ResearchSealError):
        host.seal(spec=forged)
    with pytest.raises(ResearchSealError):
        seal_oos_test_use(
            host.spec.to_config(),
            **host.kwargs(),
            admitted_at=ADMITTED_AT,
            prior_exposure=DECLARATION,
            mode="first_use",
        )
    assert not list(host.root.iterdir())


def test_alias_mutation_cannot_change_durable_or_returned_facts(host: Fixture) -> None:
    receipt = host.seal()
    original = host.seal_path().read_bytes()
    copied = receipt.to_payload()
    copied["test_use_allowed"] = False
    copied["seal"]["partitions"]["test"]["row_start"] = 0
    accessed = receipt["seal"]
    accessed["spec"]["recipe"]["backtest"]["fee_bps"] = 0.0
    assert receipt["seal"]["partitions"]["test"]["row_start"] == 1008
    assert host.seal_path().read_bytes() == original
    assert host.verify()["test_use_allowed"] is False
