from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from quantforge.product.strategy_optimization import OptimizationProtocol

from qf_platform.optimization_cli import run_csv


def test_frozen_csv_reproduction_and_source_conflict(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for index in range(160):
            price = 100 + index * 0.1
            day = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=index)
            writer.writerow(
                [
                    day.date().isoformat(),
                    f"{price:.3f}",
                    f"{price + 1:.3f}",
                    f"{price - 1:.3f}",
                    f"{price:.3f}",
                    10000,
                ]
            )
    options = {
        "source": "Synthetic CSV for unit testing, not historical market evidence",
        "adjustment": "Synthetic prices with no adjustments",
        "prior_exposure": "Synthetic test fixture exposed in this unit test",
        "protocol": OptimizationProtocol(120, 80, 20, 30),
        "usage_root": tmp_path / "usage",
    }
    output = tmp_path / "study"
    first = run_csv(source, "TEST", output, **options)
    before = (output / "result.json").read_bytes()
    second = run_csv(source, "TEST", output, **options)
    assert first == second
    assert (output / "result.json").read_bytes() == before
    assert (output / "report.md").is_file()
    assert (output / "metrics.csv").is_file()
    with pytest.raises(ValueError, match="frozen plan/source changed"):
        run_csv(source, "TEST", output, **{**options, "source": "different source"})
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    result["validated_alpha"] = True
    (output / "result.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity mismatch"):
        run_csv(source, "TEST", output, **options)
