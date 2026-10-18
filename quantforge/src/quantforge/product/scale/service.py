"""Host-owned scale authority and a JSON-only development projection."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from quantforge.data import BAR_SCHEMA, ParquetDataCatalog, logical_content_hash
from quantforge.experiments import ArtifactRef, ExperimentRecord, ExperimentStore
from quantforge.product.context import code_context, source_identity
from quantforge.product.evidence import evidence_id, read_evidence
from quantforge.product.storage import local_root, writer_lock

from .evaluator import evaluate, synchronized_frames
from .registry import ProgramRegistry, identifier
from .replay import replay_step
from .statistics import block_sign_diagnostic, cscv_pbo, dsr_not_applicable
from .storage import Journal, ScaleError, digest, publish, read_json
from .vault import HostBenchmarkCapability, inspect_seal, seal

METRIC_KEYS = ("total_return", "sharpe", "max_drawdown")


def execution_identity() -> dict[str, Any]:
    return {
        **code_context(),
        "source_sha256": source_identity(),
        "cryptography": importlib.metadata.version("cryptography"),
    }


def encode_frames(frames: dict[str, pl.DataFrame]) -> dict[str, Any]:
    return {
        symbol: frame.with_columns(pl.col("timestamp").cast(pl.Int64)).to_dict(as_series=False)
        for symbol, frame in frames.items()
    }


def decode_frames(value: dict[str, Any]) -> dict[str, pl.DataFrame]:
    return {
        symbol: pl.DataFrame(columns).with_columns(
            [pl.col(name).cast(dtype) for name, dtype in BAR_SCHEMA.items()]
        ).select(list(BAR_SCHEMA))
        for symbol, columns in value.items()
    }


def prepare_program(
    root: Path,
    frames: dict[str, pl.DataFrame],
    qualification: dict[str, Any],
    declaration: dict[str, Any],
    *,
    key_path: Path | None = None,
) -> dict[str, Any]:
    """Host acquisition only. Declare before any research result is computed."""
    root = local_root(root)
    if list(root.iterdir()):
        raise ScaleError("PREPARATION_REQUIRES_EMPTY_ROOT")
    program = identifier(declaration["program_id"])
    selected = synchronized_frames(frames)
    families = declaration["budgets"]["families"]
    if any(type(cap) is not int or cap < 5 or cap % 5 for cap in families.values()):
        raise ScaleError("FAMILY_BUDGET_MUST_RESERVE_COMPLETE_FIVE_SLOT_ATTEMPTS")
    synthetic = qualification == {"host_fixture": True}
    if not synthetic and qualification.get("schema") != "research-scale-corpus-qualification-v1":
        raise ScaleError("QUALIFIED_CORPUS_OR_EXPLICIT_SYNTHETIC_FIXTURE_REQUIRED")
    if not synthetic and (qualification.get("universe_id") != declaration["universe_id"]
                          or qualification.get("content_hashes") != {s: logical_content_hash(f) for s, f in selected.items()}):
        raise ScaleError("CORPUS_UNIVERSE_OR_CONTENT_BINDING_MISMATCH")
    if not synthetic:
        required = {"schema", "universe_id", "universe_sha256", "source", "content_hashes", "as_of",
                    "alignment", "diagnostics", "classifications", "corpus_sha256", "corpus_id"}
        groups = {"OBSERVED_FACT", "SOURCE_CLAIM", "LOCAL_VALIDATION", "ASSUMPTION", "LIMITATION", "LICENSE / DISTRIBUTION NOTE"}
        expected_hash = digest({k: v for k, v in qualification.items() if k not in ("corpus_sha256", "corpus_id")})
        if (set(qualification) != required
            or set(qualification.get("classifications", {})) != groups
            or qualification["corpus_sha256"] != expected_hash
            or qualification["corpus_id"] != "qf-qlib-corpus-" + expected_hash[:24]):
            raise ScaleError("CORPUS_IDENTITY_OR_METADATA_DRIFT")
    cutoff = datetime.fromisoformat(declaration["research_end"])
    if cutoff.tzinfo is None:
        raise ScaleError("AWARE_PARTITION_REQUIRED")
    prefix = {s: f.filter(pl.col("timestamp") <= cutoff) for s, f in selected.items()}
    research_n = next(iter(prefix.values())).height
    history = declaration["protocol"]["initial_history_bars"]
    if type(history) is not int or history < 50 or research_n < history + 20:
        raise ScaleError("INSUFFICIENT_DECLARED_RESEARCH_HISTORY")
    if any(f.height - research_n < 20 for f in selected.values()):
        raise ScaleError("INSUFFICIENT_SEALED_BENCHMARK")
    # A native per-symbol catalog contains only the physical research prefix.
    for name in ("catalog", "evidence", "worker", "replay"):
        (root / name).mkdir()
    catalog = ParquetDataCatalog(root / "catalog")
    snapshots = {
        symbol: catalog.write(
            "prefix-" + symbol, frame,
            source="pinned-qlib-normalized-adjusted-research",
            schema_version="bars-v1", as_of=cutoff,
        ).model_dump(mode="json")
        for symbol, frame in prefix.items()
    }
    prefix_id = digest(snapshots)
    benchmark_key = digest(encode_frames({s: f.slice(research_n) for s, f in selected.items()}))
    binding = {
        "program_id": program, "universe_id": declaration["universe_id"],
        "prefix_id": prefix_id, "benchmark_key": benchmark_key,
        "protocol_sha256": digest(declaration["protocol"]),
    }
    sealed = seal(root, program, {
        "frames": encode_frames(selected), "qualification": qualification,
        "research_bars": research_n,
    }, binding, key_path=key_path)
    manifest = {
        **declaration, "schema_version": "scale-program-v1", "code": execution_identity(),
        "snapshots": snapshots, "prefix_id": prefix_id, "sealed": sealed,
        "qualification_mode": "SYNTHETIC_ENGINEERING_FIXTURE" if synthetic else "AUTHENTICATED_QLIB_CORPUS",
        "selection_rule": "maximum_development_sharpe_then_lowest_allocation_primary_only",
        "probe_policy": "four_counted_nonselectable_neighbor_slots_per_attempt",
        "scenario_scan_multiplier": 16,
        "data_semantics": {
            "prices": "normalized_adjusted_research_units",
            "volume": "normalized_research_proxy_original_unit_unverified",
            "pit": "retrospective_availability_selected_survivors",
            "corporate_actions": "provider_adjustment_no_entitlement_ledger",
            "licensing": "upstream_vendor_rights_not_independently_proven_no_redistribution_claim",
            "missing": "complete_common_sessions_no_fill_no_replacement",
        },
    }
    registry = ProgramRegistry(root)
    try:
        registry.create(manifest)
    finally:
        registry.close()
    return {"program_id": program, "manifest_id": digest(manifest), "prefix_id": prefix_id,
            "benchmark_state": "SEALED", "real_transport": "LOCKED"}


class ScaleService:
    """Supported intelligence capability: contexts, proposals, budget and history only.

    This Python facade is a capability discipline, not an OS sandbox for hostile
    same-user code. Host benchmark and replay operations are separate functions.
    """

    def __init__(self, root: Path) -> None:
        self._root = local_root(root)
        self._registry = ProgramRegistry(self._root)
        self._worker_held = False
        self._registry.history()

    @contextmanager
    def campaign_lock(self) -> Iterator[None]:
        if self._worker_held:
            raise ScaleError("WORKER_ALREADY_HELD")
        with writer_lock(local_root(self._root / "worker")):
            self._worker_held = True
            try:
                yield
            finally:
                self._worker_held = False

    def _identity(self) -> None:
        expected = self._registry.manifest["code"]
        actual = execution_identity()
        # A later documentation/integration commit is compatible if executable
        # source, runtime and lock bytes remain exact. Original SHA is retained.
        if {k: v for k, v in expected.items() if k != "git_commit"} != {
            k: v for k, v in actual.items() if k != "git_commit"
        }:
            raise ScaleError("SOURCE_OR_RUNTIME_DRIFT")

    def _frames(self) -> dict[str, pl.DataFrame]:
        self._identity()
        catalog = ParquetDataCatalog(local_root(self._root / "catalog"))
        result = {}
        for symbol, snapshot in self._registry.manifest["snapshots"].items():
            observed = catalog.verify(snapshot["dataset_id"])
            if observed.model_dump(mode="json") != snapshot:
                raise ScaleError("DATASET_SUBSTITUTION")
            frame = catalog.read(snapshot["dataset_id"])
            if logical_content_hash(frame) != snapshot["content_hash"]:
                raise ScaleError("DATASET_CONTENT_DRIFT")
            result[symbol] = frame
        return synchronized_frames(result)

    def status(self) -> dict[str, Any]:
        status = self._registry.status()
        history = self._registry.history()
        terminals = {e["allocation"]: e for e in history if e["kind"] == "TRIAL_TERMINAL"}
        status["trial_history"] = [
            {**{k: e[k] for k in ("allocation", "trial_id", "family", "origin")},
             **{k: terminals.get(e["allocation"], {}).get(k) for k in ("status", "reason", "run_id")}}
            for e in history if e["kind"] == "TRIAL_ALLOCATED"
        ]
        return status

    def _evidence(self, run_id: str) -> dict[str, Any]:
        identifier(run_id)
        pack = read_evidence(self._root / "evidence" / (run_id + ".json"))
        fact = pack["FACT"]
        store = ExperimentStore(self._root / "experiments.sqlite")
        try:
            record = store.get(run_id)
            expected_record = self._record(pack)
            if record != expected_record:
                raise ScaleError("RECORD_EVIDENCE_MISMATCH")
        finally:
            store.close()
        terminal = next((e for e in self._registry.history() if e["kind"] == "TRIAL_TERMINAL"
                         and e.get("run_id") == run_id), None)
        if terminal is None or terminal["evidence_id"] != evidence_id(pack):
            raise ScaleError("EVIDENCE_REGISTRY_MISMATCH")
        if fact["manifest_id"] != digest(self._registry.manifest):
            raise ScaleError("EVIDENCE_PROGRAM_MISMATCH")
        return pack

    def _projection(self, event: dict[str, Any]) -> dict[str, Any]:
        pack = self._evidence(event["run_id"])
        fact = pack["FACT"]
        return {"evidence_id": evidence_id(pack), "run_id": event["run_id"],
                "family": fact["family"], "strategy": fact["proposal"],
                "metrics": {k: fact["evaluation"]["metrics"][k] for k in METRIC_KEYS},
                "limitations": pack["LIMITATION"]}

    def context(self, family: str) -> dict[str, Any]:
        self._registry.ensure_search()
        self._frames()
        manifest = self._registry.manifest
        if family not in manifest["budgets"]["families"]:
            raise ScaleError("FAMILY_NOT_PREDECLARED")
        history = self._registry.history()
        primary = {e["allocation"] for e in history if e["kind"] == "TRIAL_ALLOCATED"
                   and e["origin"] != "STRESS"}
        completed = [e for e in history if e["kind"] == "TRIAL_TERMINAL"
                     and e["status"] == "COMPLETED" and e["allocation"] in primary]
        projections = [self._projection(e) for e in completed]
        return {
            "phase": "next" if projections else "first", "goal": manifest["goal"],
            "max_long_window": 50, "evidence": projections[-12:], "family": family,
            "round_budget": max(0, (manifest["budgets"]["families"][family] - self.status()["family_counts"][family]) // 5),
            "allowed_actions": ["propose", "modify", "branch", "continue", "reject"],
            "context_id": digest({"history": history, "family": family}),
            "benchmark_state": "SEALED",
            "state_epoch": len(history),
        }

    def reserve_model(self, *, family: str) -> str:
        with self._owned_or_new_lock():
            self._identity()
            return self._registry.reserve_model(family=family)

    def finish_model(self, call: str, *, status: str, ledger_id: str | None = None) -> None:
        self._registry.finish_model(call, status=status, ledger_id=ledger_id)

    def _record(self, pack: dict[str, Any]) -> ExperimentRecord:
        fact = pack["FACT"]
        config = {k: fact[k] for k in ("manifest_id", "allocation", "family", "proposal", "protocol", "code")}
        return ExperimentRecord.create(
            experiment_id=fact["run_id"], git_commit=fact["code"]["git_commit"],
            dataset_id="scale-" + self._registry.manifest["program_id"],
            dataset_hash=self._registry.manifest["prefix_id"], backtest_config=config,
            random_seed=0, started_at=datetime.fromisoformat(fact["started_at"]),
            metrics=fact["evaluation"]["metrics"],
            artifact_refs=(ArtifactRef(name="evidence", uri="evidence/" + fact["run_id"] + ".json"),),
        )

    def submit(self, trial_id: str, family: str, proposal: dict[str, Any], *,
               origin: str = "AI", call_id: str | None = None) -> dict[str, Any]:
        with self._owned_or_new_lock():
            return self._submit(trial_id, family, proposal, origin=origin, call_id=call_id)

    def _submit(self, trial_id: str, family: str, proposal: dict[str, Any], *,
                origin: str, call_id: str | None) -> dict[str, Any]:
        self._identity()
        bundle = self._registry.allocate_bundle(trial_id, family, proposal, origin=origin, call_id=call_id)
        primary, probes = bundle[0], bundle[1:]
        allocation = primary["allocation"]
        valid = primary["admitted"]
        try:
            if not valid:
                return {**primary, "status": "REJECTED"}
            frames = self._frames()
            rows = sum(f.height for f in frames.values())
            self._registry.reserve_scan(rows * self._registry.manifest["scenario_scan_multiplier"])
            protocol = self._registry.manifest["protocol"]
            result = evaluate(frames, proposal, protocol)
            run_id = "trial-" + str(allocation)
            pack = {
                "schema_version": "evidence-pack-v1",
                "FACT": {
                    "run_id": run_id, "evaluation_stage": "DEVELOPMENT",
                    "manifest_id": digest(self._registry.manifest), "allocation": allocation,
                    "family": family, "proposal": proposal, "protocol": protocol,
                    "code": self._registry.manifest["code"], "started_at": datetime.now(UTC).isoformat(),
                    "probe_allocations": [e["allocation"] for e in probes], "evaluation": result,
                    "diagnostics": {
                        "pbo": cscv_pbo([], selection_design="adaptive_path"),
                        "dsr": dsr_not_applicable(),
                        "block_sign": block_sign_diagnostic(result["returns"], block_size=20,
                            frozen_trial_budget=self._registry.manifest["budgets"]["trials"]),
                    },
                },
                "DECLARED_ASSUMPTION": result["assumptions"],
                "LIMITATION": result["limitations"] + [
                    "Retrospective survivor/availability selected corpus; no institutional point-in-time universe.",
                    "No validated alpha; fixed-grid PBO and DSR are not applicable to this adaptive path.",
                    "Underlying vendor redistribution rights not independently proven.",
                ],
            }
            store = ExperimentStore(self._root / "experiments.sqlite")
            try:
                with writer_lock(self._root):
                    publish(self._root / "evidence" / (run_id + ".json"), pack)
                    store.save(self._record(pack))
            finally:
                store.close()
            self._registry.terminal(allocation, "COMPLETED", run_id=run_id, evidence_id=evidence_id(pack))
            for probe in probes:
                if probe["admitted"]:
                    self._registry.terminal(probe["allocation"], "COMPLETED", parent=allocation,
                                            selectable=False, evidence_id=evidence_id(pack))
            return {**primary, "status": "COMPLETED", "run_id": run_id,
                    "evidence_id": evidence_id(pack), "projection": self._projection({"run_id": run_id})}
        except Exception:  # noqa: BLE001 -- retain a counted failure for every compute failure
            pending = self._registry.status()["pending_trials"]
            for item in [primary, *probes]:
                if item["allocation"] in pending:
                    self._registry.terminal(item["allocation"], "FAILED", reason="COMPUTATION_OR_PUBLICATION_FAILED")
            raise ScaleError("COUNTED_TRIAL_FAILED_INSPECT_HISTORY") from None

    def reproduce(self, run_id: str) -> dict[str, Any]:
        pack = self._evidence(run_id)
        fact = pack["FACT"]
        frames = self._frames()
        result = evaluate(frames, fact["proposal"], fact["protocol"])
        if result != fact["evaluation"]:
            raise ScaleError("EXACT_REPRODUCTION_FAILED")
        return {"run_id": run_id, "evidence_id": evidence_id(pack), "exact": True,
                "evaluation_sha256": digest(result), "source_sha256": source_identity()}

    def recover(self, *, locked: bool = False) -> dict[str, Any]:
        if not locked:
            with self.campaign_lock():
                return self.recover(locked=True)
        if not self._worker_held:
            raise ScaleError("WORKER_LOCK_REQUIRED")
        history = self._registry.history()
        for event in history:
            if (event["kind"] == "MODEL_RESERVED"
                and not any(e["kind"] == "TRIAL_ALLOCATED" and e.get("call_id") == event["call"] for e in history)):
                try:
                    self.submit("unknown-" + event["call"], event["family"], {}, call_id=event["call"])
                except ScaleError:
                    pass  # spent/expired budgets never redispatch unknown work
        self._registry.recover()
        if self._registry.status()["benchmark_state"] == "SPENT" and not any(e["kind"] == "BENCHMARK_TERMINAL" for e in self._registry.history()):
            with writer_lock(self._root):
                self._registry.journal.append("BENCHMARK_TERMINAL", status="UNKNOWN", reason="INTERRUPTED_AFTER_PERMANENT_RESERVATION_NO_RETRY")
        return self.status()

    def pause(self, paused: bool) -> dict[str, Any]:
        return self._registry.pause(paused)

    def freeze(self) -> dict[str, Any]:
        lock = self._owned_or_new_lock()
        with lock:
            status = self.status()
            if status["benchmark_state"] != "SEALED" or status["pending_trials"] or status["unresolved_model_calls"]:
                raise ScaleError("CANNOT_FREEZE_ACTIVE_OR_CLOSED_PROGRAM")
            if status["trials"] != status["budget"]["trials"]:
                raise ScaleError("PRECOMMITTED_SEARCH_NOT_EXHAUSTED")
            completed = [e for e in status["trial_history"] if e["origin"] != "STRESS" and e["status"] == "COMPLETED"]
            if not completed:
                raise ScaleError("NO_COMPLETED_RESEARCH_CANDIDATE")
            chosen = min(completed, key=lambda e: (-self._evidence(e["run_id"])["FACT"]["evaluation"]["metrics"]["sharpe"], e["allocation"]))
            proof = self.reproduce(chosen["run_id"])
            fact = self._evidence(chosen["run_id"])["FACT"]
            frozen = {"run_id": chosen["run_id"], "evidence_id": proof["evidence_id"],
                      "proposal": fact["proposal"], "manifest_id": status["manifest_id"],
                      "rule": self._registry.manifest["selection_rule"], "reproduction": proof}
            with writer_lock(self._root):
                self._registry.journal.append("CANDIDATE_FROZEN", **frozen)
            return frozen

    @contextmanager
    def _owned_or_new_lock(self) -> Iterator[None]:
        if self._worker_held:
            yield
        else:
            with self.campaign_lock():
                yield

    def inspect(self) -> dict[str, Any]:
        return {"status": self.status(), "data_semantics": self._registry.manifest["data_semantics"],
                "live_eligible": False, "validated_alpha": False}

    def close(self) -> None:
        self._registry.close()


def open_benchmark(service: ScaleService, capability: HostBenchmarkCapability, *, custody_root: Path | None = None) -> dict[str, Any]:
    """Host-only single use, permanent reservation precedes plaintext access."""
    root, registry = service._root, service._registry
    with service.campaign_lock():
        service._identity()
        status = registry.status()
        if status["benchmark_state"] != "FROZEN_CLOSED":
            raise ScaleError("BENCHMARK_OPENING_DENIED")
        manifest = registry.manifest
        if inspect_seal(root) != manifest["sealed"]:
            raise ScaleError("SEALED_BINDING_DRIFT")
        custody = custody_root or Path.home() / ".quantforge" / "research_exposure"
        if custody.resolve().is_relative_to(root.resolve()) or ".autolab" in {x.casefold() for x in custody.parts}:
            raise ScaleError("INVALID_EXPOSURE_CUSTODY")
        custody.mkdir(parents=True, exist_ok=True)
        exposure = Journal(custody)
        try:
            with writer_lock(custody):
                key = manifest["sealed"]["binding"]["benchmark_key"]
                if any(e.get("benchmark_key") == key for e in exposure.history()):
                    raise ScaleError("BENCHMARK_ALREADY_EXPOSED_OTHER_PROGRAM")
                exposure.append("BENCHMARK_EXPOSURE_RESERVED", benchmark_key=key,
                                program_id=manifest["program_id"], manifest_id=status["manifest_id"])
                with writer_lock(root):
                    registry.journal.append("BENCHMARK_RESERVED", exposure_key=key)
        finally:
            exposure.close()
        try:
            payload = capability.decrypt(root, expected=manifest["sealed"])
            frames = synchronized_frames(decode_frames(payload["frames"]))
            research_n = payload["research_bars"]
            prefix = service._frames()
            if any(logical_content_hash(f.head(research_n)) != logical_content_hash(prefix[s]) for s, f in frames.items()):
                raise ScaleError("BENCHMARK_PREFIX_SUBSTITUTION")
            candidate = next(e for e in registry.history() if e["kind"] == "CANDIDATE_FROZEN")
            history_n = manifest["protocol"]["initial_history_bars"]
            tail = {s: f.slice(research_n - history_n) for s, f in frames.items()}
            registry.reserve_scan(sum(f.height for f in tail.values()) * manifest["scenario_scan_multiplier"], benchmark=True)
            result = evaluate(tail, candidate["proposal"], manifest["protocol"])
            report = {"status": "COMPLETED", "program_id": manifest["program_id"], "candidate": candidate["run_id"],
                      "manifest_id": status["manifest_id"], "evidence_id": candidate["evidence_id"],
                      "evaluation": result, "validated_alpha": False, "live_eligible": False,
                      "retuning_authorized": False, "benchmark_openings": 1}
            with writer_lock(root):
                publish(root / "benchmark-result.json", report)
                registry.journal.append("BENCHMARK_TERMINAL", status="COMPLETED", report_sha256=digest(report))
            return report
        except Exception:  # noqa: BLE001 -- first-use reservation remains spent on all failures
            with writer_lock(root):
                registry.journal.append("BENCHMARK_TERMINAL", status="FAILED", reason="SEALED_COMPUTATION_FAILED_NO_RETRY")
            raise ScaleError("SPENT_BENCHMARK_FAILED_INSPECT_HISTORY") from None


def inspect_benchmark(service: ScaleService) -> dict[str, Any]:
    if service._registry.status()["benchmark_state"] != "SPENT":
        raise ScaleError("BENCHMARK_FACTS_NOT_AUTHORIZED")
    event = next((e for e in service._registry.history() if e["kind"] == "BENCHMARK_TERMINAL"), None)
    if event is None or event["status"] != "COMPLETED":
        return {"program_id": service.status()["program_id"], "status": event["status"] if event else "UNKNOWN",
                "reason": event["reason"] if event else "SPENT_RESERVATION_WITHOUT_CONFIRMED_TERMINAL",
                "benchmark_state": "SPENT", "reopening_authorized": False,
                "real_transport": "LOCKED", "live_eligible": False}
    report = read_json(service._root / "benchmark-result.json")
    if event["report_sha256"] != digest(report):
        raise ScaleError("BENCHMARK_REPORT_DRIFT")
    return report


class ReplayRuntime:
    """Host paper replay, prefix-only, atomic cursor/state/fills event commits."""

    def __init__(self, service: ScaleService) -> None:
        self.service = service
        if service.status()["benchmark_state"] != "SPENT":
            raise ScaleError("REPLAY_REQUIRES_TERMINAL_BENCHMARK")
        if inspect_benchmark(service)["status"] != "COMPLETED":
            raise ScaleError("REPLAY_REQUIRES_COMPLETED_BENCHMARK")
        self._journal = Journal(local_root(service._root / "replay"))
        self._candidate = next(e for e in service._registry.history() if e["kind"] == "CANDIDATE_FROZEN")

    def status(self) -> dict[str, Any]:
        history = self._journal.history()
        steps = [e for e in history if e["kind"] == "STEP"]
        running = next((e["running"] for e in reversed(history) if e["kind"] == "RUN_STATE"), False)
        for index, event in enumerate(steps):
            if event["cursor"] != index + 1 or event["prefix_id"] != self.service._registry.manifest["prefix_id"]:
                raise ScaleError("REPLAY_BINDING_OR_CURSOR_DRIFT")
        return {"cursor": len(steps), "running": running,
                "state": steps[-1]["result"]["state"] if steps else {},
                "units": "hypothetical_normalized_research_units", "real_transport": "LOCKED"}

    def set_running(self, running: bool) -> dict[str, Any]:
        with writer_lock(self._journal.root):
            self.status()
            self._journal.append("RUN_STATE", running=running)
        return self.status()

    def step(self) -> dict[str, Any]:
        with writer_lock(self._journal.root):
            status = self.status()
            if not status["running"]:
                raise ScaleError("REPLAY_STOPPED")
            frames = self.service._frames()
            if status["cursor"] >= next(iter(frames.values())).height:
                raise ScaleError("REPLAY_EXHAUSTED")
            result = replay_step(frames, self._candidate["proposal"], status["cursor"], status["state"])
            self._journal.append("STEP", cursor=status["cursor"] + 1,
                                 prefix_id=self.service._registry.manifest["prefix_id"], result=result)
            return result

    def history(self) -> list[dict[str, Any]]:
        self.status()
        return self._journal.history()

    def close(self) -> None:
        self._journal.close()
