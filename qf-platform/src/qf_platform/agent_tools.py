"""Pinned real-data task tools through Core's public, development-only facade."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qf_lab.bounded_planner import MomentumIntent, SmaIntent
from quantforge.data import logical_content_hash
from quantforge.product.evidence import evidence_id
from quantforge.product.pilot.data import read_snapshot
from quantforge.product.service import ResearchService
from quantforge.product.storage import local_root, writer_lock
from quantforge.strategies.models import strategy_from_parameters

from .dataset_use import WORKSPACE, register_research_use
from .storage import PlatformError, digest

DATASET = "cn510300-exposed-raw-v1"
PROGRAM = "agent-lt-20261013"
FAMILY = "bounded-task-v1"
CORE_TRIAL_LIMIT = 8


class AgentCoreBusy(PlatformError):
    """The host gate denied admission before a Core operation was dispatched."""


@dataclass(frozen=True)
class AgentDataset:
    pilot_root: Path
    snapshot_id: str
    inventory_path: Path


def default_dataset() -> AgentDataset:
    return AgentDataset(
        WORKSPACE / "local-smoke/strategy-optimization-20261013/run-510300-v2",
        "cnpilot-6159c44497b3310260d2946e",
        WORKSPACE
        / "local-smoke/long-term-20261013/data-quality"
        / "vintage-20261013T063055-e0d2531f/inventory.json",
    )


class AgentTools:
    def __init__(
        self,
        core_root: Path,
        *,
        source: AgentDataset | None = None,
        usage_root: Path | None = None,
    ) -> None:
        core_root.mkdir(parents=True, exist_ok=True)
        self.core_root = local_root(core_root)
        self.source = default_dataset() if source is None else source
        self.usage_root = usage_root
        self._lock = threading.RLock()
        self._gate_root = self.core_root / "host-gate"
        self._gate_root.mkdir(exist_ok=True)

    @contextmanager
    def _gate(self) -> Iterator[None]:
        with self._lock:
            context = writer_lock(self._gate_root)
            try:
                context.__enter__()
            except OSError:
                raise AgentCoreBusy("AGENT_CORE_BUSY_NOT_DISPATCHED") from None
            try:
                yield
            finally:
                context.__exit__(None, None, None)

    def _data(self) -> tuple[Any, dict[str, Any]]:
        path = self.source.inventory_path
        local_root(path.parent)
        if path.is_symlink() or path.stat().st_size > 3_000_000:
            raise PlatformError("AGENT_SOURCE_MANIFEST_INVALID")
        inventory = json.loads(path.read_bytes())
        checksum_path = path.with_name("inventory-digest.json")
        if (
            checksum_path.is_symlink()
            or digest(inventory) != json.loads(checksum_path.read_bytes())["sha256"]
        ):
            raise PlatformError("AGENT_SOURCE_MANIFEST_INVALID")
        frame, snapshot = read_snapshot(self.source.pilot_root, self.source.snapshot_id)
        report = inventory["assets"]["510300"]
        if (
            frame["symbol"].unique().to_list() != ["510300"]
            or logical_content_hash(frame) != report["content_hash"]
            or report["price_research_allowed"] is not True
        ):
            raise PlatformError("AGENT_SOURCE_DATA_OR_QUALITY_DENIED")
        register_research_use(
            frame,
            program=PROGRAM + "-source",
            reference_hash=digest(inventory),
            registry_root=self.usage_root,
        )
        return frame, {
            "inventory_hash": digest(inventory),
            "snapshot_hash": digest(snapshot),
            "source": report["source"],
            "quality_status": report["status"],
        }

    def _bind(self, core: ResearchService) -> tuple[str, dict[str, Any]]:
        frame, declaration = self._data()
        admitted = core.import_pilot_dataset(
            self.source.pilot_root,
            self.source.snapshot_id,
            start=frame["timestamp"][0].date().isoformat(),
            end=frame["timestamp"][-1].date().isoformat(),
            exposure_declaration="All dates are already exposed "
            "retrospective research; "
            "development/validation labels do not establish untouched test data, "
            "independent alpha, exchange execution or total return.",
        )
        return str(admitted["dataset"]["dataset_id"]), {
            **declaration,
            "dataset_snapshot": admitted["dataset"],
            "qualification": admitted["qualification"],
        }

    @staticmethod
    def _availability(core: ResearchService, dataset_id: str) -> list[dict[str, Any]]:
        view = core.research_availability(dataset_id)
        if view["status"] not in {"AVAILABLE", "BOUND_TO_PRIOR_PROGRAM"}:
            raise PlatformError("AGENT_CORE_SOURCE_UNRESOLVED")
        bindings = view["bindings"]
        if any(
            row["program_id"] != PROGRAM or row["family_id"] != FAMILY
            for row in bindings
        ):
            raise PlatformError("AGENT_CORE_SOURCE_BOUND_TO_OTHER_PROGRAM")
        # A retained reservation without a verified pack is an unresolved effect.
        for row in bindings:
            core.inspect(row["run_id"])
        return list(bindings)

    def inspect_dataset(self, dataset: str) -> dict[str, Any]:
        with self._gate():
            return self._inspect_dataset(dataset)

    def _inspect_dataset(self, dataset: str) -> dict[str, Any]:
        if dataset != DATASET:
            raise PlatformError("AGENT_DATASET_NOT_ALLOWED")
        core = ResearchService(self.core_root)
        try:
            dataset_id, data = self._bind(core)
            bindings = self._availability(core, dataset_id)
            return {
                "dataset": DATASET,
                **data,
                "core_trial_limit": CORE_TRIAL_LIMIT,
                "core_trials_remaining": CORE_TRIAL_LIMIT - len(bindings),
                "scope": "ALREADY_EXPOSED_DEVELOPMENT_ONLY",
                "basis": "RAW_PRICE_ONLY",
                "total_return_qualified": False,
                "validated_alpha": False,
                "live_authority": "NONE",
                "tool_allowlist": [
                    "inspect_dataset",
                    "run_experiment",
                    "compare_results",
                ],
            }
        finally:
            core.close()

    def run_experiment(
        self,
        payload: dict[str, Any],
        *,
        task_id: str,
        index: int,
        call_id: str,
        goal: str,
        rationale: str,
    ) -> dict[str, Any]:
        with self._gate():
            return self._run_experiment(
                payload,
                task_id=task_id,
                index=index,
                call_id=call_id,
                goal=goal,
                rationale=rationale,
            )

    def _run_experiment(
        self,
        payload: dict[str, Any],
        *,
        task_id: str,
        index: int,
        call_id: str,
        goal: str,
        rationale: str,
    ) -> dict[str, Any]:
        intent = (
            SmaIntent.model_validate(payload)
            if payload.get("family") == "moving_average_crossover"
            else MomentumIntent.model_validate(payload)
        )
        if (
            not task_id.startswith("task-")
            or len(task_id) != 37
            or any(value not in "0123456789abcdef" for value in task_id[5:])
            or type(index) is not int
            or not 0 <= index < 6
        ):
            raise PlatformError("AGENT_TASK_OR_STEP_ID_INVALID")
        run_id = "agent-" + task_id[5:] + "-" + str(index)
        core = ResearchService(self.core_root)
        try:
            dataset_id, _ = self._bind(core)
            bindings = self._availability(core, dataset_id)
            existing = next((row for row in bindings if row["run_id"] == run_id), None)
            if existing:
                pack = core.inspect(run_id)
                spec = pack["FACT"]["specification"]
                if (
                    spec["request"]["goal_text"] != goal
                    or spec["request"]["provenance"]["reference"] != call_id
                    or spec["recipe"]["strategy"]
                    != strategy_from_parameters(intent.family, intent.parameters)
                ):
                    raise PlatformError("AGENT_CORE_REPLAY_INPUT_CONFLICT")
            else:
                if len(bindings) >= CORE_TRIAL_LIMIT:
                    raise PlatformError("AGENT_CORE_TRIAL_BUDGET_EXHAUSTED")
                pack = core.propose_and_run(
                    dataset_id=dataset_id,
                    program_id=PROGRAM,
                    family_id=FAMILY,
                    run_id=run_id,
                    goal=goal,
                    submitted_at=datetime.now(UTC),
                    model_call_id=call_id,
                    rationale=rationale,
                    family=intent.family,
                    parameters=intent.parameters,
                    provenance_source="llm",
                )
            if (
                pack["FACT"]["evaluation_stage"] != "DEVELOPMENT"
                or pack["FACT"]["final_lockbox_scored"] is not False
            ):
                raise PlatformError("AGENT_CORE_STAGE_INVALID")
            return {
                "core_evidence_id": evidence_id(pack),
                "evidence_pack": pack,
                "run_id": run_id,
                "scope": "ALREADY_EXPOSED_DEVELOPMENT_ONLY",
                "basis": "RAW_PRICE_ONLY",
                "validated_alpha": False,
                "live_authority": "NONE",
            }
        finally:
            core.close()

    def compare_results(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        with self._gate():
            return self._compare_results(results)

    def _compare_results(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        if not results:
            raise PlatformError("AGENT_CORE_EVIDENCE_REQUIRED")
        rows = []
        for result in results:
            pack = result["evidence_pack"]
            core = ResearchService(self.core_root)
            try:
                if core.inspect(result["run_id"]) != pack:
                    raise PlatformError("AGENT_CORE_EVIDENCE_MISMATCH")
            finally:
                core.close()
            if (
                evidence_id(pack) != result["core_evidence_id"]
                or pack["FACT"]["final_lockbox_scored"] is not False
            ):
                raise PlatformError("AGENT_CORE_EVIDENCE_MISMATCH")
            rows.append(
                {
                    "core_evidence_id": result["core_evidence_id"],
                    "run_id": result["run_id"],
                    "strategy": pack["FACT"]["specification"]["recipe"]["strategy"],
                    "development_metrics": pack["FACT"]["development_metrics"],
                    "validation_metrics": pack["FACT"]["validation_metrics"],
                }
            )
        return {
            "rows": rows,
            "conclusion": "DESCRIPTIVE_ONLY",
            "mode": "REAL_RESEARCH",
            "basis": "RAW_PRICE_ONLY",
            "validated_alpha": False,
            "live_authority": "NONE",
            "limitations": [
                "All source intervals were previously exposed; "
                "internal splits are not independent final data.",
                "Fixed 10 bps switch fee; no lot, actual commission, "
                "slippage or distribution cash ledger.",
                "Only the host emits computed metrics "
                "and verified Core EvidencePack identities.",
                "No generated code execution, final access, "
                "broker actions or live authority.",
            ],
        }
