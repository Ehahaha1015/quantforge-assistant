from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from quantforge.data import BAR_SCHEMA, ParquetDataCatalog
from quantforge.experiments import (
    ExperimentRecord,
    ExperimentStore,
    canonical_config_hash,
)
from quantforge.research import ExperimentSpec, ResearchRequest, admit_research_request
from quantforge.research.oos import seal_oos_test_use, verify_oos_test_use

from .._research_fixtures import complete_inputs, request_fields

ADMITTED_AT = datetime(2026, 10, 8, tzinfo=UTC)
DECLARATION = "none_known_outside_factory"


def revised_spec(
    spec: ExperimentSpec,
    *,
    recipe: dict[str, Any] | None = None,
    request: dict[str, Any] | None = None,
) -> ExperimentSpec:
    body = spec.to_config().to_payload()
    if recipe:
        body["recipe"].update(recipe)
    if request:
        body["request"].update(request)
    return ExperimentSpec.model_validate(
        {**body, "config_hash": canonical_config_hash(body)}
    )


def bars(
    *,
    symbol: str = "SPY",
    start: datetime | None = None,
    observations: int = 1260,
    price: float = 100.0,
) -> pl.DataFrame:
    start = start or datetime(2020, 1, 1, 14, 30, tzinfo=UTC)
    return pl.DataFrame(
        {
            "symbol": [symbol] * observations,
            "timestamp": [start + timedelta(days=i) for i in range(observations)],
            "open": [price] * observations,
            "high": [price + 1] * observations,
            "low": [price - 1] * observations,
            "close": [price] * observations,
            "volume": [1000.0] * observations,
        },
        schema=BAR_SCHEMA,
    )


def retained_spec(
    catalog: ParquetDataCatalog,
    *,
    experiment_id: str = "experiment.001",
    dataset_id: str = "retained-SPY",
    symbol: str = "SPY",
    start: datetime | None = None,
    price: float = 100.0,
    frame: pl.DataFrame | None = None,
) -> ExperimentSpec:
    frame = bars(symbol=symbol, start=start, price=price) if frame is None else frame
    snapshot = catalog.write(
        dataset_id,
        frame,
        source="local-synthetic",
        schema_version="bars-v1",
        as_of=ADMITTED_AT,
    )
    inputs = complete_inputs()
    fields = request_fields()
    fields.update(experiment_id=experiment_id, symbol=symbol)
    inputs["request"] = ResearchRequest(**fields)
    inputs["dataset_snapshot"] = snapshot
    instants = catalog.read(dataset_id)["timestamp"].to_list()
    inputs["split_timestamps"] = dict(
        zip(
            (
                "development_start",
                "development_end",
                "validation_start",
                "validation_end",
                "test_start",
                "test_end",
            ),
            [instants[i].isoformat() for i in (0, 755, 756, 1007, 1008, 1259)],
            strict=True,
        )
    )
    return admit_research_request(**inputs)


class Fixture:
    """Test host only: public native dependencies, explicit root and context."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.catalog = ParquetDataCatalog(path / "catalog")
        self.root = path / "artifacts"
        self.root.mkdir()
        self.store_path = path / "experiments.sqlite"
        self.store = ExperimentStore(self.store_path)
        self.spec = retained_spec(self.catalog)

    def kwargs(self, spec: ExperimentSpec | None = None) -> dict[str, Any]:
        spec = self.spec if spec is None else spec
        return {
            "catalog": self.catalog,
            "experiment_store": self.store,
            "artifact_root": self.root,
            "code_context": spec.recipe["code_context"],
        }

    def seal(
        self,
        *,
        spec: ExperimentSpec | None = None,
        mode: str = "first_use",
        **changes: Any,
    ) -> Any:
        spec = self.spec if spec is None else spec
        args = {
            **self.kwargs(spec),
            "admitted_at": ADMITTED_AT,
            "prior_exposure": DECLARATION,
            "mode": mode,
            **changes,
        }
        return seal_oos_test_use(spec, **args)

    def verify(self, *, spec: ExperimentSpec | None = None, **changes: Any) -> Any:
        spec = self.spec if spec is None else spec
        return verify_oos_test_use(spec, **{**self.kwargs(spec), **changes})

    def seal_path(self, spec: ExperimentSpec | None = None) -> Path:
        spec = self.spec if spec is None else spec
        encoded = (
            base64.b32encode(spec.experiment_id.encode()).decode().lower().rstrip("=")
        )
        return self.root / ("run-" + encoded) / "oos-seal.json"

    def save_terminal(
        self,
        *,
        outcome: str = "INCOMPLETE",
        spec: ExperimentSpec | None = None,
        **changes: Any,
    ) -> ExperimentRecord:
        spec = self.spec if spec is None else spec
        path = self.seal_path(spec)
        recipe = spec.recipe.to_payload()
        args = {
            "experiment_id": spec.experiment_id,
            "git_commit": recipe["code_context"]["git_commit"],
            "dataset_id": recipe["dataset_snapshot"]["dataset_id"],
            "dataset_hash": recipe["dataset_snapshot"]["content_hash"],
            "backtest_config": spec.to_config(),
            "random_seed": recipe["random_seed"],
            "started_at": ADMITTED_AT,
            "metrics": {
                "research_factory": {
                    "outcome": outcome,
                    "artifact_integrity": {
                        "research_oos_seal": hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest()
                    },
                }
            },
            "artifact_refs": [{"name": "research_oos_seal", "uri": path.as_uri()}],
            **changes,
        }
        record = ExperimentRecord.create(**args)
        self.store.save(record)
        return record

    def reopen(self) -> None:
        self.store.close()
        self.store = ExperimentStore(self.store_path)
        self.catalog = ParquetDataCatalog(self.path / "catalog")

    def file_bytes(self) -> dict[str, bytes]:
        return {
            p.relative_to(self.path).as_posix(): p.read_bytes()
            for p in self.path.rglob("*")
            if p.is_file()
        }

    def close(self) -> None:
        self.store.close()
