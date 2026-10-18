"""Restricted, host-owned admission facade used by the replaceable Lab."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from quantforge.experiments import FrozenPayload
from quantforge.research import ResearchRequest, admit_research_request
from quantforge.strategies.models import (
    StrategyFamily,
    config_from_strategy,
    required_observations,
    strategy_from_parameters,
)

from .context import code_context
from .evidence import evidence_id
from .runner import ExperimentRunError, ExperimentRunner, _timestamps


class ResearchExecutionError(ValueError):
    """Closed product failure codes; no raw internal exception crosses into Lab."""


class ResearchService:
    MIN_OBSERVATIONS = 80

    def __init__(self, root: str | Path) -> None:
        self._runner = ExperimentRunner(root)

    def close(self) -> None:
        self._runner.close()

    def describe_dataset(self, dataset_id: str) -> dict[str, Any]:
        snapshot = self._runner.catalog.verify(dataset_id)
        # Dates/identity are metadata; final test prices never enter this response.
        return snapshot.model_dump(mode="json")

    def import_pilot_dataset(
        self,
        pilot_root: Path,
        snapshot_id: str,
        *,
        start: str,
        end: str,
        exposure_declaration: str,
    ) -> dict[str, Any]:
        from .datasets import import_pilot_dataset

        return import_pilot_dataset(
            self._runner.root,
            pilot_root,
            snapshot_id,
            start=start,
            end=end,
            exposure_declaration=exposure_declaration,
        )

    def research_availability(self, dataset_id: str) -> dict[str, Any]:
        """Preflight existing exposures without revealing final data or metrics.

        The runner remains the authority at commit time. This product query
        prevents spending model budget on a new family the runner must reject;
        it does not authorize a new experiment or reset retained reservations.
        """
        import json

        from .storage import local_root, writer_lock

        snapshot = self._runner.catalog.verify(dataset_id)
        if snapshot.min_timestamp is None or snapshot.max_timestamp is None:
            return {"status": "UNRESOLVED_DATASET", "bindings": []}
        symbol = str(self._runner.catalog.read(dataset_id)["symbol"][0])
        with writer_lock(self._runner.root):
            bindings = []
            for directory in sorted(self._runner.artifact_root.iterdir()):
                if not directory.name.casefold().startswith("run-"):
                    continue
                local_root(directory)
                path = directory / "reservation.json"
                if path.is_symlink() or not path.is_file():
                    return {"status": "UNRESOLVED_RESERVATION", "bindings": []}
                value = json.loads(path.read_bytes())
                spec = value["spec"]
                recipe = spec["recipe"]
                prior = recipe["dataset_snapshot"]
                if (
                    prior["dataset_id"] == dataset_id
                    or prior["content_hash"] == snapshot.content_hash
                    or (
                        datetime.fromisoformat(prior["min_timestamp"])
                        <= snapshot.max_timestamp
                        and snapshot.min_timestamp
                        <= datetime.fromisoformat(prior["max_timestamp"])
                        and recipe["symbol"] == symbol
                    )
                ):
                    bindings.append(
                        {
                            "program_id": value["research_program_id"],
                            "family_id": value["trial_family_id"],
                            "run_id": spec["request"]["experiment_id"],
                        }
                    )
            return {
                "status": "BOUND_TO_PRIOR_PROGRAM" if bindings else "AVAILABLE",
                "bindings": bindings,
            }

    def propose_and_run(
        self,
        *,
        dataset_id: str,
        program_id: str,
        family_id: str,
        run_id: str,
        goal: str,
        short_window: int | None = None,
        long_window: int | None = None,
        submitted_at: datetime,
        model_call_id: str,
        rationale: str,
        family: StrategyFamily | None = None,
        parameters: Mapping[str, Any] | None = None,
        provenance_source: Literal["llm", "scripted"] = "llm",
    ) -> dict[str, Any]:
        if provenance_source not in ("llm", "scripted"):
            raise ValueError("research provenance must be llm or scripted")
        legacy = family is None and parameters is None
        if legacy:
            if short_window is None or long_window is None:
                raise ValueError(
                    "legacy SMA requires both short_window and long_window"
                )
            strategy = {
                "name": "moving_average_crossover",
                "short_window": short_window,
                "long_window": long_window,
                "position_rule": "long_when_short_above_long_else_flat",
            }
        else:
            if (
                family is None
                or not isinstance(parameters, Mapping)
                or short_window is not None
                or long_window is not None
            ):
                raise ValueError(
                    "supply family and parameters together without legacy SMA fields"
                )
            strategy = strategy_from_parameters(family, parameters)
        snapshot = self._runner.catalog.verify(dataset_id)
        frame = self._runner.catalog.read(dataset_id)
        n = snapshot.row_count
        if n < self.MIN_OBSERVATIONS:
            raise ValueError("at least 80 observations required")
        validation = max(10, n // 5)
        test = validation
        development = n - validation - test
        warmup = min(50, development // 3)
        if (
            required_observations(config_from_strategy(strategy, legacy=legacy))
            > warmup
        ):
            raise ValueError("strategy exceeds frozen warmup allowance")
        times = _timestamps(frame)
        names = (
            "development_start",
            "development_end",
            "validation_start",
            "validation_end",
            "test_start",
            "test_end",
        )
        indices = (
            0,
            development - 1,
            development,
            development + validation - 1,
            development + validation,
            n - 1,
        )
        request = ResearchRequest(
            schema_version="research-request-v1",
            experiment_id=run_id,
            goal_text=goal,
            submitted_at=submitted_at,
            symbol=str(frame["symbol"][0]),
            frequency="daily",
            constraints=FrozenPayload.from_payload(
                {"max_drawdown_loss_magnitude": 0.15}, name="constraints"
            ),
            provenance=FrozenPayload.from_payload(
                {"source": provenance_source, "reference": model_call_id},
                name="provenance",
            ),
        )
        proposal = {
            "schema_version": "sma-proposal-v1" if legacy else "strategy-proposal-v2",
            "hypothesis": {
                "statement": goal,
                "rationale": rationale,
                "falsification": "Reject a research screen breach; hold out final test until freeze.",
            },
            "strategy": strategy,
            "execution": {"timing": "next_bar_open", "terminal_liquidation": False},
            "backtest": {
                "initial_cash": 100000.0,
                "fee_bps": 10.0,
                "periods_per_year": 252,
            },
            "costs": {
                "model": "position_switch_fee_bps",
                "slippage": "not_modelled",
                "dividends": "not_modelled",
            },
            "split": {
                "observations": n,
                "warmup_bars": warmup,
                "development_bars": development,
                "validation_bars": validation,
                "test_bars": test,
                "validation_window_bars": 1 if validation % 5 else 5,
            },
            "parameter_optimization": "none",
            "random_seed": 0,
        }
        spec = admit_research_request(
            request,
            proposal=proposal,
            dataset_snapshot=snapshot,
            data_conventions={
                "timestamp": "UTC session-date label",
                "session": "Provider daily session",
                "adjustment": "Provider raw OHLCV; consult qualified snapshot limitations",
                "vintage": "Immutable retained provider response",
            },
            code_context=code_context(),
            split_timestamps={
                name: times[i].isoformat()
                for name, i in zip(names, indices, strict=True)
            },
        )
        try:
            return self._runner.run_experiment(
                spec, research_program_id=program_id, trial_family_id=family_id
            )
        except ExperimentRunError as exc:
            code = {
                "same dataset is already bound to another research family/program": "CORE_DATASET_ALREADY_BOUND",
                "trial family partitions cannot change": "CORE_PARTITIONS_CHANGED",
                "trial family dataset/vintage cannot change": "CORE_DATASET_VINTAGE_CHANGED",
                "bounded research family exhausted": "CORE_TRIAL_BUDGET_EXHAUSTED",
                "executing code context differs from recipe": "CORE_CODE_CONTEXT_MISMATCH",
                "run already terminal; no redispatch": "CORE_RUN_ALREADY_TERMINAL",
            }.get(str(exc), "CORE_EXPERIMENT_FAILED")
            raise ResearchExecutionError(code) from None

    def inspect(self, run_id: str) -> dict[str, Any]:
        return self._runner.inspect(run_id)

    def reproduce(self, run_id: str) -> dict[str, Any]:
        return self._runner.reproduce(run_id)

    def list_runs(self) -> list[dict[str, Any]]:
        return [
            {
                "run_id": r.experiment_id,
                "evidence_id": r.metrics["development_run"]["evidence_id"],
            }
            for r in self._runner.store.list()
            if "development_run" in r.metrics
        ]

    def research_diagnostics(
        self, run_ids: list[str], *, frozen_trial_budget: int
    ) -> dict[str, Any]:
        """Trusted, development-only multiplicity diagnostics for a bounded AI path.

        Adaptive proposals cannot be relabelled as a fixed-grid PBO experiment.
        Facts come from verified EvidencePacks, never model-supplied return streams.
        """
        from .scale.statistics import (
            block_sign_diagnostic,
            cscv_pbo,
            dsr_not_applicable,
        )

        if (
            type(frozen_trial_budget) is not int
            or not 1 <= frozen_trial_budget <= 100
            or not run_ids
            or len(set(run_ids)) != len(run_ids)
            or len(run_ids) > frozen_trial_budget
        ):
            raise ValueError("invalid frozen adaptive trial budget")
        diagnostics = []
        for run_id in run_ids:
            evidence = self.inspect(run_id)
            facts = evidence["FACT"]
            periods = facts["validation_periods"]
            returns = [row["strategy_return"] for row in periods]
            realism = self.execution_realism(run_id)
            realism["scenarios"] = [
                {k: v for k, v in scenario.items() if k != "periods"}
                for scenario in realism["scenarios"]
            ]
            diagnostics.append(
                {
                    "run_id": run_id,
                    "evidence_id": evidence_id(evidence),
                    "block_sign": block_sign_diagnostic(
                        returns,
                        block_size=20,
                        frozen_trial_budget=frozen_trial_budget,
                        selection_design="adaptive_path",
                    ),
                    "validation_windows": facts["validation_windows"],
                    "costs": evidence["DECLARED_ASSUMPTION"],
                    "limitations": evidence["LIMITATION"],
                    "execution_realism": realism,
                }
            )
        return {
            "schema_version": "bounded-adaptive-diagnostics-v1",
            "frozen_trial_budget": frozen_trial_budget,
            "attempted_runs": len(run_ids),
            "pbo": cscv_pbo([], selection_design="adaptive_path"),
            "dsr": dsr_not_applicable(),
            "trials": diagnostics,
            "validated_alpha": False,
            "live_eligible": False,
        }

    def execution_realism(self, run_id: str) -> dict[str, Any]:
        """Supplement verified validation decisions with fixed cash/lot stress.

        Raw market data is read only inside Core. Lab receives no final prices,
        and this diagnostic never overwrites admitted metrics or stored facts.
        """
        from .realism import cash_lot_stress

        evidence = self.inspect(run_id)
        facts = evidence["FACT"]
        recipe = facts["specification"]["recipe"]
        frame = self._runner.catalog.read(recipe["dataset_snapshot"]["dataset_id"])
        market = {
            row["timestamp"].isoformat(): row for row in frame.iter_rows(named=True)
        }
        rows = []
        for period in facts["validation_periods"]:
            stamp = datetime.fromisoformat(period["timestamp"]).isoformat()
            bar = market[stamp]
            rows.append(
                {
                    "timestamp": stamp,
                    "open": bar["open"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "target": period["position_after_open"],
                }
            )
        return cash_lot_stress(rows, evidence_id=evidence_id(evidence))

    def freeze(self, run_id: str, *, program_id: str) -> dict[str, Any]:
        from .policy import freeze_candidate

        return freeze_candidate(self._runner, run_id, program_id=program_id)

    def model_context(self, run_id: str) -> dict[str, Any]:
        evidence = self.inspect(run_id)
        facts = evidence["FACT"]
        strategy = facts["specification"]["recipe"]["strategy"]
        legacy = facts["specification"]["schema_version"] == "experiment-spec-v1"
        return {
            "evidence_id": evidence_id(evidence),
            "FACT": {
                name: facts[name]
                for name in (
                    "run_id",
                    "evaluation_stage",
                    "development_metrics",
                    "validation_metrics",
                    "validation_windows",
                    "screen_pass",
                    "final_lockbox_scored",
                    "research",
                )
            }
            | ({"families": facts["families"]} if not legacy else {}),
            "strategy": strategy
            if legacy
            else {
                "family": strategy["name"],
                "parameters": {
                    k: v
                    for k, v in strategy.items()
                    if k not in {"name", "position_rule"}
                },
            },
            "DECLARED_ASSUMPTION": evidence["DECLARED_ASSUMPTION"],
            "LIMITATION": evidence["LIMITATION"],
        }
