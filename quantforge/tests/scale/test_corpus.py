from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import struct
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from quantforge.product.scale import corpus

SYMBOLS = ("SH600000", "SH600004", "SH600006", "SH600007", "SH600008")
AS_OF = datetime(2026, 10, 10, tzinfo=UTC)


def _bind_universe(value: dict[str, Any]) -> dict[str, Any]:
    semantic = {
        key: item
        for key, item in value.items()
        if key not in {"universe_sha256", "universe_id"}
    }
    digest = hashlib.sha256(
        json.dumps(
            semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    value["universe_sha256"] = digest
    value["universe_id"] = "qf-cn-fixed5-" + digest[:24]
    return value


def _write_archive(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = root / "qlib_bin.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        for path in sorted((root / "cn_data").rglob("*")):
            if path.is_file():
                raw = path.read_bytes()
                item = tarfile.TarInfo(
                    "qlib_bin/" + path.relative_to(root / "cn_data").as_posix()
                )
                item.size = len(raw)
                target.addfile(item, io.BytesIO(raw))
    raw = archive.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(corpus, "_ARCHIVE_SHA256", digest)
    monkeypatch.setattr(corpus, "_ARCHIVE_BYTES", len(raw))
    manifest = {
        **corpus._MANIFEST_FIXED,
        "archive_size_bytes": len(raw),
        "archive_sha256": "sha256:" + digest,
    }
    (root / "qlib_bin.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any]]:
    provider = tmp_path / "cn_data"
    (provider / "calendars").mkdir(parents=True)
    (provider / "instruments").mkdir()
    days = [
        "2025-12-24",
        "2025-12-25",
        "2025-12-26",
        "2025-12-29",
        "2025-12-30",
        "2025-12-31",
        "2026-09-15",
    ]
    calendar = ("\n".join(days) + "\n").encode()
    members = [
        {"symbol": symbol, "source_start": "2000-01-04", "source_end": "2026-09-15"}
        for symbol in SYMBOLS
    ]
    instruments = "".join(
        f"{symbol}\t2000-01-04\t2026-09-15\n" for symbol in SYMBOLS
    ).encode()
    (provider / "calendars/day.txt").write_bytes(calendar)
    (provider / "instruments/all.txt").write_bytes(instruments)
    for symbol in SYMBOLS:
        directory = provider / "features" / symbol.lower()
        directory.mkdir(parents=True)
        for field in corpus._FIELDS:
            values = [100.0 + i for i in range(len(days))]
            if field == "high":
                values = [value + 1 for value in values]
            elif field == "low":
                values = [value - 1 for value in values]
            elif field == "volume":
                values = [1000.0] * len(days)
            elif field == "factor":
                values = [0.1] * len(days)
            (directory / f"{field}.day.bin").write_bytes(
                struct.pack("<" + "f" * (len(values) + 1), 0, *values)
            )
    universe = _bind_universe(
        {
            "schema": "research-scale-universe-precommit-v1",
            "program_id": "fixture",
            "observation_date": "2026-10-10",
            "provider_snapshot": corpus._SNAPSHOT,
            "membership_source": "cn_data/instruments/all.txt",
            "membership_source_sha256": hashlib.sha256(instruments).hexdigest(),
            "calendar_source_sha256": hashlib.sha256(calendar).hexdigest(),
            "start": "2025-12-24",
            "end": "2025-12-31",
            "selection_count": 5,
            "membership_rule": "fixed metadata-only lexicographic five A shares",
            "selection_channel_count": 2,
            "supersedes_metadata_only_proposal": "indices rejected before values",
            "replacement_policy": "no replacement",
            "strategy_class": "single-asset search fixed universe",
            "pit_classification": "RETROSPECTIVE_NOT_INSTITUTIONAL_PIT",
            "survivorship_classification": "AVAILABILITY_SELECTED_SURVIVOR_SUBSET",
            "limitations": ["survivorship/PIT unavailable"],
            "members": members,
        }
    )
    _write_archive(tmp_path, monkeypatch)
    return provider, universe


def _set_cell(
    provider: Path, symbol: str, field: str, index: int, value: float
) -> None:
    path = provider / "features" / symbol.lower() / f"{field}.day.bin"
    raw = bytearray(path.read_bytes())
    struct.pack_into("<f", raw, 4 * (index + 1), value)
    path.write_bytes(raw)


def test_deterministic_offline_reload_native_hashes_and_honest_semantics(
    source: tuple[Path, dict[str, Any]],
) -> None:
    provider, universe = source
    frames, qualification = corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)
    reloaded, again = corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)
    assert qualification == again
    assert set(frames) == set(SYMBOLS)
    assert all(frame.equals(reloaded[symbol]) for symbol, frame in frames.items())
    assert qualification["alignment"]["common_sessions"] == 6
    assert (
        qualification["classifications"]["ASSUMPTION"]["price_unit"]
        == "QLIB_FIRST_CLOSE_NORMALIZED_ADJUSTED_PRICE"
    )
    assert (
        qualification["classifications"]["LICENSE / DISTRIBUTION NOTE"][
            "upstream_market_data_entitlements"
        ]
        == "NOT_INDEPENDENTLY_ESTABLISHED"
    )
    assert qualification["content_hashes"] == {
        symbol: corpus.logical_content_hash(frame) for symbol, frame in frames.items()
    }
    assert "returns" not in json.dumps(qualification)


@pytest.mark.parametrize(
    "attack",
    [
        "tamper",
        "duplicate",
        "symbol",
        "pit",
        "future",
        "missing_metadata",
        "channel_reset",
    ],
)
def test_universe_attacks_fail_closed(
    source: tuple[Path, dict[str, Any]], attack: str
) -> None:
    _, original = source
    universe = copy.deepcopy(original)
    if attack == "tamper":
        universe["start"] = "2025-12-25"
    elif attack == "duplicate":
        universe["members"][1] = universe["members"][0]
        _bind_universe(universe)
    elif attack == "symbol":
        universe["members"][0]["symbol"] = "../SH600000"
        _bind_universe(universe)
    elif attack == "pit":
        universe["pit_classification"] = "INSTITUTIONAL_PIT"
        _bind_universe(universe)
    elif attack == "future":
        universe["end"] = "2027-01-01"
        _bind_universe(universe)
    elif attack == "channel_reset":
        universe["selection_channel_count"] = 1
        _bind_universe(universe)
    else:
        del universe["limitations"]
    with pytest.raises(corpus.CorpusQualificationError):
        corpus.validate_universe(universe)


def test_feature_tamper_archive_tamper_and_stale_snapshot(
    source: tuple[Path, dict[str, Any]],
) -> None:
    provider, universe = source
    _set_cell(provider, SYMBOLS[0], "close", 0, 99)
    with pytest.raises(
        corpus.CorpusQualificationError, match="extracted source artifact"
    ):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)
    archive = provider.parent / "qlib_bin.tar.gz"
    raw = bytearray(archive.read_bytes())
    raw[-1] ^= 1
    archive.write_bytes(raw)
    with pytest.raises(corpus.CorpusQualificationError, match="archive hash"):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)
    manifest_path = provider.parent / "qlib_bin.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["release_tag"] = "2026-07-20"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(corpus.CorpusQualificationError, match="manifest mismatch"):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("factor", math.nan, "restoration metadata"),
        ("factor", 0.0, "nonpositive"),
        ("close", math.inf, "infinite"),
        ("volume", -1.0, "negative"),
        ("high", 1.0, "OHLC"),
    ],
)
def test_bad_source_observations_quarantine_even_when_authentic(
    source: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: float,
    reason: str,
) -> None:
    provider, universe = source
    _set_cell(provider, SYMBOLS[0], field, 0, value)
    _write_archive(provider.parent, monkeypatch)
    with pytest.raises(ValueError, match=reason):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)


