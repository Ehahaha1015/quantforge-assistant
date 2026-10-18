from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quantforge.product.scale.registry import ProgramRegistry
from quantforge.product.scale.storage import ScaleError, publish


@pytest.fixture
def registry(tmp_path: Path) -> ProgramRegistry:
    value = ProgramRegistry(tmp_path)
    value.create(
        {
            "program_id": "test",
            "budgets": {
                "trials": 6,
                "families": {"f": 4, "g": 2},
                "model_calls": 4,
                "wall_seconds": 3600,
                "data_rows_scanned": 10000,
                "benchmark_openings": 1,
                "retries": 0,
            },
        }
    )
    yield value
    value.close()


def test_rejected_duplicate_manual_failed_crashed_and_hidden_reset_count(
    registry: ProgramRegistry,
) -> None:
    first = registry.allocate(
        "a", "f", {"short_window": 2, "long_window": 5}, origin="AI"
    )
    assert first["admitted"]
    registry.terminal(first["allocation"], "FAILED", reason="Synthetic failure")
    assert not registry.allocate(
        "a", "f", {"short_window": 3, "long_window": 5}, origin="MANUAL"
    )["admitted"]
    assert not registry.allocate(
        "b", "reset-family", {"short_window": 3, "long_window": 6}, origin="AI"
    )["admitted"]
    assert not registry.allocate("c", "f", {"sharpe": 99}, origin="AI")["admitted"]
    pending = registry.allocate(
        "d", "g", {"short_window": 4, "long_window": 7}, origin="MANUAL"
    )
    assert pending["admitted"]
    assert registry.status()["trials"] == 5
    registry.recover()
    assert not registry.status()["pending_trials"]
    assert any(e.get("status") == "CRASHED" for e in registry.history())
    assert not registry.allocate(
        "e", "f", {"short_window": 2, "long_window": 5}, origin="AI"
    )["admitted"]
    with pytest.raises(ScaleError, match="TRIAL_BUDGET"):
        registry.allocate("f", "f", {"short_window": 4, "long_window": 8}, origin="AI")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        registry.journal.connection.execute("DELETE FROM events")


def test_invalid_id_counted_rejected_and_reserved_chain_metadata_cannot_mutate(registry: ProgramRegistry) -> None:
    invalid = registry.allocate("../../invalid", "f", {"short_window": 2, "long_window": 5}, origin="MANUAL")
    assert not invalid["admitted"] and invalid["reason"] == "INVALID_IDENTIFIER"
    assert not registry.status()["pending_trials"]
    good = registry.allocate("good", "f", {"short_window": 3, "long_window": 7}, origin="MANUAL")
    before = registry.history()
    for values in ({"event_hash": "0" * 64}, {"seq": 991}, {"previous": "injected"}, {"kind": "OTHER"}):
        with pytest.raises((ScaleError, TypeError)):
            registry.terminal(good["allocation"], "COMPLETED", **values)
        assert registry.history() == before
    registry.terminal(good["allocation"], "COMPLETED")
    assert registry.pause(True)["trials"] == 2


def test_model_reservation_survives_restart_no_retry_and_pause(
    registry: ProgramRegistry,
) -> None:
    call = registry.reserve_model()
    with pytest.raises(ScaleError, match="AMBIGUOUS"):
        registry.reserve_model()
    registry.recover()
    assert registry.status()["model_calls"] == 1
    assert any(
        e.get("call") == call and e.get("status") == "UNKNOWN"
        for e in registry.history()
    )
    registry.pause(True)
    with pytest.raises(ScaleError, match="PAUSED"):
        registry.allocate("z", "f", {"short_window": 4, "long_window": 8}, origin="AI")
    registry.pause(False)
    registry.reserve_scan(10000)
    with pytest.raises(ScaleError, match="SCAN_BUDGET"):
        registry.reserve_scan(1)


def test_atomic_publish_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "proof.json"
    publish(path, {"value": 1})
    with pytest.raises(ScaleError, match="ALREADY_EXISTS"):
        publish(path, {"value": 2})
    assert path.read_bytes() == b'{"value":1}'
