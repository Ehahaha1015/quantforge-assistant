"""Pilot market-data snapshots: operator CSV import and corpus raw-scale bridge.

Three bounded, deterministic sources feed the paper-only pilot:

- ``import_bars_csv`` admits an operator-supplied daily-bar CSV for any CNY
  instrument (the intended source for a low-price ETF), with the operator's
  declared adjustment/unit statement retained verbatim as a source claim;
- ``extend_bars_csv`` appends strictly later sessions to an existing snapshot
  so a daily pilot can continue on the same frozen declaration; every append
  is recorded in an append-only revision chain and any prefix change fails
  closed before the extension becomes visible;
- ``corpus_snapshot`` recovers the raw vendor price scale of one member of the
  authenticated pinned A-share corpus (``OHLC / factor``) for the same aligned
  rows the qualified loader returns.

Neither path certifies exchange calendars, entitlements, adjustment quality or
capacity, and neither can place an order.
"""

from __future__ import annotations

import csv
import hashlib
import os
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import polars as pl

from quantforge.data import BAR_SCHEMA, logical_content_hash, prepare_bars
from quantforge.experiments import canonical_json

from ..scale.corpus import load_qlib_corpus
from ..scale.storage import digest, publish, read_json
from ..storage import local_root, writer_lock

__all__ = [
    "PilotDataError",
    "corpus_snapshot",
    "extend_bars_csv",
    "import_bars_csv",
    "list_snapshots",
    "read_snapshot",
]

_MAX_CSV_BYTES = 8 * 1024 * 1024
_MAX_ROWS = 20_000
_TICK = Decimal("0.01")
_TICK_TOLERANCE = Decimal("0.004")
_REQUIRED_COLUMNS = {"date", "open", "high", "low", "close"}
_OPTIONAL_COLUMNS = {"volume", "symbol"}
_ALL_COLUMNS = _REQUIRED_COLUMNS | _OPTIONAL_COLUMNS


class PilotDataError(ValueError):
    """Sanitized pilot market-data precondition failure."""


def _local_file(path: Path, *, name: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise PilotDataError(f"{name} must be an existing regular local file")
    return path


def _price(text: str, *, name: str, row: int) -> Decimal:
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ArithmeticError, ValueError) as exc:
        raise PilotDataError(f"row {row}: {name} is not a decimal number") from exc
    if not value.is_finite() or value <= 0:
        raise PilotDataError(f"row {row}: {name} must be finite and positive")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -3:
        raise PilotDataError(f"row {row}: {name} exceeds the 0.001 declared tick")
    return value


def _snapshot_id(payload: dict[str, Any]) -> str:
    return "cnpilot-" + digest(payload)[:24]


def _publish_snapshot(
    root: Path, snapshot_id: str, frame: pl.DataFrame, document: dict[str, Any]
) -> dict[str, Any]:
    directory = root / "data" / snapshot_id
    if directory.exists() or directory.is_symlink():
        existing = read_json(directory / "snapshot.json")
        stored = pl.read_parquet(directory / "bars.parquet")
        if (
            existing != document
            or logical_content_hash(stored) != document["content_hash"]
        ):
            raise PilotDataError("SNAPSHOT_ID_CONFLICT")
        return existing
    directory.mkdir(parents=True)
    temporary = directory / ".pending-bars.parquet"
    frame.write_parquet(temporary)
    os.replace(temporary, directory / "bars.parquet")
    publish(directory / "snapshot.json", document)
    return document


def _frame_from_records(
    symbol: str, records: list[tuple[date, Decimal, Decimal, Decimal, Decimal, float]]
) -> pl.DataFrame:
    frame = pl.DataFrame(
        {
            "symbol": [symbol] * len(records),
            "timestamp": [
                datetime.combine(day, time.min, tzinfo=UTC) for day, *_ in records
            ],
            "open": [float(item[1]) for item in records],
            "high": [float(item[2]) for item in records],
            "low": [float(item[3]) for item in records],
            "close": [float(item[4]) for item in records],
            "volume": [item[5] for item in records],
        },
        schema=BAR_SCHEMA,
    )
    return frame


