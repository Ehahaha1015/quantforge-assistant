from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

BENCHMARK_ID = "qlib-alpha158-lightgbm-csi300-v1"
REGION = "CN"
MARKET = "csi300"
BENCHMARK_INDEX = "SH000300"
HANDLER = "Alpha158"
MODEL = "LGBModel"
TRAIN_SEGMENT = ("2008-01-01", "2014-12-31")
VALID_SEGMENT = ("2015-01-01", "2016-12-31")
TEST_SEGMENT = ("2017-01-01", "2020-08-01")
WORKFLOW_STEPS = ("train", "predict", "signal_analysis", "portfolio_analysis")


@dataclass(frozen=True)
class GoldenBenchmarkSpec:
    """Immutable pinned identity of the external Qlib golden benchmark.

    ``config_hash`` covers the semantic benchmark definition only: the
    benchmark id, region/market/benchmark, handler, model, the three date
    segments and the workflow steps.  It deliberately excludes the provider
    path, artifact directory, clock, hostname and process state, because those
    are runtime inputs rather than benchmark semantics.
    """

    benchmark_id: str
    region: str
    market: str
    benchmark_index: str
    handler: str
    model: str
    train_segment: tuple[str, str]
    valid_segment: tuple[str, str]
    test_segment: tuple[str, str]
    workflow: tuple[str, ...]
    provider_uri: str
    config_hash: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "config_hash", compute_config_hash(self))


def semantic_payload(spec: GoldenBenchmarkSpec) -> dict[str, object]:
    return {
        "benchmark_id": spec.benchmark_id,
        "region": spec.region,
        "market": spec.market,
        "benchmark_index": spec.benchmark_index,
        "handler": spec.handler,
        "model": spec.model,
        "train_segment": list(spec.train_segment),
        "valid_segment": list(spec.valid_segment),
        "test_segment": list(spec.test_segment),
        "workflow": list(spec.workflow),
    }


def canonical_spec_json(spec: GoldenBenchmarkSpec) -> str:
    return json.dumps(
        semantic_payload(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def compute_config_hash(spec: GoldenBenchmarkSpec) -> str:
    return sha256(canonical_spec_json(spec).encode("utf-8")).hexdigest()


def build_golden_benchmark_spec(provider_uri: str | Path) -> GoldenBenchmarkSpec:
    """Build the fixed benchmark specification for one provider location."""
    provider = str(provider_uri)
    if not provider:
        raise ValueError("provider_uri must be a non-empty path")
    return GoldenBenchmarkSpec(
        benchmark_id=BENCHMARK_ID,
        region=REGION,
        market=MARKET,
        benchmark_index=BENCHMARK_INDEX,
        handler=HANDLER,
        model=MODEL,
        train_segment=TRAIN_SEGMENT,
        valid_segment=VALID_SEGMENT,
        test_segment=TEST_SEGMENT,
        workflow=WORKFLOW_STEPS,
        provider_uri=provider,
    )
