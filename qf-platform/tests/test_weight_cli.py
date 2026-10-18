from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from quantforge.data import BAR_SCHEMA, logical_content_hash

from qf_platform.storage import digest
from qf_platform.weight_cli import run_weights


def sources(root: Path) -> tuple[Path, Path]:
    prices = [10 + index * 0.02 for index in range(80)]
    frame = pl.DataFrame(
        {
            "symbol": ["510300"] * 80,
            "timestamp": [
                datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=index)
                for index in range(80)
            ],
            "open": prices,
            "close": prices,
            "high": [value + 1 for value in prices],
            "low": [value - 1 for value in prices],
            "volume": [1_000_000.0] * 80,
        },
        schema=BAR_SCHEMA,
    )
    frame.write_parquet(root / "510300-bars.parquet")
    inventory = {"assets": {"510300": {"content_hash": logical_content_hash(frame)}}}
    path, dividend = root / "inventory.json", root / "dividend-plan.json"
    path.write_text(json.dumps(inventory))
    (root / "inventory-digest.json").write_text(
        json.dumps({"sha256": digest(inventory)})
    )
    dividend.write_text(json.dumps({"events": [], "scope": "TEST_ONLY"}))
    return path, dividend


def test_weight_cli_freezes_all_configs_checks_identity_and_repeats(
    tmp_path: Path,
) -> None:
    inventory, dividend = sources(tmp_path)
    options = {"history_bars": 20, "usage_root": tmp_path / "usage"}
    first = run_weights(inventory, dividend, tmp_path / "out", **options)
    root = Path(first["artifact_root"])
    result = json.loads((root / "result.json").read_text())
    plan = json.loads((root / "plan.json").read_text())
    assert result["plan_id"] == digest(plan)
    assert (
        digest(result)
        == json.loads((root / "result-digest.json").read_text())["sha256"]
    )
    assert set(plan["configurations"]) == set(result["scenarios"])
    assert result["scenarios"]["CASH"]["fill_count"] == 0
    assert Decimal(result["scenarios"]["CASH"]["final_equity"]) == 100000
    assert not result["validated_alpha"] and not result["live_eligible"]
    second = run_weights(inventory, dividend, tmp_path / "out", **options)
    assert first["scenarios"] == second["scenarios"]
    assert (root / "report.md").is_file()
    inventory.write_text(json.dumps({"assets": {}}))
    with pytest.raises(ValueError, match="checksum"):
        run_weights(inventory, dividend, tmp_path / "out", **options)


def test_invalid_warmup_is_rejected_before_output_or_data_use(tmp_path: Path) -> None:
    inventory, dividend = sources(tmp_path)
    with pytest.raises(ValueError, match="warmup"):
        run_weights(
            inventory,
            dividend,
            tmp_path / "out",
            history_bars=80,
            usage_root=tmp_path / "usage",
        )
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "usage").exists()
