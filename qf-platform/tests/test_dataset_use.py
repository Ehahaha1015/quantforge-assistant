from __future__ import annotations

from pathlib import Path

from quantforge.product.data_usage import DataUsageRegistry
from quantforge.product.trend_refinement import evaluate_refinements

from qf_platform.dataset_use import register_research_use, window_of
from qf_platform.task_tools import bars


def test_output_path_is_not_part_of_source_exposure_identity(tmp_path: Path) -> None:
    frame = bars()
    first = register_research_use(
        frame,
        program="research-one",
        reference_hash="a" * 64,
        registry_root=tmp_path,
    )
    second = register_research_use(
        frame.clone(),
        program="research-one",
        reference_hash="a" * 64,
        registry_root=tmp_path,
    )
    assert second["status"] == "ALREADY_RECORDED"
    assert first["identity"] == second["identity"]
    assert len(DataUsageRegistry(tmp_path).history()) == 1
    result = evaluate_refinements(frame, history_bars=120)
    assert window_of(frame).content_hash == result["dataset_hash"]
