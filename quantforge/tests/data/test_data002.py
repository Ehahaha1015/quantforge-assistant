from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from quantforge.data import (
    DatasetIntegrityError,
    ParquetDataCatalog,
    validate_dataset_id,
)

AS_OF = datetime(2025, 1, 4, tzinfo=UTC)


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


def write_dataset(catalog: ParquetDataCatalog, dataset_id: str = "bars.001"):
    return catalog.write(
        dataset_id,
        bar_frame(),
        source="unit-test",
        schema_version="1",
        as_of=AS_OF,
    )


def test_verify_is_the_trust_boundary_for_round_trip(tmp_path: Path) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    snapshot = write_dataset(catalog)
    assert catalog.verify("bars.001") == snapshot
    assert catalog.read_snapshot("bars.001") == snapshot
    assert catalog.read("bars.001").height == 3
    assert catalog.query("bars.001").height == 3


def test_failed_staging_leaves_new_dataset_absent_and_other_data_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog, "unrelated")

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("injected staging failure")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", fail_write)
    with pytest.raises(OSError, match="injected staging failure"):
        write_dataset(catalog, "new-data")
    assert not (tmp_path / "new-data").exists()
    assert catalog.verify("unrelated").dataset_id == "unrelated"


def test_failed_publication_does_not_expose_partial_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = ParquetDataCatalog(tmp_path)

    def fail_publish(*args: object, **kwargs: object) -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr(catalog, "_publish_transaction", fail_publish)
    with pytest.raises(OSError, match="injected publication failure"):
        write_dataset(catalog, "new-data")
    assert not (tmp_path / "new-data").exists()
    assert not list((tmp_path / ".qf_transactions").glob("*/"))


def test_failure_after_candidate_promotion_removes_uncommitted_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = ParquetDataCatalog(tmp_path)

    def fail_publish(tx: Path, dataset_id: str, directory: Path, *, overwrite: bool) -> None:
        os.replace(tx / "candidate", directory)
        raise OSError("failure after promotion")

    monkeypatch.setattr(catalog, "_publish_transaction", fail_publish)
    with pytest.raises(OSError, match="failure after promotion"):
        write_dataset(catalog, "new-data")
    assert not (tmp_path / "new-data").exists()


def test_failed_create_cleanup_failure_preserves_uncommitted_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = ParquetDataCatalog(tmp_path)

    def fail_publish(tx: Path, dataset_id: str, directory: Path, *, overwrite: bool) -> None:
        os.replace(tx / "candidate", directory)
        raise OSError("failure after promotion")

    original_remove = ParquetDataCatalog._remove_tree

    def fail_final_cleanup(self: ParquetDataCatalog, path: Path) -> None:
        if path == tmp_path / "new-data":
            raise OSError("injected final cleanup failure")
        original_remove(self, path)

    monkeypatch.setattr(catalog, "_publish_transaction", fail_publish)
    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", fail_final_cleanup)
    with pytest.raises(OSError, match="injected final cleanup failure"):
        write_dataset(catalog, "new-data")
    assert (tmp_path / "new-data" / "manifest.json").is_file()
    assert list((tmp_path / ".qf_transactions").glob("*/"))

    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", original_remove)
    restarted = ParquetDataCatalog(tmp_path)
    with pytest.raises(FileNotFoundError):
        restarted.verify("new-data")
    assert not (tmp_path / "new-data").exists()
    assert not list((tmp_path / ".qf_transactions").glob("*/"))
    with pytest.raises(FileNotFoundError):
        ParquetDataCatalog(tmp_path).verify("new-data")


def test_stale_failed_create_transaction_cannot_delete_later_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    tx = tmp_path / ".qf_transactions" / "stale-create"
    tx.mkdir(parents=True)
    (tx / "state.json").write_text(
        json.dumps({"dataset_id": "new-data", "phase": "prepared", "had_existing": False}),
        encoding="utf-8",
    )
    original_remove = ParquetDataCatalog._remove_tree

    def fail_stale_cleanup(self: ParquetDataCatalog, path: Path) -> None:
        if path == tx:
            raise OSError("stale cleanup failure")
        original_remove(self, path)

    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", fail_stale_cleanup)
    with pytest.raises(DatasetIntegrityError, match="unresolved transaction"):
        write_dataset(catalog, "new-data")
    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", original_remove)
    committed = write_dataset(catalog, "new-data")
    assert catalog.verify("new-data") == committed


