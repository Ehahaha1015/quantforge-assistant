from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from quantforge.data import BAR_SCHEMA, ParquetDataCatalog, logical_content_hash
from quantforge.product.scale.service import (
    ReplayRuntime,
    ScaleService,
    inspect_benchmark,
    open_benchmark,
    prepare_program,
)
from quantforge.product.scale.storage import ScaleError
from quantforge.product.scale.vault import HostBenchmarkCapability


def frames(*, tail: float = 1.0) -> dict[str, pl.DataFrame]:
    result = {}
    for symbol in ("A", "B"):
        prices = [(100 + i * 0.3 + math.sin(i / 3)) * (tail if i >= 75 else 1) for i in range(105)]
        result[symbol] = pl.DataFrame({
            "symbol": [symbol] * 105,
            "timestamp": [datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i) for i in range(105)],
            "open": prices, "high": [v + 1 for v in prices], "low": [v - 1 for v in prices],
            "close": prices, "volume": [1000.] * 105,
        }, schema=BAR_SCHEMA)
    return result


def make(parent: Path, *, name: str = "program", tail: float = 1.) -> tuple[ScaleService, Path]:
    root = parent / name
    root.mkdir()
    key = parent / (name + ".key")
    prepare_program(root, frames(tail=tail), {"host_fixture": True}, {
        "program_id": name, "goal": "Bounded synthetic engineering test, not market evidence",
        "universe_id": "fixed-A-B", "research_end": "2020-03-15T00:00:00+00:00",
        "protocol": {"initial_history_bars": 50, "window_bars": 10, "partition_id": "frozen-date"},
        "budgets": {"trials": 10, "families": {"f": 10}, "model_calls": 3,
                    "wall_seconds": 3600, "data_rows_scanned": 100000,
                    "benchmark_openings": 1, "retries": 0},
    }, key_path=key)
    return ScaleService(root), key


def exhaust(service: ScaleService) -> str:
    value = service.submit("first", "f", {"short_window": 5, "long_window": 15}, origin="MANUAL")
    assert value["status"] == "COMPLETED"
    second = service.submit("duplicate", "f", {"short_window": 5, "long_window": 15}, origin="MANUAL")
    assert second["status"] == "REJECTED"
    assert service.status()["trials"] == 10
    return value["run_id"]


def test_full_native_trial_evidence_reproduce_freeze_single_open_replay_restart(tmp_path: Path) -> None:
    service, key = make(tmp_path)
    run_id = exhaust(service)
    assert service.reproduce(run_id)["exact"]
    context = service.context("f")
    assert set(context["evidence"][0]["metrics"]) == {"total_return", "sharpe", "max_drawdown"}
    assert "benchmark" not in str(context["evidence"])
    service.freeze()
    with pytest.raises(ScaleError, match="SEARCH_CLOSED"):
        service.context("f")
    with pytest.raises(ScaleError, match="FACTS_NOT_AUTHORIZED"):
        inspect_benchmark(service)
    report = open_benchmark(service, HostBenchmarkCapability("program", key), custody_root=tmp_path / "custody")
    assert report["benchmark_openings"] == 1
    assert not report["live_eligible"]
    assert inspect_benchmark(service) == report
    with pytest.raises(ScaleError, match="OPENING_DENIED"):
        open_benchmark(service, HostBenchmarkCapability("program", key), custody_root=tmp_path / "custody")
    replay = ReplayRuntime(service)
    replay.set_running(True)
    for _ in range(16):
        replay.step()
    replay.set_running(False)
    state = replay.status()
    replay.close()
    replay = ReplayRuntime(service)
    assert replay.status() == state
    with pytest.raises(ScaleError, match="REPLAY_STOPPED"):
        replay.step()
    replay.set_running(True)
    replay.step()
    assert replay.status()["cursor"] == 17
    replay.close()
    service.close()


