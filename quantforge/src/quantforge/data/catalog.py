from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Self, cast

import duckdb
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .bars import (
    BAR_COLUMNS,
    BAR_SCHEMA,
    canonicalize_bars,
    logical_content_hash,
    normalize_utc,
    prepare_bars,
    validate_bars,
)

_DATASET_ID = re.compile(r"[A-Za-z0-9._-]+\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_PRIVATE_NAMESPACE = ".qf_transactions"
_ACTIVE_TRANSACTION_PHASES = {"staging", "prepared", "old_moved", "restoring"}
_TERMINAL_TRANSACTION_PHASES = {"restored", "published"}
_TRANSACTION_PHASES = _ACTIVE_TRANSACTION_PHASES | _TERMINAL_TRANSACTION_PHASES
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class DatasetIntegrityError(ValueError):
    """Persisted manifest and parquet contents are inconsistent."""


def validate_dataset_id(dataset_id: str) -> str:
    if (
        not isinstance(dataset_id, str)
        or not dataset_id
        or _DATASET_ID.fullmatch(dataset_id) is None
        or ".." in dataset_id
        or dataset_id.endswith(".")
    ):
        raise ValueError("dataset_id must use only ASCII letters, digits, '.', '_', or '-' without '..'")
    stem = dataset_id.split(".", 1)[0].casefold()
    if (
        stem in _WINDOWS_RESERVED_NAMES
        or dataset_id.casefold() == _PRIVATE_NAMESPACE.casefold()
        or dataset_id.casefold().startswith(_PRIVATE_NAMESPACE.casefold() + ".")
    ):
        raise ValueError("dataset_id is reserved on Windows")
    return dataset_id


def _datetime_from_microseconds(value: int | None) -> datetime | None:
    if value is None:
        return None
    try:
        return _EPOCH + timedelta(microseconds=value)
    except OverflowError as exc:
        raise ValueError("dataset timestamp is outside Python's representable range") from exc


class DatasetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    source: str
    schema_version: str
    row_count: int = Field(ge=0)
    min_timestamp: datetime | None
    max_timestamp: datetime | None
    as_of: datetime
    content_hash: str
    relative_path: str

    _dataset_id = field_validator("dataset_id")(validate_dataset_id)

    @field_validator("min_timestamp", "max_timestamp", "as_of")
    @classmethod
    def _utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc(value, name="snapshot timestamp")

    @field_validator("content_hash")
    @classmethod
    def _content_hash(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("content_hash must be lowercase SHA-256 hexadecimal")
        return value

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not isinstance(value, str)
            or not value
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or ":" in value
        ):
            raise ValueError("relative_path must be a safe path relative to the catalog root")
        return value

    @classmethod
    def from_json(cls, value: str) -> Self:
        return cls.model_validate_json(value)


class ParquetDataCatalog:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._recovery_errors: dict[str, DatasetIntegrityError] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._check_casefold_collisions()
        self._recover_transactions()

    def _check_casefold_collisions(self) -> None:
        entries: dict[str, list[Path]] = {}
        for entry in self.root.iterdir():
            entries.setdefault(entry.name.casefold(), []).append(entry)
        private_aliases = entries.get(_PRIVATE_NAMESPACE.casefold(), [])
        if any(entry.name != _PRIVATE_NAMESPACE for entry in private_aliases):
            raise DatasetIntegrityError("catalog transaction namespace case collision")
        entries.pop(_PRIVATE_NAMESPACE.casefold(), None)
        collisions = [paths for paths in entries.values() if len(paths) > 1]
        if collisions:
            names = sorted(path.name for paths in collisions for path in paths)
            raise DatasetIntegrityError(
                f"ambiguous case-insensitive dataset identity: {', '.join(names)}"
            )

    def _dataset_directory(self, dataset_id: str) -> Path:
        dataset_id = validate_dataset_id(dataset_id)
        self._check_casefold_collisions()
        matches = [
            p
            for p in self.root.iterdir()
            if p.name.casefold() == dataset_id.casefold()
            and p.name.casefold() != _PRIVATE_NAMESPACE.casefold()
        ]
        return matches[0] if matches else self.root / dataset_id

    def _tx_root(self) -> Path:
        path = self.root / _PRIVATE_NAMESPACE
        self._safe_inside(path)
        if path.exists() and not path.is_dir():
            raise DatasetIntegrityError("catalog transaction namespace is not a directory")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _safe_inside(self, path: Path) -> Path:
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise DatasetIntegrityError("path escapes catalog root") from exc
        return path

    def _remove_tree(self, path: Path) -> None:
        """Best-effort cleanup for private transaction artifacts."""
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            return

    def _restore_backup(self, tx: Path, final: Path, dataset_id: str) -> None:
        backup = tx / "backup"
        if not backup.exists():
            raise DatasetIntegrityError("transaction has no recoverable committed dataset")
        backup_snapshot = self._verify_directory(backup, dataset_id)
        self._write_state(
            tx,
            dataset_id,
            "restoring",
            restored_content_hash=backup_snapshot.content_hash,
        )
        if final.exists():
            self._remove_tree(final)
        os.replace(backup, final)
        self._verify_directory(final, dataset_id)
        self._write_state(
            tx,
            dataset_id,
            "restored",
            restored_content_hash=backup_snapshot.content_hash,
        )

    def _cleanup_transaction(self, tx: Path) -> bool:
        try:
            self._remove_tree(tx)
        except OSError:
            # Cleanup is best effort after authority has settled. The marker
            # remains inert and blocks a later same-ID publication.
            return False
        return not tx.exists()

    def _recover_transaction(
        self,
        tx: Path,
        dataset_id: str,
        phase: str,
        had_existing: bool,
        state: dict[str, Any],
    ) -> None:
        if phase in _TERMINAL_TRANSACTION_PHASES:
            self._cleanup_transaction(tx)
            return

        final = self._dataset_directory(dataset_id)
        effective_dataset_id = final.name if final.exists() else dataset_id
        candidate = tx / "candidate"
        backup = tx / "backup"
        self._safe_inside(candidate)
        self._safe_inside(backup)
        self._safe_inside(final)

        # Recovery authority table:
        # phase       authority       recovery action          later generation
        # staging     previous/none   discard uncommitted NEW  no authority
        # prepared    previous/none   discard uncommitted NEW  no authority
        # old_moved   recorded OLD    restore OLD              none after terminal
        # restoring   recorded OLD    finish/confirm restore   none after terminal
        # restored    settled OLD     cleanup only             never
        # published   settled NEW     cleanup only             never
        if phase == "old_moved":
            self._restore_backup(tx, final, effective_dataset_id)
            self._cleanup_transaction(tx)
        elif phase == "restoring":
            if backup.exists():
                self._restore_backup(tx, final, effective_dataset_id)
            else:
                expected_hash = state.get("restored_content_hash")
                try:
                    restored = self._verify_directory(final, effective_dataset_id)
                except Exception as exc:
                    raise DatasetIntegrityError(
                        "restoring transaction has no verifiable restored dataset"
                    ) from exc
                if not isinstance(expected_hash, str) or restored.content_hash != expected_hash:
                    raise DatasetIntegrityError(
                        "restoring transaction has no authoritative restored dataset"
                    )
                self._write_state(
                    tx,
                    effective_dataset_id,
                    "restored",
                    restored_content_hash=restored.content_hash,
                )
            self._cleanup_transaction(tx)
        elif phase in {"prepared", "staging"}:
            # The old final dataset was not moved in these phases. Any
            # candidate is uncommitted and must never be promoted.
            if backup.exists():
                self._restore_backup(tx, final, effective_dataset_id)
            elif not had_existing and final.exists():
                self._remove_tree(final)
            self._cleanup_transaction(tx)

    def _raise_recovery_error(self, dataset_id: str) -> None:
        error = self._recovery_errors.get(validate_dataset_id(dataset_id).casefold())
        if error is not None:
            raise DatasetIntegrityError(str(error)) from error

    def _ensure_publication_barrier(self, dataset_id: str) -> None:
        folded_dataset_id = validate_dataset_id(dataset_id).casefold()
        txroot = self.root / _PRIVATE_NAMESPACE
        if not txroot.exists():
            return
        for tx in txroot.iterdir():
            state_file = tx / "state.json"
            if not state_file.is_file():
                continue
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                tx_dataset = validate_dataset_id(str(state["dataset_id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise DatasetIntegrityError(f"invalid transaction state: {tx.name}") from exc
            if tx_dataset.casefold() == folded_dataset_id:
                raise DatasetIntegrityError(
                    f"unresolved transaction blocks publication: {tx.name}"
                )

    def _recover_transactions(self) -> None:
        recovery_errors: dict[str, DatasetIntegrityError] = {}
        txroot = self.root / _PRIVATE_NAMESPACE
        if not txroot.exists():
            if txroot.is_symlink():
                raise DatasetIntegrityError("catalog transaction namespace escapes catalog root")
            self._recovery_errors = recovery_errors
            return
        self._safe_inside(txroot)
        if not txroot.is_dir():
            raise DatasetIntegrityError("catalog transaction namespace is not a directory")
        transactions = sorted(
            (p for p in txroot.iterdir() if p.is_dir()), key=lambda p: p.name
        )
        by_dataset: dict[
            str, list[tuple[Path, str, str, bool, dict[str, Any]]]
        ] = {}
        for tx in transactions:
            self._safe_inside(tx)
            state_file = tx / "state.json"
            if not state_file.exists():
                # An empty transaction directory is an abandoned staging
                # artifact with no authority over canonical datasets.
                if not any(tx.iterdir()):
                    self._cleanup_transaction(tx)
                    continue
                raise DatasetIntegrityError(f"invalid transaction state: {tx.name}")
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                dataset_id = validate_dataset_id(str(state["dataset_id"]))
            except Exception as exc:
                raise DatasetIntegrityError(f"invalid transaction state: {tx.name}") from exc
            folded_id = dataset_id.casefold()
            try:
                phase = state["phase"]
                if not isinstance(phase, str) or phase not in _TRANSACTION_PHASES:
                    raise ValueError("unknown transaction phase")
                had_existing = state.get("had_existing", False)
                if not isinstance(had_existing, bool):
                    raise TypeError("invalid existing-state marker")
            except (KeyError, TypeError, ValueError) as exc:
                error = DatasetIntegrityError(f"invalid transaction state: {tx.name}")
                error.__cause__ = exc
                recovery_errors[folded_id] = error
                continue
            by_dataset.setdefault(folded_id, []).append(
                (tx, dataset_id, phase, had_existing, state)
            )

        for folded_id in sorted(by_dataset):
            entries = by_dataset[folded_id]
            if folded_id in recovery_errors:
                continue
            terminal_entries = [
                entry for entry in entries if entry[2] in _TERMINAL_TRANSACTION_PHASES
            ]
            active_entries = [
                entry for entry in entries if entry[2] in _ACTIVE_TRANSACTION_PHASES
            ]
            for tx, dataset_id, phase, had_existing, state in terminal_entries:
                self._recover_transaction(tx, dataset_id, phase, had_existing, state)
            if len(active_entries) > 1:
                recovery_errors[folded_id] = DatasetIntegrityError(
                    f"multiple pending transactions for dataset: {entries[0][1]}"
                )
                continue
            if not active_entries:
                continue
            tx, dataset_id, phase, had_existing, state = active_entries[0]
            try:
                self._recover_transaction(tx, dataset_id, phase, had_existing, state)
            except DatasetIntegrityError as exc:
                recovery_errors[folded_id] = exc
            except OSError as exc:
                error = DatasetIntegrityError(
                    f"transaction recovery failed for dataset: {dataset_id}"
                )
                error.__cause__ = exc
                recovery_errors[folded_id] = error
        self._recovery_errors = recovery_errors

    def _write_state(
        self,
        tx: Path,
        dataset_id: str,
        phase: str,
        *,
        had_existing: bool | None = None,
        restored_content_hash: str | None = None,
    ) -> None:
        if had_existing is None and (tx / "state.json").is_file():
            previous = json.loads((tx / "state.json").read_text(encoding="utf-8"))
            had_existing = previous.get("had_existing", False)
        state = {
            "dataset_id": dataset_id,
            "phase": phase,
            "had_existing": bool(had_existing),
        }
        if restored_content_hash is not None:
            state["restored_content_hash"] = restored_content_hash
        (tx / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def _publish_transaction(self, tx: Path, dataset_id: str, directory: Path, *, overwrite: bool) -> None:
        """Publish a prepared candidate; kept as a narrow fault-injection seam."""
        candidate, backup = tx / "candidate", tx / "backup"
        if overwrite and directory.exists():
            os.replace(directory, backup)
            self._write_state(tx, dataset_id, "old_moved")
        os.replace(candidate, directory)
        self._write_state(tx, dataset_id, "published")

    def _verify_directory(self, directory: Path, dataset_id: str) -> DatasetSnapshot:
        self._safe_inside(directory)
        manifest_path, parquet_path = directory / "manifest.json", directory / "bars.parquet"
        if not directory.is_dir() or not manifest_path.is_file() or not parquet_path.is_file():
            raise DatasetIntegrityError("dataset files are incomplete")
        try:
            snapshot = DatasetSnapshot.from_json(manifest_path.read_text(encoding="utf-8"))
            if (
                snapshot.dataset_id.casefold() != dataset_id.casefold()
                or snapshot.relative_path.casefold()
                != f"{dataset_id}/bars.parquet".casefold()
            ):
                raise DatasetIntegrityError("manifest identity/path mismatch")
            frame = pl.read_parquet(parquet_path)
            if frame.schema != BAR_SCHEMA:
                raise DatasetIntegrityError("persisted dataset does not have the canonical schema")
            canonical = canonicalize_bars(frame)
            validate_bars(canonical, as_of=snapshot.as_of)
            if canonical.height != snapshot.row_count:
                raise DatasetIntegrityError("manifest row/schema mismatch")
            ts = canonical["timestamp"].cast(pl.Int64)
            minimum = _datetime_from_microseconds(cast(int | None, ts.min()))
            maximum = _datetime_from_microseconds(cast(int | None, ts.max()))
            if minimum != snapshot.min_timestamp or maximum != snapshot.max_timestamp or logical_content_hash(canonical) != snapshot.content_hash:
                raise DatasetIntegrityError("manifest does not match persisted data")
            if maximum is not None and maximum > snapshot.as_of:
                raise DatasetIntegrityError("dataset exceeds as_of")
            return snapshot
        except DatasetIntegrityError:
            raise
        except Exception as exc:
            raise DatasetIntegrityError("persisted dataset failed integrity validation") from exc

    def write(
        self,
        dataset_id: str,
        frame: pl.DataFrame,
        *,
        source: str,
        schema_version: str,
        as_of: datetime,
        overwrite: bool = False,
    ) -> DatasetSnapshot:
        dataset_id = validate_dataset_id(dataset_id)
        self._recover_transactions()
        self._raise_recovery_error(dataset_id)
        directory = self._dataset_directory(dataset_id)
        self._ensure_publication_barrier(directory.name if directory.exists() else dataset_id)
        had_existing = directory.exists()
        if had_existing and not overwrite:
            raise FileExistsError(f"dataset already exists: {dataset_id}")
        effective_dataset_id = directory.name if directory.exists() else dataset_id

        canonical = prepare_bars(frame, as_of=as_of)
        as_of_utc = normalize_utc(as_of, name="as_of")
        relative_path = PurePosixPath(effective_dataset_id, "bars.parquet").as_posix()
        timestamp_values = canonical["timestamp"].cast(pl.Int64)
        minimum = cast(int | None, timestamp_values.min())
        maximum = cast(int | None, timestamp_values.max())
        snapshot = DatasetSnapshot(
            dataset_id=effective_dataset_id,
            source=source,
            schema_version=schema_version,
            row_count=canonical.height,
            min_timestamp=_datetime_from_microseconds(minimum),
            max_timestamp=_datetime_from_microseconds(maximum),
            as_of=as_of_utc,
            content_hash=logical_content_hash(canonical),
            relative_path=relative_path,
        )
        tx = self._tx_root() / uuid.uuid4().hex
        candidate = tx / "candidate"
        tx.mkdir(parents=True)
        try:
            self._write_state(
                tx, effective_dataset_id, "staging", had_existing=had_existing
            )
            candidate.mkdir()
            canonical.write_parquet(candidate / "bars.parquet")
            (candidate / "manifest.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
            self._verify_directory(candidate, effective_dataset_id)
            self._write_state(tx, effective_dataset_id, "prepared")
            self._publish_transaction(tx, effective_dataset_id, directory, overwrite=overwrite)
            self._verify_directory(directory, effective_dataset_id)
            try:
                self._remove_tree(tx)
            except OSError:
                # The new dataset is already committed and verified. A stale
                # private transaction is safe to clean on the next operation.
                pass
            return snapshot
        except Exception:
            rollback_ok = False
            try:
                if (tx / "backup").exists():
                    self._restore_backup(tx, directory, effective_dataset_id)
                    rollback_ok = True
                elif not had_existing and directory.exists():
                    # A create that failed after candidate promotion has no
                    # prior snapshot to restore; remove the uncommitted final.
                    self._remove_tree(directory)
            finally:
                # A failed create may have promoted an uncommitted candidate.
                # Keep its journal whenever final cleanup did not complete;
                # recovery must retain authority metadata across restart.
                final_absent = not directory.exists()
                if rollback_ok or (not (tx / "backup").exists() and final_absent):
                    try:
                        self._remove_tree(tx)
                    except OSError:
                        pass
            raise

    def read(self, dataset_id: str) -> pl.DataFrame:
        self.verify(dataset_id)
        frame = pl.read_parquet(self._dataset_directory(dataset_id) / "bars.parquet")
        return frame.sort(["symbol", "timestamp"])

    def read_snapshot(self, dataset_id: str) -> DatasetSnapshot:
        return self.verify(dataset_id)

    def verify(self, dataset_id: str) -> DatasetSnapshot:
        dataset_id = validate_dataset_id(dataset_id)
        self._recover_transactions()
        self._raise_recovery_error(dataset_id)
        directory = self._dataset_directory(dataset_id)
        if not directory.exists():
            raise FileNotFoundError(f"dataset not found: {dataset_id}")
        return self._verify_directory(directory, directory.name)

    def query(
        self,
        dataset_id: str,
        *,
        symbols: list[str] | tuple[str, ...] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        columns: list[str] | tuple[str, ...] | None = None,
    ) -> pl.DataFrame:
        self.verify(dataset_id)
        selected = list(BAR_COLUMNS if columns is None else columns)
        invalid = set(selected) - set(BAR_COLUMNS)
        if invalid or not selected:
            raise ValueError(f"invalid requested columns: {sorted(invalid)}")

        predicates: list[str] = []
        parameters: list[object] = [str(self._dataset_directory(dataset_id) / "bars.parquet")]
        if symbols is not None:
            if not symbols:
                return pl.DataFrame(schema={name: cast(Any, BAR_SCHEMA[name]) for name in selected})
            predicates.append(f"symbol IN ({', '.join('?' for _ in symbols)})")
            parameters.extend(symbols)
        if start is not None:
            predicates.append("timestamp >= ?")
            parameters.append(normalize_utc(start, name="start"))
        if end is not None:
            predicates.append("timestamp <= ?")
            parameters.append(normalize_utc(end, name="end"))

        projection = ", ".join(f'"{name}"' for name in selected)
        where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        sql = f"SELECT {projection} FROM read_parquet(?){where} ORDER BY symbol, timestamp"
        connection = duckdb.connect(":memory:")
        try:
            result = connection.execute(sql, parameters).pl()
        finally:
            connection.close()
        return result.select([pl.col(name).cast(cast(Any, BAR_SCHEMA[name])) for name in selected])