def test_published_cleanup_artifacts_cannot_rollback_tampered_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    original_remove = ParquetDataCatalog._remove_tree

    def fail_cleanup(self: ParquetDataCatalog, path: Path) -> None:
        if path.name != "bars.001":
            raise OSError("published cleanup failure")
        original_remove(self, path)

    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", fail_cleanup)
    replacement = catalog.write(
        "bars.001", bar_frame().with_columns(pl.col("close") + 0.5),
        source="unit-test", schema_version="1", as_of=AS_OF, overwrite=True,
    )
    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", original_remove)
    tampered = bar_frame().with_columns(pl.col("close") + 10.0)
    tampered.write_parquet(tmp_path / "bars.001" / "bars.parquet")
    assert replacement.dataset_id == "bars.001"
    with pytest.raises(DatasetIntegrityError):
        catalog.verify("bars.001")


def test_empty_abandoned_transaction_does_not_brick_catalog(tmp_path: Path) -> None:
    write_dataset(ParquetDataCatalog(tmp_path), "unrelated")
    (tmp_path / ".qf_transactions" / "empty").mkdir(parents=True)
    recovered = ParquetDataCatalog(tmp_path)
    assert recovered.verify("unrelated").dataset_id == "unrelated"


def test_failed_overwrite_restores_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    old = write_dataset(catalog)

    def fail_publish(tx: Path, dataset_id: str, directory: Path, *, overwrite: bool) -> None:
        assert overwrite
        os.replace(directory, tx / "backup")
        catalog._write_state(tx, dataset_id, "old_moved")
        raise OSError("injected overwrite failure")

    monkeypatch.setattr(catalog, "_publish_transaction", fail_publish)
    with pytest.raises(OSError, match="injected overwrite failure"):
        catalog.write(
            "bars.001",
            bar_frame().with_columns(pl.col("close") + 0.5),
            source="unit-test",
            schema_version="1",
            as_of=AS_OF,
            overwrite=True,
        )
    assert catalog.verify("bars.001") == old
    assert catalog.read("bars.001")["close"].to_list() == [11.0, 12.0, 21.0]


def test_interrupted_old_moved_transaction_recovers_and_is_idempotent(tmp_path: Path) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    old = write_dataset(catalog)
    tx = tmp_path / ".qf_transactions" / "synthetic"
    tx.mkdir(parents=True)
    shutil.copytree(tmp_path / "bars.001", tx / "backup")
    (tx / "candidate").mkdir()
    (tx / "state.json").write_text(
        json.dumps({"dataset_id": "bars.001", "phase": "old_moved"}), encoding="utf-8"
    )
    shutil.rmtree(tmp_path / "bars.001")

    recovered = ParquetDataCatalog(tmp_path)
    assert recovered.verify("bars.001") == old


def test_restore_then_cleanup_failure_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    old = write_dataset(catalog)
    tx = tmp_path / ".qf_transactions" / "synthetic"
    tx.mkdir(parents=True)
    shutil.copytree(tmp_path / "bars.001", tx / "backup")
    (tx / "state.json").write_text(
        json.dumps({"dataset_id": "bars.001", "phase": "old_moved", "had_existing": True}),
        encoding="utf-8",
    )
    shutil.rmtree(tmp_path / "bars.001")

    original_remove = catalog._remove_tree
    calls = 0

    def fail_transaction_cleanup(self: ParquetDataCatalog, path: Path) -> None:
        nonlocal calls
        if path == tx:
            calls += 1
            raise OSError("injected cleanup failure")
        original_remove(path)

    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", fail_transaction_cleanup)
    recovered = ParquetDataCatalog(tmp_path)
    assert calls == 1
    assert recovered.verify("bars.001") == old
    assert (tmp_path / "bars.001" / "manifest.json").is_file()
    assert (tx / "state.json").exists()

    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", original_remove)
    restarted = ParquetDataCatalog(tmp_path)
    assert restarted.verify("bars.001") == old
    assert restarted.read("bars.001")["close"].to_list() == [11.0, 12.0, 21.0]
    assert not tx.exists()
    restarted_again = ParquetDataCatalog(tmp_path)
    assert restarted_again.verify("bars.001") == old
    assert not tx.exists()
    recovered._recover_transactions()
    assert recovered.verify("bars.001") == old


def test_restored_journal_cannot_outlive_next_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    old = write_dataset(catalog)
    tx = tmp_path / ".qf_transactions" / "restored-old"
    tx.mkdir(parents=True)
    shutil.copytree(tmp_path / "bars.001", tx / "backup")
    (tx / "state.json").write_text(
        json.dumps(
            {"dataset_id": "bars.001", "phase": "old_moved", "had_existing": True}
        ),
        encoding="utf-8",
    )
    shutil.rmtree(tmp_path / "bars.001")

    original_remove = ParquetDataCatalog._remove_tree

    def fail_old_transaction_cleanup(self: ParquetDataCatalog, path: Path) -> None:
        if path == tx:
            raise OSError("injected terminal cleanup failure")
        original_remove(self, path)

    monkeypatch.setattr(
        ParquetDataCatalog, "_remove_tree", fail_old_transaction_cleanup
    )
    recovered = ParquetDataCatalog(tmp_path)
    assert recovered.verify("bars.001") == old
    assert json.loads((tx / "state.json").read_text(encoding="utf-8"))["phase"] == "restored"

    with pytest.raises(DatasetIntegrityError, match="unresolved transaction"):
        recovered.write(
            "bars.001",
            bar_frame().with_columns(pl.col("close") + 0.5),
            source="unit-test",
            schema_version="1",
            as_of=AS_OF,
            overwrite=True,
        )
    assert recovered.verify("bars.001") == old

    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", original_remove)
    new = recovered.write(
        "bars.001",
        bar_frame().with_columns(pl.col("close") + 0.5),
        source="unit-test",
        schema_version="1",
        as_of=AS_OF,
        overwrite=True,
    )
    assert not tx.exists()
    assert ParquetDataCatalog(tmp_path).verify("bars.001") == new
    assert ParquetDataCatalog(tmp_path).verify("bars.001") == new


