"""Offline qualification of one authenticated retrospective Qlib snapshot.

Native Qlib OHLC are adjusted and normalized, not executable currency quotes.
This adapter admits a fixed research universe without claiming institutional
PIT, total-return accounting, or calibrated liquidity/capacity measurements.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import tarfile
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Literal

import polars as pl

from quantforge.data import BAR_SCHEMA, logical_content_hash, prepare_bars
from quantforge.data.bars import normalize_utc
from quantforge.experiments import canonical_json

_FIELDS = ("open", "high", "low", "close", "volume", "factor")
_SYMBOL = re.compile(r"(?:SH6|SZ0|SZ3)[0-9]{5}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SNAPSHOT = "investment_data/2026-09-15"
_ARCHIVE_SHA256 = "0d933f3fb0e4604f67ef805dd8c96ec11df02f262aa551bd7151d87ae91379ae"
_ARCHIVE_BYTES = 565_276_687
_SOURCE_COMMIT = "b8c129b4d9b838f050eac8b1135111b3b79fc894"
_QLIB_COMMIT = "b87a2c294d364a33fb739359886acffe8ec907d1"
_MANIFEST_FIXED = {
    "release_tag": "2026-09-15",
    "target_trade_date": "2026-09-15",
    "future_start_date": "2026-09-16",
    "future_end_date": "2026-12-31",
    "dolt_commit": "olv24bv9h7s235dlurur18ecst0ravf3",
    "investment_data_commit": _SOURCE_COMMIT,
    "qlib_commit": _QLIB_COMMIT,
    "image_digest": "sha256:499bf932b7b967b6e5a06d120577a3fbed9d4fcf851dc0f6a1564d052e129f4e",
}
_SOURCE_BASE = f"https://github.com/chenditc/investment_data/blob/{_SOURCE_COMMIT}"
_QLIB_BASE = f"https://github.com/microsoft/qlib/blob/{_QLIB_COMMIT}"
_UNIVERSE_KEYS = {
    "schema",
    "program_id",
    "observation_date",
    "provider_snapshot",
    "membership_source",
    "membership_source_sha256",
    "calendar_source_sha256",
    "start",
    "end",
    "selection_count",
    "membership_rule",
    "selection_channel_count",
    "supersedes_metadata_only_proposal",
    "replacement_policy",
    "strategy_class",
    "pit_classification",
    "survivorship_classification",
    "limitations",
    "members",
    "universe_sha256",
    "universe_id",
}


class CorpusQualificationError(ValueError):
    """Source identity or declared data semantics could not be established."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise CorpusQualificationError(reason)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(payload: dict[str, Any]) -> bytes:
    # This is the already-frozen universe precommit ABI, not a new JSON dialect.
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _day(value: Any) -> date:
    _require(isinstance(value, str), "canonical session date required")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise CorpusQualificationError("invalid session date") from None
    _require(result.isoformat() == value, "noncanonical session date")
    return result