@pytest.mark.parametrize("finished", [False, True])
def test_ambiguous_and_finished_response_before_submit_recovery_counts_no_retry(tmp_path: Path, finished: bool) -> None:
    service, _ = make(tmp_path)
    call = service.reserve_model(family="f")
    if finished:
        service.finish_model(call, status="RESPONSE", ledger_id="response")
    service.close()
    service = ScaleService(tmp_path / "program")
    status = service.recover()
    assert status["model_calls"] == 1 and status["trials"] == 5
    assert not status["unresolved_model_calls"] and not status["pending_trials"]
    assert all(e["status"] == "REJECTED" for e in status["trial_history"])
    assert service.recover()["trials"] == 5
    service.close()


def test_cross_lab_root_worker_race_recovery_denied(tmp_path: Path) -> None:
    service, _ = make(tmp_path)
    other = ScaleService(tmp_path / "program")
    with service.campaign_lock():
        with pytest.raises(OSError):
            other.recover()
        with pytest.raises(ScaleError, match="WORKER_LOCK"):
            other.recover(locked=True)
    other.close()
    service.close()


def test_physical_prefix_only_and_changed_benchmark_cannot_change_development_metrics(tmp_path: Path) -> None:
    one, _ = make(tmp_path, name="one", tail=1.)
    two, _ = make(tmp_path, name="two", tail=3.)
    for service in (one, two):
        catalog = ParquetDataCatalog(service._root / "catalog")
        assert catalog.read("prefix-A").height == 75
        with pytest.raises(ScaleError, match="FACTS_NOT_AUTHORIZED"):
            inspect_benchmark(service)
    proposal = {"short_window": 5, "long_window": 15}
    a = one.submit("trial", "f", proposal)["projection"]
    b = two.submit("trial", "f", proposal)["projection"]
    assert a["metrics"] == b["metrics"]
    assert a["strategy"] == b["strategy"] and a["limitations"] == b["limitations"]
    # Opaque content identities bind different precommits; no benchmark fact is projected.
    one.close()
    two.close()


def test_data_substitution_and_terminal_alias_reopening_rejected(tmp_path: Path) -> None:
    first, key1 = make(tmp_path, name="one")
    second, key2 = make(tmp_path, name="two")
    for service in (first, second):
        exhaust(service)
        service.freeze()
    custody = tmp_path / "custody"
    open_benchmark(first, HostBenchmarkCapability("one", key1), custody_root=custody)
    with pytest.raises(ScaleError, match="ALREADY_EXPOSED"):
        open_benchmark(second, HostBenchmarkCapability("two", key2), custody_root=custody)
    catalog = ParquetDataCatalog(first._root / "catalog")
    modified = frames()["A"].head(75).with_columns((pl.col("volume") + 1).alias("volume"))
    catalog.write("prefix-A", modified, source="substitution", schema_version="bars-v1",
                  as_of=datetime(2020, 3, 15, tzinfo=UTC), overwrite=True)
    with pytest.raises(ScaleError, match="SUBSTITUTION"):
        first.reproduce("trial-1")
    first.close()
    second.close()


def test_wrong_key_spends_opening_and_never_falls_back(tmp_path: Path) -> None:
    service, key = make(tmp_path)
    exhaust(service)
    service.freeze()
    key.write_bytes(b"x" * 32)
    with pytest.raises(ScaleError, match="SPENT_BENCHMARK_FAILED"):
        open_benchmark(service, HostBenchmarkCapability("program", key), custody_root=tmp_path / "custody")
    assert service.status()["benchmark_state"] == "SPENT"
    assert inspect_benchmark(service)["status"] == "FAILED"
    with pytest.raises(ScaleError, match="SEARCH_CLOSED"):
        service.reserve_model(family="f")
    service.close()


def test_active_direct_submit_excludes_recovery_and_full_bundle_survives_pause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import quantforge.product.scale.service as module
    service, _ = make(tmp_path)
    other = ScaleService(service._root)
    original = module.evaluate
    def pause_during_compute(*args: object, **kwargs: object) -> object:
        assert other.status()["trials"] == 5
        with pytest.raises(OSError):
            other.recover()
        other.pause(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "evaluate", pause_during_compute)
    result = service.submit("first", "f", {"short_window": 5, "long_window": 15})
    assert result["status"] == "COMPLETED"
    assert other.recover()["trials"] == 5
    assert not other.status()["pending_trials"]
    other.close()
    service.close()