def test_missing_policy_intersection_no_fill_no_replacement(
    source: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, universe = source
    for field in corpus._FIELDS:
        _set_cell(provider, SYMBOLS[0], field, 1, math.nan)
    _write_archive(provider.parent, monkeypatch)
    frames, qualification = corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)
    assert all(frame.height == 5 for frame in frames.values())
    assert qualification["alignment"]["removed_session_dates"] == ["2025-12-25"]
    assert qualification["diagnostics"][SYMBOLS[0]]["missing_source_sessions"] == 1
    assert qualification["diagnostics"][SYMBOLS[1]]["missing_source_sessions"] == 0


def test_invalid_ohlc_cannot_hide_behind_another_symbols_missing_bar(
    source: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, universe = source
    for field in corpus._FIELDS:
        _set_cell(provider, SYMBOLS[0], field, 1, math.nan)
    _set_cell(provider, SYMBOLS[1], "high", 1, 1)
    _write_archive(provider.parent, monkeypatch)
    with pytest.raises(ValueError, match="OHLC"):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)


@pytest.mark.parametrize(
    "upper,lower,missing",
    [
        ("high", "low", "open"),
        ("high", "low", "close"),
        ("high", "low", "volume"),
        ("high", "open", "close"),
        ("high", "open", "volume"),
        ("high", "close", "open"),
        ("high", "close", "volume"),
        ("open", "low", "close"),
        ("open", "low", "volume"),
        ("close", "low", "open"),
        ("close", "low", "volume"),
    ],
)
def test_observed_ohlc_pair_contradiction_cannot_hide_in_incomplete_row(
    source: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    upper: str,
    lower: str,
    missing: str,
) -> None:
    provider, universe = source
    _set_cell(provider, SYMBOLS[0], upper, 0, 1.0)
    _set_cell(provider, SYMBOLS[0], lower, 0, 100.0)
    _set_cell(provider, SYMBOLS[0], missing, 0, math.nan)
    _write_archive(provider.parent, monkeypatch)
    with pytest.raises(corpus.CorpusQualificationError, match="partial OHLC"):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)


