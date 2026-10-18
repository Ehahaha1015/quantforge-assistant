from __future__ import annotations

import concurrent.futures
import copy
import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from quantforge.product.data_usage import DataUsageRegistry
from quantforge.product.evidence import evidence_id
from quantforge.product.pilot.data import import_bars_csv, read_snapshot
from quantforge.product.service import ResearchService

from qf_platform.agent_tools import DATASET, AgentCoreBusy, AgentDataset, AgentTools
from qf_platform.dataset_use import window_of
from qf_platform.storage import PlatformError, digest


def tools(root: Path) -> AgentTools:
    path = root / "test-only.csv"
    start = datetime(2020, 1, 1, tzinfo=UTC)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for index in range(240):
            price = 10 + index * 0.02
            writer.writerow(
                [
                    (start + timedelta(days=index)).date().isoformat(),
                    f"{price:.3f}",
                    f"{price + 1:.3f}",
                    f"{price - 1:.3f}",
                    f"{price:.3f}",
                    "1000000",
                ]
            )
    pilot = root / "pilot"
    pilot.mkdir()
    snapshot = import_bars_csv(
        pilot,
        path,
        "510300",
        as_of=start + timedelta(days=250),
        declared_adjustment="TEST_ONLY synthetic fixture, not market evidence",
    )
    inventory = {
        "assets": {
            "510300": {
                "content_hash": snapshot["content_hash"],
                "price_research_allowed": True,
                "source": {"kind": "TEST_ONLY"},
                "status": "TEST_ONLY",
            }
        }
    }
    manifest = root / "inventory.json"
    manifest.write_text(json.dumps(inventory))
    (root / "inventory-digest.json").write_text(
        json.dumps({"sha256": digest(inventory)})
    )
    return AgentTools(
        root / "core",
        source=AgentDataset(pilot, snapshot["snapshot_id"], manifest),
        usage_root=root / "usage",
    )


def intent() -> dict[str, object]:
    return {
        "tool": "run_experiment",
        "family": "moving_average_crossover",
        "short_window": 5,
        "long_window": 20,
        "fee_bps": 10,
    }


def test_public_core_pack_and_same_step_recovery_do_not_repeat_trials(
    tmp_path: Path,
) -> None:
    host = tools(tmp_path)
    dataset = host.inspect_dataset(DATASET)
    assert (
        dataset["dataset"] == DATASET
        and dataset["dataset_snapshot"]["row_count"] == 240
    )
    assert dataset["core_trials_remaining"] == 8
    options = {
        "task_id": "task-" + "a" * 32,
        "index": 0,
        "call_id": "test-only-model-intent",
        "goal": "Compare two synthetic test strategies.",
        "rationale": "TEST ONLY causal mechanics, not real model execution.",
    }
    result = host.run_experiment(intent(), **options)
    assert result["core_evidence_id"] == evidence_id(result["evidence_pack"])
    assert result["evidence_pack"]["FACT"]["evaluation_stage"] == "DEVELOPMENT"
    assert not result["evidence_pack"]["FACT"]["final_lockbox_scored"]
    assert host.run_experiment(intent(), **options) == result
    assert host.inspect_dataset(DATASET)["core_trials_remaining"] == 7
    assert host.compare_results([result])["conclusion"] == "DESCRIPTIVE_ONLY"
    with pytest.raises(PlatformError, match="INPUT_CONFLICT"):
        host.run_experiment({**intent(), "short_window": 6}, **options)
    forged = copy.deepcopy(result)
    forged["evidence_pack"]["FACT"]["validation_metrics"]["total_return"] = 9.0
    forged["core_evidence_id"] = evidence_id(forged["evidence_pack"])
    with pytest.raises(PlatformError, match="MISMATCH"):
        host.compare_results([forged])


def test_final_reserved_source_and_unknown_dataset_are_denied(tmp_path: Path) -> None:
    host = tools(tmp_path)
    frame, _ = read_snapshot(host.source.pilot_root, host.source.snapshot_id)
    usage = tmp_path / "usage"
    usage.mkdir()
    DataUsageRegistry(usage).reserve_final(
        window_of(frame), program="final", protocol_hash="f" * 64
    )
    with pytest.raises(ValueError):
        host.inspect_dataset(DATASET)
    with pytest.raises(PlatformError, match="NOT_ALLOWED"):
        host.inspect_dataset("arbitrary-final-or-private-path")


def test_current_manifest_quality_is_checked_before_creating_core_facts(
    tmp_path: Path,
) -> None:
    host = tools(tmp_path)
    inventory = json.loads(host.source.inventory_path.read_text())
    inventory["assets"]["510300"]["price_research_allowed"] = False
    host.source.inventory_path.write_text(json.dumps(inventory))
    host.source.inventory_path.with_name("inventory-digest.json").write_text(
        json.dumps({"sha256": digest(inventory)})
    )
    with pytest.raises(PlatformError, match="QUALITY_DENIED"):
        host.inspect_dataset(DATASET)
    core = ResearchService(host.core_root)
    try:
        assert core.list_runs() == []
    finally:
        core.close()


def test_concurrent_metadata_reads_serialize_and_foreign_gate_is_before_dispatch(
    tmp_path: Path,
) -> None:
    host = tools(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        values = list(pool.map(lambda _: host.inspect_dataset(DATASET), range(6)))
    assert all(value["core_trials_remaining"] == 8 for value in values)
    other = AgentTools(host.core_root, source=host.source, usage_root=host.usage_root)
    with host._gate():
        with pytest.raises(AgentCoreBusy, match="NOT_DISPATCHED"):
            other.inspect_dataset(DATASET)
