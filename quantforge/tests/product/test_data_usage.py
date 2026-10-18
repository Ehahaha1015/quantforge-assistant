from __future__ import annotations

import concurrent.futures
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from quantforge.product.data_usage import DataUsageRegistry, DataWindow


def window() -> DataWindow:
    return DataWindow("CN:510300", date(2020, 1, 1), date(2026, 9, 30), "a" * 64)


def test_exposure_blocks_relabelled_path_vintage_and_parameters(tmp_path: Path) -> None:
    registry = DataUsageRegistry(tmp_path)
    registry.record_use(
        window(), purpose="DEVELOPMENT", program="old", reference_hash="b" * 64
    )
    renamed_and_revised = replace(window(), content_hash="c" * 64)
    with pytest.raises(ValueError, match="prior exposure"):
        registry.reserve_final(
            renamed_and_revised,
            program="new-path-new-parameters",
            protocol_hash="d" * 64,
        )
    subset = replace(window(), start=date(2025, 1, 1))
    with pytest.raises(ValueError, match="prior exposure"):
        registry.reserve_final(subset, program="subset", protocol_hash="d" * 64)
    assert DataUsageRegistry(tmp_path).history() == registry.history()


def test_final_requires_freeze_and_only_one_opening_across_threads(
    tmp_path: Path,
) -> None:
    registry = DataUsageRegistry(tmp_path)
    final = registry.reserve_final(window(), program="frozen", protocol_hash="b" * 64)
    with pytest.raises(ValueError, match="frozen candidate"):
        registry.open_final(final, candidate_hash="c" * 64)
    with pytest.raises(ValueError, match="sealed final"):
        registry.record_use(
            window(), purpose="SELECTION", program="bad", reference_hash="b" * 64
        )
    registry.freeze_candidate(final, candidate_hash="c" * 64)
    with pytest.raises(ValueError, match="immutable"):
        registry.freeze_candidate(final, candidate_hash="d" * 64)

    def open_once(_: int) -> str:
        try:
            return registry.open_final(final, candidate_hash="c" * 64)["status"]
        except ValueError:
            return "DENIED"

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(open_once, range(8)))
    assert results.count("ONE_OPENING_CONSUMED") == 1
    assert results.count("DENIED") == 7


def test_replay_use_does_not_duplicate_and_final_purpose_cannot_bypass(
    tmp_path: Path,
) -> None:
    registry = DataUsageRegistry(tmp_path)
    options = {"purpose": "DEVELOPMENT", "program": "study", "reference_hash": "b" * 64}
    registry.record_use(window(), **options)  # type: ignore[arg-type]
    assert registry.record_use(window(), **options)["status"] == "ALREADY_RECORDED"  # type: ignore[arg-type]
    assert len(registry.history()) == 1
    with pytest.raises(ValueError):
        registry.record_use(
            window(),
            purpose="FINAL_CONFIRMATION",
            program="bad",
            reference_hash="b" * 64,
        )


def test_forward_declaration_does_not_turn_history_into_elapsed_observation(
    tmp_path: Path,
) -> None:
    registry = DataUsageRegistry(tmp_path)
    declared = registry.declare_forward(program="paper", protocol_hash="b" * 64)
    assert declared["starts_on"] > datetime.now(UTC).date().isoformat()
    with pytest.raises(ValueError, match="historical rows"):
        registry.record_forward(
            window(), program="paper", source_captured_at=datetime.now(UTC)
        )
    with pytest.raises(ValueError, match="immutable"):
        registry.declare_forward(program="paper", protocol_hash="c" * 64)


def test_registry_tampering_is_detected(tmp_path: Path) -> None:
    registry = DataUsageRegistry(tmp_path)
    registry.record_use(
        window(), purpose="DEVELOPMENT", program="study", reference_hash="b" * 64
    )
    with registry.connect() as db:
        db.execute("DROP TRIGGER no_update")
        value = json.loads(db.execute("SELECT payload FROM events").fetchone()[0])
        value["purpose"] = "FINAL_CONFIRMATION"
        db.execute("UPDATE events SET payload=?", (json.dumps(value),))
    with pytest.raises(ValueError, match="integrity"):
        registry.history()