@pytest.mark.parametrize("missing", ["open", "close", "volume"])
def test_noncontradictory_partial_row_still_retains_missing_exclusion(
    source: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    provider, universe = source
    _set_cell(provider, SYMBOLS[0], missing, 0, math.nan)
    _write_archive(provider.parent, monkeypatch)
    frames, qualification = corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)
    assert all(frame.height == 5 for frame in frames.values())
    assert qualification["alignment"]["removed_session_dates"] == ["2025-12-24"]
    assert qualification["diagnostics"][SYMBOLS[0]]["missing_source_sessions"] == 1


@pytest.mark.parametrize("header", [math.nan, -1.0, 0.5, 99.0])
def test_calendar_feature_header_attack(
    source: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, header: float
) -> None:
    provider, universe = source
    _set_cell(provider, SYMBOLS[0], "open", -1, header)
    _write_archive(provider.parent, monkeypatch)
    with pytest.raises(corpus.CorpusQualificationError, match="calendar header"):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)


def test_duplicate_calendar_and_member_source_binding(
    source: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, universe = source
    path = provider / "calendars/day.txt"
    raw = path.read_bytes().replace(b"2025-12-25", b"2025-12-24")
    path.write_bytes(raw)
    universe["calendar_source_sha256"] = hashlib.sha256(raw).hexdigest()
    _bind_universe(universe)
    _write_archive(provider.parent, monkeypatch)
    with pytest.raises(corpus.CorpusQualificationError, match="duplicate"):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)


def test_symbol_substitution_with_rehashed_universe(
    source: tuple[Path, dict[str, Any]],
) -> None:
    provider, universe = source
    universe["members"][-1]["symbol"] = "SH600009"
    _bind_universe(universe)
    with pytest.raises(corpus.CorpusQualificationError, match="missing"):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)


def test_missing_corporate_metadata_and_manifest_duplicate_keys(
    source: tuple[Path, dict[str, Any]],
) -> None:
    provider, universe = source
    (provider / "features" / SYMBOLS[0].lower() / "factor.day.bin").unlink()
    with pytest.raises(corpus.CorpusQualificationError, match="missing"):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)
    manifest = provider.parent / "qlib_bin.manifest.json"
    manifest.write_text('{"release_tag":"2026-09-15","release_tag":"2026-09-15"}')
    with pytest.raises(corpus.CorpusQualificationError, match="duplicate"):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF)


def test_naive_asof_and_future_declaration_rejected(
    source: tuple[Path, dict[str, Any]],
) -> None:
    provider, universe = source
    with pytest.raises(ValueError, match="timezone-aware"):
        corpus.load_qlib_corpus(provider, universe, as_of=AS_OF.replace(tzinfo=None))
    with pytest.raises(corpus.CorpusQualificationError, match="after as_of"):
        corpus.load_qlib_corpus(
            provider, universe, as_of=datetime(2025, 12, 31, tzinfo=UTC)
        )