def test_corrupted_published_dataset_does_not_brick_unrelated_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog, "affected")
    healthy = write_dataset(catalog, "healthy")
    original_remove = ParquetDataCatalog._remove_tree

    def retain_affected_transaction(self: ParquetDataCatalog, path: Path) -> None:
        if path.parent == tmp_path / ".qf_transactions":
            raise OSError("injected published cleanup failure")
        original_remove(self, path)

    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", retain_affected_transaction)
    catalog.write(
        "affected",
        bar_frame().with_columns(pl.col("close") + 0.5),
        source="unit-test",
        schema_version="1",
        as_of=AS_OF,
        overwrite=True,
    )
    monkeypatch.setattr(ParquetDataCatalog, "_remove_tree", original_remove)
    tampered = bar_frame().with_columns(pl.col("close") + 0.25)
    tampered.write_parquet(tmp_path / "affected" / "bars.parquet")

    for _ in range(2):
        recovered = ParquetDataCatalog(tmp_path)
        with pytest.raises(DatasetIntegrityError):
            recovered.verify("affected")
        with pytest.raises(DatasetIntegrityError):
            recovered.read_snapshot("affected")
        with pytest.raises(DatasetIntegrityError):
            recovered.read("affected")
        with pytest.raises(DatasetIntegrityError):
            recovered.query("affected")
        assert recovered.verify("healthy") == healthy
        assert recovered.read_snapshot("healthy") == healthy
        assert recovered.read("healthy").height == 3
        assert recovered.query("healthy").height == 3


def test_interrupted_prepared_create_is_not_promoted(tmp_path: Path) -> None:
    write_dataset(ParquetDataCatalog(tmp_path), "other")
    candidate = tmp_path / ".qf_transactions" / "synthetic" / "candidate"
    candidate.mkdir(parents=True)
    # A candidate can be complete yet remains uncommitted while phase=prepared.
    shutil.copytree(tmp_path / "other", candidate, dirs_exist_ok=True)
    final = tmp_path / "new-data"
    os.replace(candidate, final)
    tx = tmp_path / ".qf_transactions" / "synthetic"
    (tx / "state.json").write_text(
        json.dumps({"dataset_id": "new-data", "phase": "prepared", "had_existing": False}),
        encoding="utf-8",
    )
    recovered = ParquetDataCatalog(tmp_path)
    with pytest.raises(FileNotFoundError):
        recovered.verify("new-data")
    assert recovered.verify("other").dataset_id == "other"


def test_published_invalid_candidate_surfaces_integrity_error(tmp_path: Path) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    tx = tmp_path / ".qf_transactions" / "synthetic"
    tx.mkdir(parents=True)
    shutil.copytree(tmp_path / "bars.001", tx / "backup")
    tampered = bar_frame().with_columns(pl.col("close") + 0.5)
    tampered.write_parquet(tmp_path / "bars.001" / "bars.parquet")
    (tx / "state.json").write_text(
        json.dumps({"dataset_id": "bars.001", "phase": "published", "had_existing": True}),
        encoding="utf-8",
    )
    with pytest.raises(DatasetIntegrityError):
        ParquetDataCatalog(tmp_path).verify("bars.001")


def test_failed_rollback_leaves_recoverable_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    old = write_dataset(catalog)
    original = catalog._restore_backup

    def fail_restore(*args: object, **kwargs: object) -> None:
        raise OSError("injected rollback failure")

    def fail_publish(tx: Path, dataset_id: str, directory: Path, *, overwrite: bool) -> None:
        os.replace(directory, tx / "backup")
        catalog._write_state(tx, dataset_id, "old_moved", had_existing=True)
        raise OSError("injected publication failure")

    monkeypatch.setattr(catalog, "_restore_backup", fail_restore)
    monkeypatch.setattr(catalog, "_publish_transaction", fail_publish)
    with pytest.raises(OSError, match="injected rollback failure"):
        catalog.write(
            "bars.001",
            bar_frame().with_columns(pl.col("close") + 0.5),
            source="unit-test",
            schema_version="1",
            as_of=AS_OF,
            overwrite=True,
        )
    assert list((tmp_path / ".qf_transactions").glob("*/backup"))
    monkeypatch.setattr(catalog, "_restore_backup", original)
    # Publication was rejected before moving the old dataset, so a later
    # operation remains usable and the committed snapshot is unchanged.
    assert catalog.verify("bars.001") == old