def _parse_csv_records(
    csv_path: Path, symbol: str, *, minimum_rows: int = 3
) -> tuple[list[tuple[date, Decimal, Decimal, Decimal, Decimal, float]], bytes]:
    if not symbol or symbol.strip() != symbol or len(symbol) > 32:
        raise PilotDataError("symbol must be a trimmed nonempty identifier")
    source = _local_file(csv_path, name="csv")
    if source.stat().st_size > _MAX_CSV_BYTES:
        raise PilotDataError("csv exceeds the bounded import size")
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PilotDataError("csv must be utf-8 text") from exc
    reader = csv.DictReader(text.splitlines())
    header = [column.strip().lower() for column in (reader.fieldnames or [])]
    if not _REQUIRED_COLUMNS <= set(header) or not set(header) <= _ALL_COLUMNS:
        raise PilotDataError(
            f"csv columns must be exactly a subset containing {sorted(_REQUIRED_COLUMNS)} "
            f"of {sorted(_ALL_COLUMNS)}"
        )
    records: list[tuple[date, Decimal, Decimal, Decimal, Decimal, float]] = []
    previous: date | None = None
    for row_number, row in enumerate(reader, start=2):
        if row_number - 1 > _MAX_ROWS:
            raise PilotDataError("csv exceeds the bounded row count")
        values = {
            (key or "").strip().lower(): (value or "").strip()
            for key, value in row.items()
        }
        if values.get("symbol") and values["symbol"] != symbol:
            raise PilotDataError(f"row {row_number}: symbol does not match import")
        try:
            day = date.fromisoformat(values["date"])
        except ValueError as exc:
            raise PilotDataError(
                f"row {row_number}: date must be ISO YYYY-MM-DD"
            ) from exc
        if previous is not None and day <= previous:
            raise PilotDataError("csv dates must be strictly ascending and unique")
        previous = day
        open_ = _price(values["open"], name="open", row=row_number)
        high = _price(values["high"], name="high", row=row_number)
        low = _price(values["low"], name="low", row=row_number)
        close = _price(values["close"], name="close", row=row_number)
        volume_text = values.get("volume", "")
        if volume_text:
            try:
                volume = float(Decimal(volume_text))
            except (InvalidOperation, ArithmeticError, ValueError) as exc:
                raise PilotDataError(
                    f"row {row_number}: volume is not a number"
                ) from exc
            if not Decimal(volume_text).is_finite() or volume < 0:
                raise PilotDataError(
                    f"row {row_number}: volume must be finite and nonnegative"
                )
        else:
            volume = 0.0
        records.append((day, open_, high, low, close, volume))
    if len(records) < minimum_rows:
        raise PilotDataError(f"csv must contain at least {minimum_rows} observation(s)")
    return records, raw


_EXTENSIONS_SCHEMA = "cn-pilot-snapshot-extensions-v1"


def _read_extensions(directory: Path) -> list[dict[str, Any]]:
    path = directory / "extensions.json"
    if not path.exists() or path.is_symlink():
        return []
    value = read_json(path)
    if (
        set(value) != {"schema", "revisions"}
        or value["schema"] != _EXTENSIONS_SCHEMA
        or not isinstance(value["revisions"], list)
        or not value["revisions"]
    ):
        raise PilotDataError("malformed snapshot extension record")
    revisions = value["revisions"]
    for revision in revisions:
        if not isinstance(revision, dict) or set(revision) != {
            "revision",
            "prior_content_hash",
            "new_content_hash",
            "prior_rows",
            "new_rows",
            "appended_from",
            "appended_to",
            "file_sha256",
            "declared_adjustment",
            "as_of",
        }:
            raise PilotDataError("malformed snapshot extension record")
    for index, revision in enumerate(revisions, start=1):
        if revision["revision"] != index:
            raise PilotDataError("snapshot extension chain is discontinuous")
    return revisions


def _verify_revisions(
    frame: pl.DataFrame, document: dict[str, Any], extensions: list[dict[str, Any]]
) -> None:
    prior_hash = document["content_hash"]
    prior_rows = document["rows"]
    for revision in extensions:
        if revision["prior_content_hash"] != prior_hash:
            raise PilotDataError("snapshot extension chain is broken")
        if revision["prior_rows"] != prior_rows:
            raise PilotDataError("snapshot extension row chain is broken")
        prefix = frame.head(prior_rows)
        if logical_content_hash(prefix) != prior_hash:
            raise PilotDataError("snapshot prefix was modified after extension")
        prior_hash = revision["new_content_hash"]
        prior_rows = revision["new_rows"]
    if logical_content_hash(frame) != prior_hash or frame.height != prior_rows:
        raise PilotDataError("snapshot content integrity mismatch")