def test_bundle_sqlite_midwrite_fault_rolls_back_all_slots(tmp_path: Path) -> None:
    import sqlite3
    service, _ = make(tmp_path)
    connection = service._registry.journal.connection
    connection.execute("CREATE TRIGGER inject_crash BEFORE INSERT ON events WHEN json_extract(NEW.payload,'$.trial_id') = 'probe-1-1' BEGIN SELECT RAISE(ABORT,'injected'); END")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        service.submit("first", "f", {"short_window": 5, "long_window": 15})
    assert service.status()["trials"] == 0
    connection.execute("DROP TRIGGER inject_crash")
    connection.commit()
    assert service.submit("first", "f", {"short_window": 5, "long_window": 15})["status"] == "COMPLETED"
    assert service.status()["trials"] == 5
    service.close()


def test_spent_without_terminal_inspect_and_recovery_never_decrypt(tmp_path: Path) -> None:
    service, _ = make(tmp_path)
    exhaust(service)
    service.freeze()
    service._registry.journal.append("BENCHMARK_RESERVED", exposure_key="synthetic-kill-injection")
    service.close()
    restarted = ScaleService(tmp_path / "program")
    assert inspect_benchmark(restarted)["status"] == "UNKNOWN"
    restarted.recover()
    assert inspect_benchmark(restarted)["status"] == "UNKNOWN"
    assert not inspect_benchmark(restarted)["reopening_authorized"]
    with pytest.raises(ScaleError, match="COMPLETED_BENCHMARK"):
        ReplayRuntime(restarted)
    restarted.close()


def test_equivalent_timezone_or_gap_cutoff_cannot_alias_same_benchmark(tmp_path: Path) -> None:
    one, key1 = make(tmp_path, name="one")
    for name, cutoff in (("two", "2020-03-15T08:00:00+08:00"), ("three", "2020-03-15T12:00:00+00:00")):
        declaration = {k: one._registry.manifest[k] for k in ("program_id", "goal", "universe_id", "research_end", "protocol", "budgets")}
        declaration.update(program_id=name, research_end=cutoff)
        root = tmp_path / name
        root.mkdir()
        prepare_program(root, frames(), {"host_fixture": True}, declaration, key_path=tmp_path / (name + ".key"))
    exhaust(one)
    one.freeze()
    custody = tmp_path / "custody"
    open_benchmark(one, HostBenchmarkCapability("one", key1), custody_root=custody)
    for name in ("two", "three"):
        other = ScaleService(tmp_path / name)
        exhaust(other)
        other.freeze()
        assert other._registry.manifest["sealed"]["binding"]["benchmark_key"] == one._registry.manifest["sealed"]["binding"]["benchmark_key"]
        with pytest.raises(ScaleError, match="ALREADY_EXPOSED"):
            open_benchmark(other, HostBenchmarkCapability(name, tmp_path / (name + ".key")), custody_root=custody)
        other.close()
    one.close()


@pytest.mark.parametrize("invalid", ["budget", "universe", "content", "qualification"])
def test_preparation_rejects_incomplete_budgets_and_corpus_bindings_before_publication(tmp_path: Path, invalid: str) -> None:
    template, _ = make(tmp_path, name="template")
    declaration = {k: template._registry.manifest[k] for k in ("program_id", "goal", "universe_id", "research_end", "protocol", "budgets")}
    template.close()
    root = tmp_path / "bad"
    root.mkdir()
    qualification = {"schema": "research-scale-corpus-qualification-v1", "universe_id": "fixed-A-B",
                     "content_hashes": {s: logical_content_hash(f) for s, f in frames().items()}}
    if invalid == "budget":
        declaration["budgets"] = {**declaration["budgets"], "families": {"f": 6}, "trials": 6}
    elif invalid == "universe":
        qualification["universe_id"] = "swapped"
    elif invalid == "content":
        qualification["content_hashes"] = {"A": "wrong", "B": "wrong"}
    else:
        qualification = {"unknown_qualification": True}
    with pytest.raises(ScaleError):
        prepare_program(root, frames(), qualification, declaration, key_path=tmp_path / "bad.key")
    assert not (tmp_path / "bad.key").exists() and not list(root.iterdir())
