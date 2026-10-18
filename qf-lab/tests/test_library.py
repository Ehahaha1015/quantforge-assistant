from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from quantforge.data import BAR_SCHEMA, ParquetDataCatalog

from qf_lab.gateway import GatewayError, ResearchDecision, canonical
from qf_lab.library import library, retrieve
from qf_lab.report import report
from qf_lab.room import ResearchRoom
from qf_lab.room_gateway import FakeRoomProvider


def dataset(core: Path, symbol: str) -> str:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    prices = [100.0 + i * 0.1 + i % 17 for i in range(100)]
    return (
        ParquetDataCatalog(core / "catalog")
        .write(
            symbol,
            pl.DataFrame(
                {
                    "symbol": [symbol] * 100,
                    "timestamp": [start + timedelta(days=i) for i in range(100)],
                    "open": prices,
                    "close": [v + 1 for v in prices],
                    "high": [v + 2 for v in prices],
                    "low": [v - 2 for v in prices],
                    "volume": [1000.0] * 100,
                },
                schema=BAR_SCHEMA,
            ),
            source="synthetic-library-qualification",
            schema_version="bars-v1",
            as_of=start + timedelta(days=100),
        )
        .dataset_id
    )


def routes(researcher: Any = None) -> dict[str, Any]:
    result = {
        r: [FakeRoomProvider(r)]
        for r in ("researcher", "critic", "director", "validator")
    }
    if researcher is not None:
        result["researcher"] = [researcher]
    return result


def test_cross_room_lesson_changes_proposal_and_survives_new_process(
    tmp_path: Path,
) -> None:
    core, rooms = tmp_path / "core", tmp_path / "rooms"
    core.mkdir()
    rooms.mkdir()
    first_root = rooms / "z-first"
    first_root.mkdir()
    first = ResearchRoom(first_root, core, routes())
    first.declare(
        program_id="first",
        goal="A bounded qualification",
        dataset_id=dataset(core, "A"),
        rounds=1,
        calls=6,
    )
    assert first.run()["status"] == "COMPLETE"
    # Old retained Room versions omitted the failure classification field.
    first.event(
        "ROLE_PROVIDER_FAILED",
        profile_id="old-profile",
        role="researcher",
        round=1,
        retry="FORBIDDEN",
    )
    original = library(rooms)
    assert any(
        v["reason"] == "LEGACY_PROVIDER_FAILURE_UNCLASSIFIED"
        for v in original["failures"]
    )
    assert any(
        v["classification"] == "RESEARCH_REJECTED" and v["evidence_refs"]
        for v in original["failures"]
    )
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; from pathlib import Path; from qf_lab.library import library; print(json.dumps(library(Path(sys.argv[1])),sort_keys=True))",
            str(rooms),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(process.stdout) == original

    class LearningResearcher(FakeRoomProvider):
        def complete_structured(
            self, context: dict[str, Any], schema: Any, **kwargs: Any
        ) -> Any:
            decision, metadata = super().complete_structured(context, schema, **kwargs)
            if context["phase"] == "first":
                memory = context["research_memory"]
                assert memory["lessons"][0]["program_id"] == "first"
                assert memory["lessons"][0]["evidence_refs"]
                tested = {
                    canonical(v["parameters"]) for v in memory["prior_hypotheses"]
                }
                for short in (3, 4, 6):
                    parameters = {"short_window": short, "long_window": 15}
                    if canonical(parameters) not in tested:
                        decision = ResearchDecision(
                            action="propose",
                            family="moving_average_crossover",
                            **parameters,
                            rationale="Historical rejection teaches that the previous tested pair is insufficient; use a distinct bounded causal question.",
                            evidence_refs=(),
                        )
                        break
            return decision, metadata

    second_root = rooms / "a-second"
    second_root.mkdir()
    second = ResearchRoom(second_root, core, routes(LearningResearcher("researcher")))
    second.declare(
        program_id="second",
        goal="Use the prior negative lesson",
        dataset_id=dataset(core, "B"),
        rounds=1,
        calls=6,
    )
    frozen = second.snapshot()["declaration"]["research_memory"]
    assert frozen["source_count"] == 1
    assert second.run()["status"] == "COMPLETE"
    assert second.snapshot()["declaration"]["research_memory"] == frozen
    assert "Statistical applicability" in Path(report(second)["report"]).read_text()
    assert library(rooms)["memory_id"] == library(rooms)["memory_id"]
    latest = retrieve(
        rooms,
        exclude=rooms / "third",
        families=("moving_average_crossover",),
    )
    assert latest["lessons"][0]["program_id"] == "second"
    assert latest["prior_hypotheses"][0]["program_id"] == "second"


def test_corrupt_source_and_reused_exposure_are_refused(tmp_path: Path) -> None:
    core, rooms = tmp_path / "core", tmp_path / "rooms"
    core.mkdir()
    rooms.mkdir()
    root = rooms / "first"
    root.mkdir()
    identity = dataset(core, "A")
    app = ResearchRoom(root, core, routes())
    app.declare(
        program_id="first",
        goal="A causal qualification",
        dataset_id=identity,
        rounds=1,
        calls=6,
    )
    assert app.run()["status"] == "COMPLETE"
    other = rooms / "other"
    other.mkdir()
    with pytest.raises(GatewayError, match="DATASET_ALREADY_BOUND_OR_UNRESOLVED"):
        ResearchRoom(other, core, routes()).declare(
            program_id="other",
            goal="Do not reset prior exposure",
            dataset_id=identity,
            rounds=1,
            calls=6,
        )
    (root / "room-calls.sqlite").write_bytes(b"corrupt")
    with pytest.raises(GatewayError, match="LIBRARY_SOURCE_INVALID"):
        library(rooms)