def _replace_json(path: Path, value: Any) -> None:
    temporary = path.parent / (".pending-" + path.name)
    with temporary.open("wb") as stream:
        stream.write(canonical_json(value).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def import_bars_csv(
    root: Path,
    csv_path: Path,
    symbol: str,
    *,
    as_of: datetime,
    declared_adjustment: str,
) -> dict[str, Any]:
    """Admit one operator-supplied CNY daily-bar CSV as a pilot snapshot."""
    if not declared_adjustment.strip():
        raise PilotDataError("declared_adjustment must state the source adjustment")
    records, raw = _parse_csv_records(csv_path, symbol)
    frame = prepare_bars(_frame_from_records(symbol, records), as_of=as_of)
    content_hash = logical_content_hash(frame)
    identity = {
        "symbol": symbol,
        "content_hash": content_hash,
        "rows": frame.height,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
    }
    snapshot_id = _snapshot_id(identity)
    document = {
        "schema": "cn-pilot-snapshot-v1",
        "snapshot_id": snapshot_id,
        "symbol": symbol,
        "source": {
            "kind": "operator-csv",
            "file_name": Path(csv_path).name,
            "file_sha256": identity["file_sha256"],
            "declared_adjustment": declared_adjustment.strip(),
            "unit_claim": "operator-supplied bar values; CNY unit and adjustment are the operator's declaration, not independently certified",
        },
        "rows": frame.height,
        "min_session": frame["timestamp"][0].date().isoformat(),
        "max_session": frame["timestamp"][-1].date().isoformat(),
        "content_hash": content_hash,
        "as_of": as_of.isoformat(),
        "tick": "0.001",
        "limitations": [
            "No exchange-calendar certification; gap days are unclassified.",
            "No corporate-action ledger; raw-price discontinuities remain the operator's declaration.",
            "No entitlement, liquidity or capacity claim; source bytes are retained locally only.",
        ],
    }
    return _publish_snapshot(root, snapshot_id, frame, document)


def extend_bars_csv(
    root: Path,
    snapshot_id: str,
    csv_path: Path,
    symbol: str,
    *,
    as_of: datetime,
    declared_adjustment: str,
) -> dict[str, Any]:
    """Append strictly later sessions to an existing snapshot, audited by revision.

    Existing rows can never change: the recomputed prefix hash must equal the
    prior revision's recorded hash, new dates must be strictly after the last
    retained session, and the append is recorded in the canonical
    ``extensions.json`` chain before it becomes visible to the pilot. The host
    writer lock is held so an extension cannot interleave with a pilot run.
    """
    root = local_root(root)
    with writer_lock(root):
        return _extend_locked(
            root,
            snapshot_id,
            csv_path,
            symbol,
            as_of=as_of,
            declared_adjustment=declared_adjustment,
        )


def _extend_locked(
    root: Path,
    snapshot_id: str,
    csv_path: Path,
    symbol: str,
    *,
    as_of: datetime,
    declared_adjustment: str,
) -> dict[str, Any]:
    if not declared_adjustment.strip():
        raise PilotDataError("declared_adjustment must state the source adjustment")
    if (
        not snapshot_id
        or "/" in snapshot_id
        or "\\" in snapshot_id
        or snapshot_id.strip() != snapshot_id
    ):
        raise PilotDataError("invalid snapshot identifier")
    directory = root / "data" / snapshot_id
    if not directory.is_dir() or directory.is_symlink():
        raise PilotDataError("requested snapshot is unavailable")
    existing_frame, document = read_snapshot(root, snapshot_id)
    if document["symbol"] != symbol:
        raise PilotDataError("snapshot symbol differs from extension")
    extensions = _read_extensions(directory)
    records, raw = _parse_csv_records(csv_path, symbol, minimum_rows=1)
    last_session = (
        extensions[-1]["appended_to"] if extensions else document["max_session"]
    )
    first_new = records[0][0].isoformat()
    if first_new <= last_session:
        raise PilotDataError(
            "extension rows must be strictly later than the retained last session"
        )
    appended = prepare_bars(_frame_from_records(symbol, records), as_of=as_of)
    combined = prepare_bars(
        pl.concat([existing_frame, appended], how="vertical"), as_of=as_of
    )
    prior_hash = (
        extensions[-1]["new_content_hash"] if extensions else document["content_hash"]
    )
    prior_rows = extensions[-1]["new_rows"] if extensions else document["rows"]
    if (
        combined.height != prior_rows + appended.height
        or logical_content_hash(combined.head(prior_rows)) != prior_hash
    ):
        raise PilotDataError("snapshot prefix changed during extension")
    revision = {
        "revision": len(extensions) + 1,
        "prior_content_hash": prior_hash,
        "new_content_hash": logical_content_hash(combined),
        "prior_rows": prior_rows,
        "new_rows": combined.height,
        "appended_from": first_new,
        "appended_to": records[-1][0].isoformat(),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "declared_adjustment": declared_adjustment.strip(),
        "as_of": as_of.isoformat(),
    }
    temporary = directory / ".pending-bars.parquet"
    combined.write_parquet(temporary)
    os.replace(temporary, directory / "bars.parquet")
    _replace_json(
        directory / "extensions.json",
        {"schema": _EXTENSIONS_SCHEMA, "revisions": [*extensions, revision]},
    )
    return {
        **document,
        "rows": combined.height,
        "max_session": revision["appended_to"],
        "extensions": len(extensions) + 1,
    }


def corpus_snapshot(
    root: Path,
    provider: Path,
    universe: dict[str, Any],
    symbol: str,
    *,
    as_of: datetime,
) -> dict[str, Any]:
    """Recover one authenticated corpus member in the raw vendor price scale."""
    frames, qualification = load_qlib_corpus(
        provider, universe, as_of=as_of, price_scale="raw"
    )
    if symbol not in frames:
        raise PilotDataError("symbol is not a member of the declared universe")
    frame = frames[symbol]
    rounded: list[dict[str, Any]] = []
    for row in frame.iter_rows(named=True):
        prices = {}
        for field in ("open", "high", "low", "close"):
            value = Decimal(repr(row[field]))
            ticked = value.quantize(_TICK, rounding=ROUND_HALF_UP)
            if (ticked - value).copy_abs() > _TICK_TOLERANCE:
                raise PilotDataError(
                    "recovered price is not on the declared 0.01 CNY tick"
                )
            prices[field] = float(ticked)
        rounded.append(
            {
                "symbol": symbol,
                "timestamp": row["timestamp"],
                **prices,
                "volume": row["volume"],
            }
        )
    recovered = prepare_bars(pl.DataFrame(rounded, schema=BAR_SCHEMA), as_of=as_of)
    content_hash = logical_content_hash(recovered)
    identity = {
        "symbol": symbol,
        "content_hash": content_hash,
        "rows": recovered.height,
        "corpus_id": qualification["corpus_id"],
    }
    snapshot_id = _snapshot_id(identity)
    observed = qualification["classifications"]["OBSERVED_FACT"]
    document = {
        "schema": "cn-pilot-snapshot-v1",
        "snapshot_id": snapshot_id,
        "symbol": symbol,
        "source": {
            "kind": "corpus-raw-scale",
            "provider_snapshot": observed["snapshot"],
            "archive_sha256": qualification["source"]["archive_sha256"],
            "universe_id": qualification["universe_id"],
            "universe_sha256": qualification["universe_sha256"],
            "corpus_id": qualification["corpus_id"],
            "corpus_sha256": qualification["corpus_sha256"],
            "recovery": "OHLC/factor rounded half-up to the 0.01 CNY stock tick; volume*factor retained as vendor scale",
            "unit_claim": "recovered vendor raw-price scale presumed CNY/share; not an exchange legal quote and not independently certified",
        },
        "rows": recovered.height,
        "min_session": recovered["timestamp"][0].date().isoformat(),
        "max_session": recovered["timestamp"][-1].date().isoformat(),
        "content_hash": content_hash,
        "as_of": as_of.isoformat(),
        "tick": "0.01",
        "limitations": [
            *qualification["classifications"]["LIMITATION"],
            "Recovered raw prices carry vendor float32 precision before tick rounding; the recovery formula is a retained local claim.",
            "Raw-scale prices keep corporate-action discontinuities; dividends and splits are not modelled.",
        ],
    }
    return _publish_snapshot(root, snapshot_id, recovered, document)


def list_snapshots(root: Path) -> list[dict[str, Any]]:
    directory = root / "data"
    if not directory.is_dir():
        return []
    result = []
    for child in sorted(directory.iterdir()):
        if child.is_dir() and (child / "snapshot.json").is_file():
            result.append(read_json(child / "snapshot.json"))
    return result


def read_snapshot(root: Path, snapshot_id: str) -> tuple[pl.DataFrame, dict[str, Any]]:
    if (
        not snapshot_id
        or "/" in snapshot_id
        or "\\" in snapshot_id
        or snapshot_id.strip() != snapshot_id
    ):
        raise PilotDataError("invalid snapshot identifier")
    directory = root / "data" / snapshot_id
    if not directory.is_dir() or directory.is_symlink():
        raise PilotDataError("requested snapshot is unavailable")
    document = read_json(directory / "snapshot.json")
    if document.get("snapshot_id") != snapshot_id:
        raise PilotDataError("snapshot identity mismatch")
    frame = pl.read_parquet(directory / "bars.parquet")
    extensions = _read_extensions(directory)
    _verify_revisions(frame, document, extensions)
    if extensions:
        document = {
            **document,
            "rows": extensions[-1]["new_rows"],
            "max_session": extensions[-1]["appended_to"],
            "extensions": len(extensions),
        }
    return frame, document