def validate_universe(universe: dict[str, Any]) -> str:
    """Validate the immutable metadata-only universe artifact; return its id."""
    _require(
        isinstance(universe, dict) and set(universe) == _UNIVERSE_KEYS,
        "universe schema/metadata mismatch",
    )
    _require(
        universe["schema"] == "research-scale-universe-precommit-v1",
        "unsupported universe schema",
    )
    _require(universe["provider_snapshot"] == _SNAPSHOT, "unsupported source snapshot")
    _require(
        universe["membership_source"] == "cn_data/instruments/all.txt",
        "membership source mismatch",
    )
    _require(
        universe["pit_classification"] == "RETROSPECTIVE_NOT_INSTITUTIONAL_PIT",
        "unsupported PIT claim",
    )
    _require(
        universe["survivorship_classification"]
        == "AVAILABILITY_SELECTED_SURVIVOR_SUBSET",
        "unsupported survivorship claim",
    )
    for field in (
        "membership_source_sha256",
        "calendar_source_sha256",
        "universe_sha256",
    ):
        _require(
            isinstance(universe[field], str)
            and _DIGEST.fullmatch(universe[field]) is not None,
            "invalid universe digest",
        )
    start, end, observed = (
        _day(universe[field]) for field in ("start", "end", "observation_date")
    )
    _require(start <= end <= observed, "universe date order/future mismatch")
    _require(
        type(universe["selection_count"]) is int and universe["selection_count"] == 5,
        "fixed five-member universe required",
    )
    _require(
        type(universe["selection_channel_count"]) is int
        and universe["selection_channel_count"] == 2,
        "metadata proposal accounting mismatch",
    )
    for field in (
        "program_id",
        "membership_rule",
        "supersedes_metadata_only_proposal",
        "replacement_policy",
        "strategy_class",
    ):
        _require(
            isinstance(universe[field], str) and bool(universe[field].strip()),
            "missing universe declaration",
        )
    limits = universe["limitations"]
    _require(
        isinstance(limits, list)
        and bool(limits)
        and all(isinstance(item, str) and item.strip() for item in limits),
        "missing universe limitations",
    )
    members = universe["members"]
    _require(
        isinstance(members, list) and len(members) == 5,
        "universe member count mismatch",
    )
    symbols = []
    for member in members:
        _require(
            isinstance(member, dict)
            and set(member) == {"symbol", "source_start", "source_end"},
            "member metadata mismatch",
        )
        symbol = member["symbol"]
        _require(
            isinstance(symbol, str) and _SYMBOL.fullmatch(symbol) is not None,
            "unsupported or renamed symbol",
        )
        _require(
            _day(member["source_start"]) <= start and _day(member["source_end"]) >= end,
            "member availability mismatch",
        )
        symbols.append(symbol)
    _require(symbols == sorted(set(symbols)), "duplicate or unordered universe members")
    semantic = {
        key: value
        for key, value in universe.items()
        if key not in {"universe_id", "universe_sha256"}
    }
    expected = _digest(_canonical(semantic))
    _require(
        universe["universe_sha256"] == expected
        and universe["universe_id"] == "qf-cn-fixed5-" + expected[:24],
        "universe identity mismatch",
    )
    return str(universe["universe_id"])


def _read_file(path: Path, root: Path) -> bytes:
    _require(
        path.is_file() and not path.is_symlink(),
        "missing or redirected source artifact",
    )
    _require(
        path.resolve().is_relative_to(root.resolve()),
        "source artifact escapes provider root",
    )
    _require(
        path.stat().st_size <= 10_000_000, "source artifact exceeds resource bound"
    )
    return path.read_bytes()


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate source manifest key")
        result[key] = value
    return result


