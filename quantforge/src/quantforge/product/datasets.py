"""Admit a verified local pilot snapshot to research without upgrading its source claims."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from quantforge.data import DatasetSnapshot, ParquetDataCatalog, logical_content_hash
from quantforge.experiments import canonical_json

from .pilot.data import read_snapshot
from .storage import local_root, write_exclusive, writer_lock

SOURCE = "operator-pilot-snapshot-v1"


def import_pilot_dataset(
    root: Path,
    pilot_root: Path,
    snapshot_id: str,
    *,
    start: str,
    end: str,
    exposure_declaration: str,
) -> dict[str, Any]:
    root, pilot_root = local_root(root), local_root(pilot_root)
    if not 10 <= len(exposure_declaration.strip()) <= 2000:
        raise ValueError(
            "declare prior research/paper exposure and retrospective limitations"
        )
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("invalid declared research interval")
    directory = local_root(pilot_root / "data" / snapshot_id)
    for name in ("bars.parquet", "snapshot.json", "extensions.json"):
        if (directory / name).is_symlink():
            raise ValueError("redirected pilot snapshot")
    frame, source = read_snapshot(pilot_root, snapshot_id)
    frame = frame.filter(pl.col("timestamp").dt.date().is_between(first, last))
    if frame.height < 80 or frame["symbol"].n_unique() != 1:
        raise ValueError("research requires at least 80 single-symbol observations")
    origin = {
        "schema_version": SOURCE,
        "source_snapshot": source,
        "selected_start": first.isoformat(),
        "selected_end": last.isoformat(),
        "selected_content_hash": logical_content_hash(frame),
        "prior_exposure_declaration": exposure_declaration.strip(),
        "limitations": [
            *source["limitations"],
            "Retrospective operator-selected interval; prior paper/research exposure is declared, not erased.",
            "Research uses declared switch fees, not a calibrated CN broker/lot/settlement execution model.",
            "No alpha, exchange authentication, point-in-time universe or live eligibility is established.",
        ],
    }
    identity = (
        "pilot-study-"
        + hashlib.sha256(canonical_json(origin).encode()).hexdigest()[:24]
    )
    catalog = ParquetDataCatalog(root / "catalog")
    with writer_lock(root):
        qualified = root / "qualifications" / identity
        qualified.mkdir(parents=True, exist_ok=True)
        local_root(qualified)
        stamp = datetime.fromisoformat(source["as_of"]).astimezone(UTC)
        if (catalog.root / identity).exists():
            snapshot = catalog.verify(identity)
            if (
                snapshot.content_hash != logical_content_hash(frame)
                or snapshot.row_count != frame.height
                or snapshot.source != SOURCE
                or snapshot.schema_version != "bars-v1"
                or snapshot.as_of != stamp
            ):
                raise ValueError("conflicting retained research dataset")
        else:
            snapshot = catalog.write(
                identity, frame, source=SOURCE, schema_version="bars-v1", as_of=stamp
            )
        document = {**origin, "dataset_snapshot": snapshot.model_dump(mode="json")}
        target = qualified / "host-qualification.json"
        if target.exists():
            if (
                target.is_symlink()
                or target.read_bytes() != canonical_json(document).encode()
            ):
                raise ValueError("conflicting retained host qualification")
        else:
            write_exclusive(target, document)
    return {
        "dataset": snapshot.model_dump(mode="json"),
        "qualification": qualification(root, snapshot),
    }


def qualification(root: Path, snapshot: DatasetSnapshot) -> dict[str, Any]:
    directory = local_root(root / "qualifications" / snapshot.dataset_id)
    path = directory / "host-qualification.json"
    if path.is_symlink():
        raise ValueError("redirected host qualification")
    raw = path.read_bytes()
    value = json.loads(raw)
    if (
        raw != canonical_json(value).encode()
        or value["schema_version"] != SOURCE
        or DatasetSnapshot.model_validate(value["dataset_snapshot"]) != snapshot
        or value["selected_content_hash"] != snapshot.content_hash
    ):
        raise ValueError("host qualification binding mismatch")
    origin = {k: v for k, v in value.items() if k != "dataset_snapshot"}
    identity = (
        "pilot-study-"
        + hashlib.sha256(canonical_json(origin).encode()).hexdigest()[:24]
    )
    if identity != snapshot.dataset_id:
        raise ValueError("host qualification identity mismatch")
    return {
        "status": "VALIDATED_OPERATOR_SNAPSHOT_NOT_EXTERNAL_AUTHENTICATION",
        "qualification_sha256": hashlib.sha256(raw).hexdigest(),
        "source": value["source_snapshot"]["source"],
        "prior_exposure_declaration": value["prior_exposure_declaration"],
        "limitations": value["limitations"],
    }
