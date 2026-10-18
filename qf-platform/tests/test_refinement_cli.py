from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

import pytest

from qf_platform.refinement_cli import run_suite


def write_csv(path: Path, *, missing: int | None = None) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for index in range(180):
            if index == missing:
                continue
            price = 100 + index * 0.1
            writer.writerow(
                [
                    (date(2020, 1, 1) + timedelta(days=index)).isoformat(),
                    f"{price:.3f}",
                    f"{price + 1:.3f}",
                    f"{price - 1:.3f}",
                    f"{price:.3f}",
                    10000,
                ]
            )


def test_suite_retains_missing_dates_and_reproduces(tmp_path: Path) -> None:
    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    write_csv(first)
    write_csv(second, missing=20)
    options = {
        "source": "Explicit synthetic test data",
        "adjustment": "Synthetic prices",
        "prior_exposure": "Test fixture only, never historical evidence",
        "history_bars": 120,
        "usage_root": tmp_path / "usage",
    }
    output = tmp_path / "suite"
    result = run_suite({"FIRST": first, "SECOND": second}, output, **options)
    assert result["session_diagnostics"] == {
        "FIRST": [],
        "SECOND": [(date(2020, 1, 1) + timedelta(days=20)).isoformat()],
    }
    assert result["assets"]["FIRST"]["observations"] == 180
    assert result["assets"]["SECOND"]["observations"] == 179
    assert (
        result["assets"]["FIRST"]["evaluation_start"]
        == result["assets"]["SECOND"]["evaluation_start"]
    )
    assert run_suite({"FIRST": first, "SECOND": second}, output, **options) == result
    assert (output / "report.md").is_file() and (output / "metrics.csv").is_file()
    with pytest.raises(ValueError, match="frozen suite plan changed"):
        run_suite(
            {"FIRST": first, "SECOND": second},
            output,
            **{**options, "source": "changed"},
        )
    with pytest.raises(ValueError):
        run_suite({"../redirect": first}, tmp_path / "invalid", **options)