def _authenticate(
    provider: Path, universe: dict[str, Any]
) -> tuple[dict[str, bytes], dict[str, Any]]:
    root = provider.parent
    manifest_raw = _read_file(root / "qlib_bin.manifest.json", root)
    try:
        manifest = json.loads(manifest_raw, object_pairs_hook=_unique_pairs)
    except CorpusQualificationError:
        raise
    except (UnicodeError, ValueError):
        raise CorpusQualificationError("invalid source manifest") from None
    expected_manifest = {
        **_MANIFEST_FIXED,
        "archive_size_bytes": _ARCHIVE_BYTES,
        "archive_sha256": "sha256:" + _ARCHIVE_SHA256,
    }
    _require(manifest == expected_manifest, "pinned source manifest mismatch")
    archive_path = root / "qlib_bin.tar.gz"
    _require(
        archive_path.is_file()
        and not archive_path.is_symlink()
        and archive_path.stat().st_size == _ARCHIVE_BYTES,
        "source archive size/path mismatch",
    )
    digest = hashlib.sha256()
    with archive_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    _require(digest.hexdigest() == _ARCHIVE_SHA256, "source archive hash mismatch")
    paths = {"calendars/day.txt", "instruments/all.txt"}
    paths.update(
        f"features/{member['symbol'].lower()}/{field}.day.bin"
        for member in universe["members"]
        for field in _FIELDS
    )
    raw_files = {
        relative: _read_file(provider / relative, provider) for relative in paths
    }
    found: set[str] = set()
    try:
        with tarfile.open(archive_path, mode="r|gz") as archive:
            for member in archive:
                relative = member.name.removeprefix("qlib_bin/")
                if not member.name.startswith("qlib_bin/") or relative not in paths:
                    continue
                _require(
                    relative not in found
                    and member.isfile()
                    and member.size == len(raw_files[relative]),
                    "duplicate or malformed source archive member",
                )
                extracted = archive.extractfile(member)
                _require(extracted is not None, "unreadable source archive member")
                assert extracted is not None
                _require(
                    extracted.read(member.size + 1) == raw_files[relative],
                    "extracted source artifact hash mismatch",
                )
                found.add(relative)
    except (tarfile.TarError, EOFError, OSError):
        raise CorpusQualificationError("unreadable source archive") from None
    _require(found == paths, "source archive misses selected artifact")
    _require(
        _digest(raw_files["calendars/day.txt"]) == universe["calendar_source_sha256"],
        "calendar source substitution",
    )
    _require(
        _digest(raw_files["instruments/all.txt"])
        == universe["membership_source_sha256"],
        "membership source substitution",
    )
    source = {
        "provider_snapshot": _SNAPSHOT,
        "archive_sha256": _ARCHIVE_SHA256,
        "archive_size_bytes": _ARCHIVE_BYTES,
        "manifest_sha256": _digest(manifest_raw),
        "manifest": manifest,
        "selected_raw_sha256": {
            key: _digest(value) for key, value in sorted(raw_files.items())
        },
    }
    return raw_files, source


def _decode(raw: bytes, calendar_count: int) -> dict[int, float]:
    _require(len(raw) >= 8 and len(raw) % 4 == 0, "malformed float32 feature length")
    header = struct.unpack("<f", raw[:4])[0]
    _require(
        math.isfinite(header) and header.is_integer() and 0 <= header < calendar_count,
        "invalid feature calendar header",
    )
    start = int(header)
    count = len(raw) // 4 - 1
    _require(start + count <= calendar_count, "feature extends beyond source calendar")
    return {
        start + offset: value[0]
        for offset, value in enumerate(struct.iter_unpack("<f", raw[4:]))
    }


def _scaled(
    field: str,
    fields: dict[str, dict[int, float]],
    index: int,
    price_scale: str,
) -> float:
    """One aligned cell in the requested declared scale.

    ``normalized`` returns the qualified research unit unchanged. ``raw``
    recovers the source's unadjusted price scale as ``OHLC / factor`` and the
    vendor volume scale as ``volume * factor``; source cells are float32, so
    recovered values are vendor claims at float32 precision, never quotes.
    """
    value = fields[field][index]
    if price_scale == "normalized":
        return value
    factor = fields["factor"][index]
    return value * factor if field == "volume" else value / factor


