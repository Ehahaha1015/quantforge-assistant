"""Slice 8 bounded stress: PYTHONHASHSEED and fresh-process determinism.

One full ledger history (prepare, claim, dispatch, two compatible
observations, one conflicting observation) is rebuilt in five fresh OS
processes under different hash seeds, and once more under a repeated seed.
Every observable identity, phase, ordering, and stored evidence byte must be
identical.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_WORKER = Path(__file__).with_name("_stress_determinism_worker.py")

_SEEDS = ("0", "1", "42", "12345", "987654321")


def _run(directory: Path, seed: str) -> subprocess.CompletedProcess[str]:
    repository = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PYTHONHASHSEED": seed}
    return subprocess.run(
        [sys.executable, str(_WORKER), str(directory)],
        cwd=str(repository),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_stress_pythonhashseed_determinism_repetition(
    tmp_path: Path,
) -> None:
    outputs: list[str] = []
    for seed in _SEEDS:
        directory = tmp_path / f"seed-{seed}"
        directory.mkdir()
        completed = _run(directory, seed)
        assert completed.returncode == 0, completed.stderr
        outputs.append(completed.stdout)
    repeat_directory = tmp_path / "seed-0-repeat"
    repeat_directory.mkdir()
    repeated = _run(repeat_directory, "0")
    assert repeated.returncode == 0, repeated.stderr
    outputs.append(repeated.stdout)
    assert len(set(outputs)) == 1

    payload = json.loads(outputs[0])
    assert payload["observation_ids"] == [
        "execstate-observation-v1-1",
        "execstate-observation-v1-2",
    ]
    assert payload["attempt_id"].startswith("execstate-attempt-v1-")
    assert payload["conflict"] is not None
    assert payload["conflict"]["result"] == "CONFLICT"
    assert payload["conflict"]["reason"] == (
        "CONFLICT_CONTRADICTORY_OBSERVATION"
    )
    assert payload["conflict"]["required_phase"] == "UNKNOWN"
    assert payload["conflict"]["existing_state"] == "DISPATCHING"
    assert [view["phase"] for view in payload["history"]] == ["OBSERVED"]
    assert payload["current"]["phase"] == "OBSERVED"
    assert payload["current"]["execstate_state"] == "DISPATCHING"
    assert payload["current"]["observation_ids"] == payload[
        "observation_ids"
    ]
    assert [row["observation_sequence"] for row in payload["raw_rows"]] == [
        1,
        2,
    ]
    assert [row["observation_kind"] for row in payload["raw_rows"]] == [
        "BROKER_OBSERVATION",
        "BROKER_OBSERVATION",
    ]
    assert len({row["attempt_id"] for row in payload["raw_rows"]}) == 1
