from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quantforge.product.pilot.data import import_bars_csv
from quantforge.product.service import ResearchService


def source(root: Path) -> str:
    root.mkdir()
    path = root / "bars.csv"
    start = datetime(2020, 1, 1, tzinfo=UTC)
    path.write_text(
        "date,open,high,low,close,volume\n"
        + "\n".join(
            f"{(start + timedelta(days=i)).date()},1.00,1.10,0.90,1.01,1000"
            for i in range(150)
        ),
        encoding="utf-8",
    )
    return str(
        import_bars_csv(
            root,
            path,
            "ETF",
            as_of=datetime(2021, 1, 1, tzinfo=UTC),
            declared_adjustment="fixture raw operator prices",
        )["snapshot_id"]
    )


@pytest.mark.parametrize("provenance", ["llm", "scripted"])
def test_operator_origin_survives_admission_reproduction_and_tampering(
    tmp_path: Path,
    provenance: str,
) -> None:
    pilot, core_root = tmp_path / "pilot", tmp_path / "core"
    core_root.mkdir()
    identity = source(pilot)
    core = ResearchService(core_root)
    try:
        result = core.import_pilot_dataset(
            pilot,
            identity,
            start="2020-01-01",
            end="2020-05-30",
            exposure_declaration="Previously inspected fixture; no unseen-alpha claim.",
        )
        dataset = result["dataset"]["dataset_id"]
        retained = core_root / "catalog" / dataset / "bars.parquet"
        original_bytes, modified = retained.read_bytes(), retained.stat().st_mtime_ns
        assert (
            core.import_pilot_dataset(
                pilot,
                identity,
                start="2020-01-01",
                end="2020-05-30",
                exposure_declaration="Previously inspected fixture; no unseen-alpha claim.",
            )
            == result
        )
        assert retained.read_bytes() == original_bytes
        assert retained.stat().st_mtime_ns == modified
        assert "NOT_EXTERNAL_AUTHENTICATION" in result["qualification"]["status"]
        core.propose_and_run(
            dataset_id=dataset,
            program_id="study",
            family_id="study-family",
            run_id="study-r1",
            goal="A retrospective mechanical qualification",
            short_window=5,
            long_window=15,
            submitted_at=datetime.now(UTC),
            model_call_id="fixture-proposal",
            rationale="Declared causal baseline under nonzero costs",
            provenance_source=provenance,
        )
        assert (
            core.inspect("study-r1")["FACT"]["specification"]["request"]["provenance"][
                "source"
            ]
            == provenance
        )
        assert core.reproduce("study-r1")["result"] == "EXACT_MATCH"
        assert (
            "Previously inspected"
            in result["qualification"]["prior_exposure_declaration"]
        )
        path = core_root / "qualifications" / dataset / "host-qualification.json"
        path.write_text(
            path.read_text().replace("Previously inspected", "Never inspected")
        )
        with pytest.raises(ValueError, match="qualification"):
            core.reproduce("study-r1")
    finally:
        core.close()
