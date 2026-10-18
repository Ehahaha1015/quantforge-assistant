"""PRICE-001 determinism, restart and concurrency oracles (C, AE, AF, AI)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _marketdata_fixtures import (
    RecordingSource,
    make_artifact,
    make_observation,
    make_target,
    produce,
)

from quantforge.marketdata import (
    PriceSourceRejectedError,
    RejectingPriceObservationSource,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_DIR = _REPO_ROOT / "src" / "quantforge" / "marketdata"


def _package_files() -> set[str]:
    return {str(path) for path in _PACKAGE_DIR.rglob("*") if path.is_file()}


def test_hash_seed_fresh_process_stability() -> None:
    probe = Path(__file__).with_name("hash_seed_probe.py")
    outputs: list[str] = []
    for seed in ("random", "0", "12345"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, str(probe)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=environment,
        )
        outputs.append(completed.stdout.strip().splitlines()[-1])
    assert len(set(outputs)) == 1
    assert outputs[0] == produce(make_observation()).model_dump_json()


def test_repeated_calls_and_no_durable_state() -> None:
    before_files = _package_files()
    first = produce(make_observation())
    with tempfile.TemporaryDirectory() as directory:
        before_temp = set(os.listdir(directory))
        second = produce(make_observation())
        after_temp = set(os.listdir(directory))
    assert before_temp == after_temp
    assert first.model_dump_json() == second.model_dump_json()
    assert _package_files() == before_files


def test_concurrent_independent_calls_share_nothing() -> None:
    def worker(_: int) -> str:
        return produce(make_observation(), provenance=make_artifact()).model_dump_json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))

    assert len(results) == 8
    assert len(set(results)) == 1


def test_rejection_mutates_nothing() -> None:
    provenance = make_artifact()
    target = make_target()
    observation = make_observation(provenance=provenance)
    provenance_json = provenance.model_dump_json()
    target_json = target.model_dump_json()
    observation_json = observation.model_dump_json()
    source = RecordingSource(observation)
    evidence = produce(observation, provenance=provenance, target=target, source=source)
    assert evidence.schema == "frontier-price-v1"
    assert provenance.model_dump_json() == provenance_json
    assert target.model_dump_json() == target_json
    assert observation.model_dump_json() == observation_json
    assert source.calls == 1

    try:
        produce(
            None,
            provenance=provenance,
            target=target,
            source=RejectingPriceObservationSource(),
        )
    except PriceSourceRejectedError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("rejecting source must fail")
    assert provenance.model_dump_json() == provenance_json
    assert target.model_dump_json() == target_json