def test_cleanup_failure_after_commit_is_safe_on_next_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    original = catalog._remove_tree

    def fail_cleanup(path: Path) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(catalog, "_remove_tree", fail_cleanup)
    replacement = catalog.write(
        "bars.001",
        bar_frame().with_columns(pl.col("close") + 0.5),
        source="unit-test",
        schema_version="1",
        as_of=AS_OF,
        overwrite=True,
    )
    assert replacement.dataset_id == "bars.001"
    assert (tmp_path / "bars.001" / "bars.parquet").is_file()
    assert list((tmp_path / ".qf_transactions").glob("*/"))

    monkeypatch.setattr(catalog, "_remove_tree", original)
    catalog._recover_transactions()
    assert catalog.verify("bars.001") == replacement
    assert not list((tmp_path / ".qf_transactions").glob("*/"))


def test_tampered_parquet_is_rejected_by_verify_read_and_query(tmp_path: Path) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    tampered = bar_frame().with_columns(pl.col("close") + 0.5)
    tampered.write_parquet(tmp_path / "bars.001" / "bars.parquet")
    for operation in (
        lambda: catalog.verify("bars.001"),
        lambda: catalog.read("bars.001"),
        lambda: catalog.query("bars.001"),
    ):
        with pytest.raises(DatasetIntegrityError):
            operation()


@pytest.mark.parametrize("field", ["row_count", "min_timestamp", "max_timestamp", "content_hash", "relative_path", "dataset_id"])
def test_tampered_manifest_is_rejected(tmp_path: Path, field: str) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog)
    manifest_path = tmp_path / "bars.001" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    replacements = {
        "row_count": 99,
        "min_timestamp": "2024-01-01T00:00:00Z",
        "max_timestamp": "2026-01-01T00:00:00Z",
        "content_hash": "0" * 64,
        "relative_path": "../escape.parquet",
        "dataset_id": "other",
    }
    manifest[field] = replacements[field]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetIntegrityError):
        catalog.verify("bars.001")


@pytest.mark.parametrize(
    "dataset_id",
    [
        "CON",
        "nul.data",
        "AUX.snapshot",
        "prn",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}.parquet" for i in range(1, 10)),
        "trailing.",
        ".qf_transactions",
        ".qf_transactions.staging",
    ],
)
def test_windows_reserved_and_internal_dataset_ids_rejected(dataset_id: str) -> None:
    with pytest.raises(ValueError):
        validate_dataset_id(dataset_id)


def test_mixed_case_id_is_valid_and_case_alias_collision_fails(tmp_path: Path) -> None:
    catalog = ParquetDataCatalog(tmp_path)
    write_dataset(catalog, "DataSetA")
    assert catalog.verify("dataseta").dataset_id == "DataSetA"

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    fake = ParquetDataCatalog.__new__(ParquetDataCatalog)
    fake.root = type("FakeRoot", (), {"iterdir": lambda self: [Entry("DataSetA"), Entry("dataseta")]})()
    with pytest.raises(DatasetIntegrityError, match="ambiguous case-insensitive"):
        fake._check_casefold_collisions()


def test_transaction_namespace_file_collision_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / ".qf_transactions").write_text("not a directory", encoding="utf-8")
    with pytest.raises(DatasetIntegrityError, match="transaction namespace"):
        ParquetDataCatalog(tmp_path)


def test_transaction_namespace_case_alias_fails_loudly() -> None:
    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    fake = ParquetDataCatalog.__new__(ParquetDataCatalog)
    fake.root = type(
        "FakeRoot",
        (),
        {"iterdir": lambda self: [Entry(".QF_TRANSACTIONS")]},
    )()
    with pytest.raises(DatasetIntegrityError, match="transaction namespace"):
        fake._check_casefold_collisions()


def test_manifest_path_model_rejects_drive_and_unc_escapes() -> None:
    from quantforge.data.catalog import DatasetSnapshot

    payload = {
        "dataset_id": "bars.001",
        "source": "test",
        "schema_version": "1",
        "row_count": 0,
        "min_timestamp": None,
        "max_timestamp": None,
        "as_of": AS_OF,
        "content_hash": "0" * 64,
    }
    for path in ["C:escape", "\\\\server\\share", "../escape"]:
        with pytest.raises(ValueError):
            DatasetSnapshot.model_validate({**payload, "relative_path": path})
