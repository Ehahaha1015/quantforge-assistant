"""Trusted development computation and append-only evidence publication.

The caller supplies an admitted recipe, never a metric or an evidence payload.
Final lockbox access is a separate, sealed operation; this path cannot score it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import polars as pl

from quantforge.backtest import BacktestConfig, run_vectorized_backtest
from quantforge.data import DatasetSnapshot, ParquetDataCatalog, logical_content_hash
from quantforge.experiments import (
    ArtifactRef,
    ExperimentRecord,
    ExperimentStore,
    canonical_json,
)
from quantforge.research import ExperimentSpec
from quantforge.strategies import (
    MovingAverageCrossoverConfig,
    generate_moving_average_signals,
)
from quantforge.strategies.models import (
    config_from_strategy,
    family_metadata,
    generate_strategy_signals,
)
from quantforge.validation import WalkForwardConfig, run_walk_forward_validation

from .context import code_context, source_identity
from .evidence import evidence_id, read_evidence, render_markdown
from .storage import local_root, run_directory, write_exclusive, writer_lock


class ExperimentRunError(ValueError):
    """A proposed run cannot establish trusted computation/evidence bindings."""


def _instant(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExperimentRunError("aware timestamp required")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _timestamps(frame: pl.DataFrame) -> list[datetime]:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return [
        epoch + timedelta(microseconds=int(v))
        for v in frame["timestamp"].cast(pl.Int64)
    ]


def _identifier(value: str) -> str:
    # Reuse the frozen experiment identifier boundary; no caller path is opened.
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ExperimentRunError("invalid program or family identifier")
    if (
        not value[0].isalnum()
        or not value.isascii()
        or any(not (c.isalnum() or c in "._-") for c in value)
    ):
        raise ExperimentRunError("invalid program or family identifier")
    return value


class ExperimentRunner:
    """One host-owned workspace; all supported product writes are computed here."""

    def __init__(self, root: str | Path) -> None:
        self.root = local_root(root)
        for name in ("catalog", "development", "oos"):
            child = self.root / name
            child.mkdir(exist_ok=True)
            local_root(child)
        self.catalog = ParquetDataCatalog(self.root / "catalog")
        self.artifact_root = self.root / "development"
        database = self.root / "experiments.sqlite"
        if database.exists() and database.is_symlink():
            raise ExperimentRunError("redirected experiment store")
        self.store = ExperimentStore(database)

    def close(self) -> None:
        self.store.close()

    def dataset_qualification(self, snapshot: DatasetSnapshot) -> dict[str, Any]:
        if snapshot.source == "operator-pilot-snapshot-v1":
            from .datasets import qualification as host_qualification

            return host_qualification(self.root, snapshot)
        if snapshot.source != "alphavantage-public-demo-TIME_SERIES_DAILY":
            return {
                "status": "HOST_DECLARED_DATA",
                "limitations": [
                    "Data source is host-declared; no external authenticity demonstrated for this dataset."
                ],
            }
        from quantforge.historical.alphavantage import parse_public_demo

        directory = self.root / "qualifications" / snapshot.dataset_id
        local_root(directory)
        for name in ("provider-response.json", "qualification.json"):
            if (directory / name).is_symlink():
                raise ExperimentRunError("redirected provider qualification")
        raw = (directory / "provider-response.json").read_bytes()
        qualification_raw = (directory / "qualification.json").read_bytes()
        qualification = json.loads(qualification_raw)
        if qualification_raw != canonical_json(qualification).encode():
            raise ExperimentRunError("noncanonical provider qualification")
        if (
            DatasetSnapshot.model_validate(qualification["dataset_snapshot"])
            != snapshot
        ):
            raise ExperimentRunError("qualification snapshot binding mismatch")
        parsed, observed = parse_public_demo(
            raw, retrieved_at=datetime.fromisoformat(qualification["retrieved_at"])
        )
        expected = {k: v for k, v in qualification.items() if k != "dataset_snapshot"}
        if (
            observed != expected
            or logical_content_hash(parsed) != snapshot.content_hash
        ):
            raise ExperimentRunError("provider response/qualification mismatch")
        return {
            "status": "QUALIFIED_PROVIDER_RESPONSE",
            "qualification_sha256": hashlib.sha256(qualification_raw).hexdigest(),
            "provider_response_sha256": hashlib.sha256(raw).hexdigest(),
            "provider": observed["provider"],
            "documentation": observed["documentation"],
            "request": observed["request"],
            "checks": observed["checks"],
            "adjustment_declaration": observed["adjustment"],
            "timestamp_declaration": observed["timestamp_semantics"],
            "limitations": observed["limitations"],
        }

    def _bound_data(self, spec: ExperimentSpec) -> pl.DataFrame:
        expected = DatasetSnapshot.model_validate(spec.recipe["dataset_snapshot"])
        directory = self.catalog.root / expected.dataset_id
        local_root(directory)
        for name in ("manifest.json", "bars.parquet"):
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise ExperimentRunError("invalid retained dataset path")
        observed = self.catalog.verify(expected.dataset_id)
        if observed != expected:
            raise ExperimentRunError("dataset snapshot binding mismatch")
        frame = self.catalog.read(expected.dataset_id)
        if logical_content_hash(frame) != expected.content_hash:
            raise ExperimentRunError("loaded dataset hash mismatch")
        if frame["symbol"].unique().to_list() != [spec.request.symbol]:
            raise ExperimentRunError("single requested symbol required")
        instants = _timestamps(frame)
        if len({v.date() for v in instants}) != len(instants):
            raise ExperimentRunError("one observation per UTC date required")
        if any(a >= b for a, b in pairwise(instants)):
            raise ExperimentRunError("strictly ordered observations required")
        split = spec.recipe["split"]
        if len(instants) != split["observations"]:
            raise ExperimentRunError("observation count mismatch")
        d, v = split["development_bars"], split["validation_bars"]
        indices = (0, d - 1, d, d + v - 1, d + v, len(instants) - 1)
        names = (
            "development_start",
            "development_end",
            "validation_start",
            "validation_end",
            "test_start",
            "test_end",
        )
        for name, index in zip(names, indices, strict=True):
            if _instant(instants[index]) != spec.recipe["split_timestamps"][name]:
                raise ExperimentRunError("observed partition endpoint mismatch")
        if self.catalog.verify(expected.dataset_id) != expected:
            raise ExperimentRunError("dataset changed during binding")
        self.dataset_qualification(expected)
        return frame

    @staticmethod
    def _compute(spec: ExperimentSpec, frame: pl.DataFrame) -> dict[str, Any]:
        recipe = spec.recipe.to_payload()
        split = recipe["split"]
        development_end = split["development_bars"]
        validation_end = development_end + split["validation_bars"]
        # Information boundary: final-test rows are removed before any strategy call.
        prefix = frame.head(validation_end)
        strategy = config_from_strategy(
            recipe["strategy"], legacy=spec.schema_version == "experiment-spec-v1"
        )
        backtest = BacktestConfig(**recipe["backtest"])
        development = prefix.head(development_end)
        train_result = run_vectorized_backtest(
            development,
            (
                generate_moving_average_signals(development, strategy)
                if isinstance(strategy, MovingAverageCrossoverConfig)
                else generate_strategy_signals(development, strategy)
            ),
            backtest,
        )
        validation = run_walk_forward_validation(
            prefix,
            strategy,
            backtest,
            WalkForwardConfig(
                initial_history_bars=development_end,
                oos_window_bars=split["validation_window_bars"],
            ),
        )
        periods = validation.aggregate.periods
        observations = [
            {**row, "timestamp": _instant(moment)}
            for row, moment in zip(
                periods.drop("timestamp").to_dicts(),
                _timestamps(periods),
                strict=True,
            )
        ]
        loss = -validation.aggregate.metrics.max_drawdown
        return {
            "development_metrics": asdict(train_result.metrics),
            "validation_metrics": asdict(validation.aggregate.metrics),
            "validation_windows": [
                asdict(w.result.metrics) for w in validation.windows
            ],
            "validation_periods": observations,
            "screen_pass": loss <= recipe["screen"]["max_drawdown_loss_magnitude"],
            "final_lockbox_scored": False,
        }

    def run_experiment(
        self,
        spec: ExperimentSpec,
        *,
        research_program_id: str,
        trial_family_id: str,
    ) -> dict[str, Any]:
        """Execute DEVELOPMENT only; facts and persistence are owned by this method."""
        try:
            spec = ExperimentSpec.from_json(spec.to_json())
            program = _identifier(research_program_id)
            family = _identifier(trial_family_id)
            actual_context = code_context()
            if actual_context != spec.recipe["code_context"]:
                raise ExperimentRunError("executing code context differs from recipe")
            source = source_identity()
            with writer_lock(self.root):
                freeze_path = self.root / "programs" / program / "freeze.json"
                if freeze_path.exists():
                    raise ExperimentRunError(
                        "frozen program cannot accept new development trials"
                    )
                directory = run_directory(self.artifact_root, spec.experiment_id)
                if directory.exists():
                    raise ExperimentRunError(
                        "run already reserved; inspect/reproduce, never redispatch"
                    )
                if any(
                    record.experiment_id == spec.experiment_id
                    for record in self.store.list()
                ):
                    raise ExperimentRunError("run already terminal; no redispatch")
                reservations = []
                for entry in sorted(self.artifact_root.iterdir()):
                    if not entry.name.casefold().startswith("run-"):
                        continue
                    path = entry / "reservation.json"
                    local_root(path.parent)
                    if path.is_symlink() or not path.is_file():
                        raise ExperimentRunError(
                            "incomplete reservation history blocks research"
                        )
                    raw = path.read_bytes()
                    config = json.loads(raw)
                    if raw != canonical_json(config).encode("utf-8"):
                        raise ExperimentRunError("corrupt retained reservation history")
                    if config.get("schema_version") != "development-run-v1":
                        raise ExperimentRunError("unknown retained reservation")
                    old_recipe = config["spec"]["recipe"]
                    old_spec = ExperimentSpec.from_json(canonical_json(config["spec"]))
                    if (
                        entry.name
                        != run_directory(
                            self.artifact_root, old_spec.experiment_id
                        ).name
                    ):
                        raise ExperimentRunError("reservation name/identity mismatch")
                    same_instrument = (
                        old_recipe["symbol"],
                        old_recipe["frequency"],
                    ) == (spec.recipe["symbol"], spec.recipe["frequency"])
                    old_times = old_recipe["split_timestamps"]
                    new_times = spec.recipe["split_timestamps"]
                    overlapping = same_instrument and max(
                        old_times["development_start"], new_times["development_start"]
                    ) <= min(old_times["test_end"], new_times["test_end"])
                    if overlapping or config["trial_family_id"] == family:
                        if (
                            config["trial_family_id"] != family
                            or config["research_program_id"] != program
                        ):
                            raise ExperimentRunError(
                                "same dataset is already bound to another research family/program"
                            )
                        if old_recipe["split"] != spec.recipe["split"]:
                            raise ExperimentRunError(
                                "trial family partitions cannot change"
                            )
                        if (
                            old_recipe["dataset_snapshot"]
                            != spec.recipe["dataset_snapshot"]
                        ):
                            raise ExperimentRunError(
                                "trial family dataset/vintage cannot change"
                            )
                        reservations.append(config)
                if len(reservations) >= 8:
                    raise ExperimentRunError("bounded research family exhausted")
                frame = self._bound_data(spec)
                metadata = {
                    "research_program_id": program,
                    "trial_family_id": family,
                    "candidate_count": len(reservations) + 1,
                    "selection_rule": "no_selection_in_development_runner",
                    "prior_exposures": [
                        config["spec"]["request"]["experiment_id"]
                        for config in reservations
                    ],
                    "partitions_seen": ["development", "validation"],
                }
                config = {
                    "schema_version": "development-run-v1",
                    "spec": json.loads(spec.to_json()),
                    "evaluation_stage": "DEVELOPMENT",
                    "source_sha256": source,
                    **{
                        k: metadata[k]
                        for k in ("research_program_id", "trial_family_id")
                    },
                }
                directory.mkdir()  # failed publication conservatively leaves a reservation
                write_exclusive(directory / "reservation.json", config)
                computed = self._compute(spec, frame)
                if code_context() != actual_context or source_identity() != source:
                    raise ExperimentRunError(
                        "executing code changed during calculation"
                    )
                if self.catalog.verify(
                    spec.recipe["dataset_snapshot"]["dataset_id"]
                ) != DatasetSnapshot.model_validate(spec.recipe["dataset_snapshot"]):
                    raise ExperimentRunError("dataset changed during calculation")
                evidence = self._pack(spec, computed, metadata, source)
                write_exclusive(directory / "EvidencePack.json", evidence)
                markdown = directory / "EvidencePack.md"
                with markdown.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(render_markdown(evidence))
                record = ExperimentRecord.create(
                    experiment_id=spec.experiment_id,
                    git_commit=actual_context["git_commit"],
                    dataset_id=spec.recipe["dataset_snapshot"]["dataset_id"],
                    dataset_hash=spec.recipe["dataset_snapshot"]["content_hash"],
                    backtest_config=config,
                    random_seed=recipe_seed(spec),
                    started_at=spec.request.submitted_at,
                    metrics={
                        "development_run": {
                            "evidence_id": evidence_id(evidence),
                            "screen_pass": computed["screen_pass"],
                        }
                    },
                    artifact_refs=(
                        ArtifactRef(
                            name="EvidencePack",
                            uri=(directory / "EvidencePack.json").as_uri(),
                        ),
                    ),
                )
                if read_evidence(directory / "EvidencePack.json") != evidence:
                    raise ExperimentRunError("evidence publication mismatch")
                self.store.save(record)
                return evidence
        except ExperimentRunError:
            raise
        except (ValueError, TypeError, OSError, KeyError) as exc:
            raise ExperimentRunError("trusted development run failed") from exc

    def _pack(
        self,
        spec: ExperimentSpec,
        computed: dict[str, Any],
        metadata: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        recipe = spec.recipe.to_payload()
        qualification = self.dataset_qualification(
            DatasetSnapshot.model_validate(recipe["dataset_snapshot"])
        )
        return {
            "schema_version": "evidence-pack-v1",
            "FACT": {
                "run_id": spec.experiment_id,
                "evaluation_stage": "DEVELOPMENT",
                "specification": json.loads(spec.to_json()),
                "source_sha256": source,
                "research": metadata,
                **(
                    {"families": family_metadata(recipe["strategy"])}
                    if spec.schema_version == "experiment-spec-v2"
                    else {}
                ),
                "data_qualification": {
                    k: v
                    for k, v in qualification.items()
                    if k
                    not in {
                        "limitations",
                        "adjustment_declaration",
                        "timestamp_declaration",
                    }
                },
                **computed,
            },
            "DECLARED_ASSUMPTION": [
                "Hypothesis text and provider declarations are research inputs, not computed performance.",
                "Explicit position-switch fee model; zero risk-free rate; 252 daily periods/year.",
                canonical_json(recipe["data_conventions"]),
                "Provider adjustment/time declarations: "
                + canonical_json(
                    {
                        k: qualification[k]
                        for k in ("adjustment_declaration", "timestamp_declaration")
                        if k in qualification
                    }
                ),
            ],
            "LIMITATION": [
                (
                    "Fixed long/flat SMA only; no alpha or future profitability claim."
                    if spec.schema_version == "experiment-spec-v1"
                    else "Fixed long/flat admitted strategy family; no alpha or future profitability claim."
                ),
                "Slippage, dividends, financing and market impact are not modelled.",
                "Validation evidence is exposed for iterative research; it is not the final lockbox.",
                "Host-owned local artifacts; checksums detect inconsistency, not hostile coherent rewrites.",
                "Windows file fsync is used; directory/power-loss durability is not guaranteed.",
            ]
            + qualification["limitations"],
        }

    def inspect(self, run_id: str) -> dict[str, Any]:
        record = self.store.get(run_id)
        config = record.backtest_config.to_payload()
        if config.get("schema_version") != "development-run-v1":
            raise ExperimentRunError("not a trusted development record")
        directory = run_directory(self.artifact_root, record.experiment_id)
        local_root(directory)
        path = directory / "EvidencePack.json"
        evidence = read_evidence(path)
        if (
            len(record.artifact_refs) != 1
            or record.artifact_refs[0].uri != path.as_uri()
        ):
            raise ExperimentRunError("evidence artifact binding mismatch")
        if record.metrics["development_run"]["evidence_id"] != evidence_id(evidence):
            raise ExperimentRunError("evidence integrity mismatch")
        spec = ExperimentSpec.from_json(canonical_json(config["spec"]))
        facts = evidence["FACT"]
        if (
            facts["specification"] != json.loads(spec.to_json())
            or facts["source_sha256"] != config["source_sha256"]
        ):
            raise ExperimentRunError("record/evidence identity mismatch")
        if (
            record.git_commit != spec.recipe["code_context"]["git_commit"]
            or record.dataset_hash != spec.recipe["dataset_snapshot"]["content_hash"]
            or record.dataset_id != spec.recipe["dataset_snapshot"]["dataset_id"]
            or facts["evaluation_stage"] != "DEVELOPMENT"
            or facts["run_id"] != run_id
        ):
            raise ExperimentRunError("record execution binding mismatch")
        return evidence

    def reproduce(self, run_id: str) -> dict[str, Any]:
        """Recompute the same development recipe; never publish a new experiment."""
        original = self.inspect(run_id)
        facts = original["FACT"]
        spec = ExperimentSpec.from_json(canonical_json(facts["specification"]))
        if (
            code_context() != spec.recipe["code_context"]
            or source_identity() != facts["source_sha256"]
        ):
            raise ExperimentRunError("reproduction code/runtime binding mismatch")
        frame = self._bound_data(spec)
        reproduced = self._pack(
            spec, self._compute(spec, frame), facts["research"], facts["source_sha256"]
        )
        original_id, reproduced_id = evidence_id(original), evidence_id(reproduced)
        if (
            code_context() != spec.recipe["code_context"]
            or source_identity() != facts["source_sha256"]
        ):
            raise ExperimentRunError("code changed during reproduction")
        if self.catalog.verify(
            spec.recipe["dataset_snapshot"]["dataset_id"]
        ) != DatasetSnapshot.model_validate(spec.recipe["dataset_snapshot"]):
            raise ExperimentRunError("dataset changed during reproduction")
        return {
            "run_id": run_id,
            "original_evidence_id": original_id,
            "reproduced_evidence_id": reproduced_id,
            "result": "EXACT_MATCH" if original_id == reproduced_id else "MISMATCH",
        }


def recipe_seed(spec: ExperimentSpec) -> int:
    return int(spec.recipe["random_seed"])