def load_qlib_corpus(
    provider: Path,
    universe: dict[str, Any],
    *,
    as_of: datetime,
    price_scale: Literal["normalized", "raw"] = "normalized",
) -> tuple[dict[str, pl.DataFrame], dict[str, Any]]:
    """Authenticate, qualify and align research-unit OHLCV without a Qlib SDK.

    Missing source cells are retained as diagnostics. The common complete-date
    intersection is applied once to all symbols, with no imputation or symbol
    replacement. Dropped sessions remain in the coverage evidence; adjacency
    means consecutive available common sessions, not guaranteed daily spacing.

    ``price_scale="raw"`` is an additive, opt-in CN-pilot bridge that recovers
    the source's unadjusted price scale as ``OHLC / factor`` and vendor volume
    scale as ``volume * factor`` (float32 source precision retained) for the
    same authenticated and aligned rows. The default ``"normalized"`` path is
    byte-identical to the qualified scale behavior; recovered raw values remain
    vendor claims with no independently certified CNY or capacity semantics.
    """
    _require(
        price_scale in ("normalized", "raw"),
        "unsupported price scale request",
    )
    as_of_utc = normalize_utc(as_of, name="as_of")
    universe_id = validate_universe(universe)
    _require(
        _day(universe["observation_date"]) <= as_of_utc.date(),
        "universe declaration after as_of",
    )
    _require(
        _day(_MANIFEST_FIXED["release_tag"]) <= as_of_utc.date(),
        "source vintage after as_of",
    )
    provider = Path(provider)
    _require(
        provider.is_dir() and not provider.is_symlink(),
        "provider root unavailable or redirected",
    )
    raw_files, source = _authenticate(provider, universe)
    try:
        calendar_text = raw_files["calendars/day.txt"].decode("utf-8")
        calendar = calendar_text.splitlines()
        _require(
            bool(calendar) and calendar == sorted(set(calendar)),
            "duplicate or nonmonotone source calendar",
        )
        dates = [_day(day) for day in calendar]
        _require(
            dates[-1].isoformat() == _MANIFEST_FIXED["target_trade_date"],
            "source calendar vintage mismatch",
        )
        membership_lines = raw_files["instruments/all.txt"].decode("utf-8").splitlines()
        membership_rows = [line.split("\t") for line in membership_lines]
        _require(
            all(len(row) == 3 for row in membership_rows), "malformed source membership"
        )
        tuples = [tuple(row) for row in membership_rows]
        _require(
            tuples == sorted(set(tuples)), "duplicate or unordered source membership"
        )
        for _, first, last in membership_rows:
            _require(_day(first) <= _day(last), "invalid membership interval")
        start, end = universe["start"], universe["end"]
        eligible = [
            {"symbol": symbol, "source_start": first, "source_end": last}
            for symbol, first, last in membership_rows
            if _SYMBOL.fullmatch(symbol) is not None
            and first <= start
            and last >= end
            and all(
                (provider / "features" / symbol.lower() / f"{field}.day.bin").is_file()
                and (provider / "features" / symbol.lower() / f"{field}.day.bin")
                .stat()
                .st_size
                >= 8
                for field in _FIELDS
            )
        ]
        _require(
            eligible[:5] == universe["members"],
            "universe substitution or availability rule mismatch",
        )
        indices = [index for index, day in enumerate(calendar) if start <= day <= end]
        _require(len(indices) >= 3, "insufficient declared calendar coverage")
        decoded: dict[str, dict[str, dict[int, float]]] = {}
        complete: dict[str, set[int]] = {}
        diagnostics: dict[str, Any] = {}
        for member in universe["members"]:
            symbol = member["symbol"]
            fields = {
                field: _decode(
                    raw_files[f"features/{symbol.lower()}/{field}.day.bin"],
                    len(calendar),
                )
                for field in _FIELDS
            }
            decoded[symbol] = fields
            valid: set[int] = set()
            missing_factor = 0
            for index in indices:
                values = {
                    field: fields[field].get(index, math.nan) for field in _FIELDS
                }
                _require(
                    not any(math.isinf(value) for value in values.values()),
                    "infinite source observation",
                )
                _require(
                    all(
                        math.isnan(value) or value > 0
                        for field, value in values.items()
                        if field != "volume"
                    ),
                    "nonpositive price/restoration factor",
                )
                _require(
                    math.isnan(values["volume"]) or values["volume"] >= 0,
                    "negative source volume",
                )
                # Missing one field must not hide a contradiction between other
                # observed prices. Compare every available finite pair before
                # deciding whether the row qualifies for the common calendar.
                for upper, lower in (
                    ("high", "low"),
                    ("high", "open"),
                    ("high", "close"),
                    ("open", "low"),
                    ("close", "low"),
                ):
                    if math.isfinite(values[upper]) and math.isfinite(values[lower]):
                        _require(
                            values[upper] >= values[lower],
                            "observed partial OHLC relationships are invalid",
                        )
                prices_observed = any(
                    not math.isnan(values[field])
                    for field in ("open", "high", "low", "close")
                )
                if prices_observed and math.isnan(values["factor"]):
                    missing_factor += 1
                if all(math.isfinite(value) for value in values.values()):
                    valid.add(index)
            _require(
                missing_factor == 0,
                "missing corporate-action restoration metadata on observed prices",
            )
            complete[symbol] = valid
            source_records = [
                {
                    "symbol": symbol,
                    "timestamp": datetime.combine(dates[index], time.min, tzinfo=UTC),
                    **{
                        field: fields[field][index]
                        for field in _FIELDS
                        if field != "factor"
                    },
                }
                for index in sorted(valid)
            ]
            if source_records:
                # Validate every complete selected-source row, even if another
                # asset is missing that date and the common intersection drops it.
                prepare_bars(
                    pl.DataFrame(source_records, schema=BAR_SCHEMA), as_of=as_of_utc
                )
            diagnostics[symbol] = {
                "declared_sessions": len(indices),
                "complete_source_sessions": len(valid),
                "missing_source_sessions": len(indices) - len(valid),
                "factor_present_positive_on_observed_prices": True,
            }
        common = sorted(set.intersection(*complete.values()))
        _require(len(common) >= 3, "insufficient common complete coverage")
        frames: dict[str, pl.DataFrame] = {}
        for symbol, fields in decoded.items():
            records = [
                {
                    "symbol": symbol,
                    "timestamp": datetime.combine(dates[index], time.min, tzinfo=UTC),
                    **{
                        field: _scaled(field, fields, index, price_scale)
                        for field in _FIELDS
                        if field != "factor"
                    },
                }
                for index in common
            ]
            frames[symbol] = prepare_bars(
                pl.DataFrame(records, schema=BAR_SCHEMA), as_of=as_of_utc
            )
        content_hashes = {
            symbol: logical_content_hash(frame)
            for symbol, frame in sorted(frames.items())
        }
        qualification = {
            "schema": "research-scale-corpus-qualification-v1",
            "universe_id": universe_id,
            "universe_sha256": universe["universe_sha256"],
            "source": source,
            "content_hashes": content_hashes,
            "as_of": as_of_utc.isoformat(),
            "alignment": {
                "policy": "COMMON_COMPLETE_INTERSECTION_NO_FILL_NO_REPLACEMENT",
                "declared_sessions": len(indices),
                "common_sessions": len(common),
                "removed_session_dates": [
                    calendar[index] for index in indices if index not in common
                ],
                "common_calendar_sha256": _digest(
                    _canonical({"dates": [calendar[index] for index in common]})
                ),
                "row_counts": {
                    symbol: frame.height for symbol, frame in sorted(frames.items())
                },
            },
            "diagnostics": diagnostics,
            "classifications": {
                "OBSERVED_FACT": {
                    "snapshot": _SNAPSHOT,
                    "authenticated_archive_sha256": _ARCHIVE_SHA256,
                    "selected_extraction_equals_archive": True,
                    "universe_declarations": universe["selection_channel_count"],
                    "prior_index_identifier_proposal": "Retained metadata-only proposal was superseded before prices because SH000xxx were indices; not a performance search.",
                },
                "SOURCE_CLAIM": {
                    "source_type": "retrospective merged crowdsourced daily A-share database",
                    "normalization": "OHLC multiplied by adjclose/original_close, then divided by first valid adjusted close; factor also divided by first valid adjusted close; volume inversely adjusted and multiplied by first valid adjusted close.",
                    "calendar": "Tushare SSE open-session calendar according to pinned exporter",
                    "references": [
                        _SOURCE_BASE + "/README.md",
                        _SOURCE_BASE + "/qlib/normalize.py",
                        _SOURCE_BASE + "/qlib/dump_all_to_qlib_source.py",
                        _QLIB_BASE + "/scripts/data_collector/yahoo/collector.py",
                        _QLIB_BASE + "/qlib/data/storage/file_storage.py",
                    ],
                },
                "LOCAL_VALIDATION": {
                    "native_bar_schema_ohlcv_hashes": "PASS",
                    "archive_manifest_extraction_binding": "PASS",
                    "calendar_duplicates": "NONE",
                    "membership_duplicates": "NONE",
                    "symbol_rule": "SH6/SZ0/SZ3 exchange-qualified identifiers; exact immutable membership binding",
                    "future_observations": "NONE_IN_ADMITTED_ROWS",
                    "restoration_factor": "REQUIRED_FINITE_POSITIVE_ON_OBSERVED_ROWS",
                },
                "ASSUMPTION": {
                    "timestamp": "Chinese session date encoded as UTC midnight label, not observed publication/intraday time",
                    "price_unit": "QLIB_FIRST_CLOSE_NORMALIZED_ADJUSTED_PRICE",
                    "volume_unit": "QLIB_ADJUSTED_VENDOR_VOLUME_ORIGINAL_SCALE_UNPROVEN",
                    "cash_unit_if_simulated": "NORMALIZED_RESEARCH_UNITS_ONLY; not CNY economic accounting",
                    "corporate_actions": "Source adjclose-based adjustment retained; no separate entitlement ledger or ex-date/PIT guarantee.",
                },
                "LIMITATION": [
                    *universe["limitations"],
                    "Underlying source combines Wind/Caihui/Tushare/Akshare/Yahoo/Baostock into one explicitly identified final table; no per-row upstream identity or vintage proof.",
                    "Normalized price levels are not executable CNY quotes; normalized volume is not established share volume. Calibrated capacity and real-money fills are not supported.",
                    "No independently verified dividend/split/delisting or symbol-renaming ledger; source adjustment semantics do not prove total-return reinvestment.",
                    "No institutional point-in-time or survivorship-free claim; historical revisions and availability selection remain.",
                    "Intersection omits unavailable sessions for every asset and does not imply consecutive exchange trading days; information horizons must use retained source-session identity.",
                    "Source snapshot hashes authenticate this distribution, not the accuracy of the originating vendors.",
                ],
                "LICENSE / DISTRIBUTION NOTE": {
                    "repository_license": "Apache-2.0",
                    "license_reference": _SOURCE_BASE + "/LICENSE",
                    "license_sha256": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
                    "upstream_market_data_entitlements": "NOT_INDEPENDENTLY_ESTABLISHED",
                    "intended_use": "Already-authorized local qualification research; no raw-data redistribution or commercial entitlement claim.",
                },
            },
        }
        if price_scale == "raw":
            # Additive opt-in block; the normalized path stays byte-identical.
            qualification["price_scale"] = {
                "mode": "raw",
                "recovery": "OHLC/factor for prices; volume*factor for vendor volume scale",
                "source_precision": "float32 feature cells retained; recovered values are not exact quotes",
                "cny_claim": "CN-pilot paper-use hypothesis only; CNY executability and entitlement are not certified",
                "limits": "No corporate-action ledger, no exchange calendar proof, no capacity or real-money fill claim.",
            }
        qualification["corpus_sha256"] = _digest(
            canonical_json(qualification).encode("utf-8")
        )
        qualification["corpus_id"] = (
            "qf-qlib-corpus-" + qualification["corpus_sha256"][:24]
        )
        return frames, qualification
    except (UnicodeError, KeyError, TypeError, struct.error):
        raise CorpusQualificationError("malformed source artifact") from None
