from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantforge.product.storage import writer_lock

from .storage import Journal, ScaleError, digest, publish, read_json


def identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 60
        or not value.isascii()
        or any(not (c.isalnum() or c in "_-") for c in value)
    ):
        raise ScaleError("INVALID_IDENTIFIER")
    return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ProgramRegistry:
    """Every allocation is counted before admission/computation, including failures."""

    def __init__(self, root: Path) -> None:
        self.journal = Journal(root)
        self.root = self.journal.root
        self.manifest = (
            read_json(root / "program.json")
            if (root / "program.json").exists()
            else {}
        )

    def create(self, manifest: dict[str, Any]) -> dict[str, Any]:
        if self.manifest:
            raise ScaleError("PROGRAM_ALREADY_DECLARED")
        identifier(manifest["program_id"])
        budgets = manifest["budgets"]
        required = {
            "trials",
            "model_calls",
            "wall_seconds",
            "data_rows_scanned",
            "benchmark_openings",
            "retries",
            "families",
        }
        if (
            set(budgets) != required
            or any(
                type(budgets[k]) is not int or budgets[k] <= 0
                for k in required - {"retries", "families"}
            )
            or budgets["retries"] != 0
            or budgets["benchmark_openings"] != 1
        ):
            raise ScaleError("INVALID_PRECOMMITTED_BUDGET")
        families = budgets["families"]
        if (
            not isinstance(families, dict)
            or not families
            or len(families) > 16
            or any(
                identifier(name) != name
                or type(count) is not int
                or not 1 <= count <= 32
                for name, count in families.items()
            )
            or sum(families.values()) != budgets["trials"]
        ):
            raise ScaleError("INVALID_FAMILY_BUDGET")
        with writer_lock(self.root):
            if self.journal.history():
                raise ScaleError("UNEXPLAINED_REGISTRY_HISTORY")
            publish(self.root / "program.json", manifest)
            self.journal.append(
                "PROGRAM_CREATED", manifest_id=digest(manifest), created_at=utc_now()
            )
            self.manifest = manifest
        return self.status()

    def history(self) -> list[dict[str, Any]]:
        if not self.manifest:
            raise ScaleError("PROGRAM_MISSING")
        manifest = read_json(self.root / "program.json")
        if manifest != self.manifest:
            raise ScaleError("PROGRAM_SUBSTITUTION")
        history = self.journal.history()
        if (
            not history
            or history[0]["kind"] != "PROGRAM_CREATED"
            or history[0]["manifest_id"] != digest(manifest)
        ):
            raise ScaleError("PROGRAM_BINDING_MISMATCH")
        return history

    def status(self) -> dict[str, Any]:
        history = self.history()
        trials = [e for e in history if e["kind"] == "TRIAL_ALLOCATED"]
        calls = [e for e in history if e["kind"] == "MODEL_RESERVED"]
        opened = any(e["kind"] == "BENCHMARK_RESERVED" for e in history)
        frozen = any(e["kind"] == "CANDIDATE_FROZEN" for e in history)
        paused = next(
            (e["paused"] for e in reversed(history) if e["kind"] == "PAUSE"), False
        )
        terminal = {
            e["allocation"]: e for e in history if e["kind"] == "TRIAL_TERMINAL"
        }
        return {
            "program_id": self.manifest["program_id"],
            "manifest_id": digest(self.manifest),
            "trials": len(trials),
            "model_calls": len(calls),
            "family_counts": {
                name: sum(e["family"] == name for e in trials)
                for name in self.manifest["budgets"]["families"]
            },
            "budget": self.manifest["budgets"],
            "data_rows_scanned": sum(
                e.get("rows", 0) for e in history if e["kind"] == "SCAN_RESERVED"
            ),
            "benchmark_state": "SPENT"
            if opened
            else "FROZEN_CLOSED"
            if frozen
            else "SEALED",
            "paused": paused,
            "pending_trials": [
                e["allocation"] for e in trials if e["allocation"] not in terminal
            ],
            "unresolved_model_calls": [
                e["call"]
                for e in calls
                if not any(
                    x["kind"] == "MODEL_TERMINAL" and x["call"] == e["call"]
                    for x in history
                )
            ],
            "real_transport": "LOCKED",
        }

    def ensure_search(self) -> None:
        status = self.status()
        if status["benchmark_state"] != "SEALED" or status["paused"]:
            raise ScaleError("SEARCH_CLOSED_OR_PAUSED")
        created = datetime.fromisoformat(self.history()[0]["created_at"])
        if (datetime.now(UTC) - created).total_seconds() > self.manifest["budgets"][
            "wall_seconds"
        ]:
            raise ScaleError("WALL_BUDGET_EXHAUSTED")

    def allocate(
        self,
        trial_id: str,
        family: str,
        proposal: dict[str, Any],
        *,
        origin: str,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        with writer_lock(self.root):
            self.ensure_search()
            history = self.history()
            allocated = [e for e in history if e["kind"] == "TRIAL_ALLOCATED"]
            if len(allocated) >= self.manifest["budgets"]["trials"]:
                raise ScaleError("TRIAL_BUDGET_EXHAUSTED")
            # Free-form inputs are never facts and never persisted before validation.
            allocation = len(allocated) + 1
            valid_keys = set(proposal) == {"short_window", "long_window"}
            safe_proposal = (
                proposal
                if valid_keys and all(type(v) is int for v in proposal.values())
                else {"invalid_schema": True}
            )
            safe_family = (
                family
                if family in self.manifest["budgets"]["families"]
                else "UNDECLARED"
            )
            invalid_id = False
            try:
                safe_id = identifier(trial_id)
            except ScaleError:
                safe_id = "invalid-" + str(allocation)
                invalid_id = True
            event = self.journal.append(
                "TRIAL_ALLOCATED",
                allocation=allocation,
                trial_id=safe_id,
                family=safe_family,
                proposal=safe_proposal,
                origin=origin if origin in ("AI", "MANUAL", "STRESS") else "INVALID",
                call_id=call_id,
            )
            reason = None
            if invalid_id:
                reason = "INVALID_IDENTIFIER"
            elif any(e["trial_id"] == safe_id for e in allocated):
                reason = "DUPLICATE_TRIAL_ID"
            elif safe_family == "UNDECLARED":
                reason = "FAMILY_NOT_PREDECLARED"
            elif (
                sum(e["family"] == family for e in allocated)
                >= self.manifest["budgets"]["families"][family]
            ):
                reason = "FAMILY_BUDGET_EXHAUSTED"
            elif (
                not valid_keys
                or not all(type(v) is int for v in proposal.values())
                or not 0 < proposal["short_window"] < proposal["long_window"] <= 50
                or origin not in ("AI", "MANUAL", "STRESS")
            ):
                reason = "PROPOSAL_OUTSIDE_ALLOWLIST"
            elif origin != "STRESS" and any(e["proposal"] == safe_proposal for e in allocated):
                reason = "DUPLICATE_PARAMETERS"
            if reason:
                self.journal.append(
                    "TRIAL_TERMINAL",
                    allocation=allocation,
                    status="REJECTED",
                    reason=reason,
                )
                return {**event, "admitted": False, "reason": reason}
            return {**event, "admitted": True}

    def terminal(self, allocation: int, status: str, **values: Any) -> None:
        if status not in (
            "COMPLETED",
            "FAILED",
            "CRASHED",
            "ABORTED",
            "REJECTED",
            "SUPERSEDED",
        ):
            raise ScaleError("INVALID_TERMINAL_STATUS")
        with writer_lock(self.root):
            history = self.history()
            if not any(
                e["kind"] == "TRIAL_ALLOCATED" and e["allocation"] == allocation
                for e in history
            ) or any(
                e["kind"] == "TRIAL_TERMINAL" and e["allocation"] == allocation
                for e in history
            ):
                raise ScaleError("TERMINAL_TRANSITION_DENIED")
            self.journal.append(
                "TRIAL_TERMINAL", allocation=allocation, status=status, **values
            )

    def allocate_bundle(self, trial_id: str, family: str, proposal: dict[str, Any], *,
                        origin: str, call_id: str | None = None) -> list[dict[str, Any]]:
        """One primary and four fixed stress slots, all-or-none durable count."""
        with writer_lock(self.root):
            self.ensure_search()
            allocated = [e for e in self.history() if e["kind"] == "TRIAL_ALLOCATED"]
            budgets = self.manifest["budgets"]
            if len(allocated) + 5 > budgets["trials"]:
                raise ScaleError("TRIAL_BUNDLE_BUDGET_EXHAUSTED")
            if family in budgets["families"] and sum(e["family"] == family for e in allocated) + 5 > budgets["families"][family]:
                raise ScaleError("FAMILY_BUNDLE_BUDGET_EXHAUSTED")
            primary_allocation = len(allocated) + 1
            typed = set(proposal) == {"short_window", "long_window"} and all(type(v) is int for v in proposal.values())
            pairs = sorted({(proposal["short_window"] - 1, proposal["long_window"]),
                            (proposal["short_window"] + 1, proposal["long_window"]),
                            (proposal["short_window"], proposal["long_window"] - 1),
                            (proposal["short_window"], proposal["long_window"] + 1)}) if typed else [(0, 0)] * 4
            declarations = [(trial_id, proposal, origin)] + [
                (f"probe-{primary_allocation}-{i}", {"short_window": pair[0], "long_window": pair[1]}, "STRESS")
                for i, pair in enumerate(pairs)
            ]
            items: list[tuple[str, dict[str, Any]]] = []
            answers = []
            primary_admitted = False
            for index, (raw_id, intent, source) in enumerate(declarations):
                allocation = primary_allocation + index
                reason = None
                try:
                    safe_id = identifier(raw_id)
                except ScaleError:
                    safe_id, reason = "invalid-" + str(allocation), "INVALID_IDENTIFIER"
                valid = set(intent) == {"short_window", "long_window"} and all(type(v) is int for v in intent.values())
                safe_intent = intent if valid else {"invalid_schema": True}
                safe_family = family if family in budgets["families"] else "UNDECLARED"
                event = {"allocation": allocation, "trial_id": safe_id, "family": safe_family,
                         "proposal": safe_intent, "origin": source if source in ("AI", "MANUAL", "STRESS") else "INVALID",
                         "call_id": call_id, "bundle": primary_allocation}
                if reason is None:
                    if any(e["trial_id"] == safe_id for e in allocated):
                        reason = "DUPLICATE_TRIAL_ID"
                    elif safe_family == "UNDECLARED":
                        reason = "FAMILY_NOT_PREDECLARED"
                    elif not valid or not 0 < intent["short_window"] < intent["long_window"] <= 50 or source not in ("AI", "MANUAL", "STRESS"):
                        reason = "PROPOSAL_OUTSIDE_ALLOWLIST"
                    elif index and not primary_admitted:
                        reason = "PARENT_ATTEMPT_REJECTED"
                    elif not index and any(e["proposal"] == safe_intent for e in allocated):
                        reason = "DUPLICATE_PARAMETERS"
                if not index:
                    primary_admitted = reason is None
                items.append(("TRIAL_ALLOCATED", event))
                if reason:
                    items.append(("TRIAL_TERMINAL", {"allocation": allocation, "status": "REJECTED", "reason": reason}))
                answers.append({**event, "admitted": reason is None, **({"reason": reason} if reason else {})})
                allocated.append(event)
            committed = self.journal.append_many(items)
            for answer in answers:
                receipt = next(e for e in committed if e["kind"] == "TRIAL_ALLOCATED" and e["allocation"] == answer["allocation"])
                answer.update({"seq": receipt["seq"], "event_hash": receipt["event_hash"], "kind": receipt["kind"]})
            return answers

    def recover(self) -> dict[str, Any]:
        with writer_lock(self.root):
            for allocation in self.status()["pending_trials"]:
                self.journal.append(
                    "TRIAL_TERMINAL",
                    allocation=allocation,
                    status="CRASHED",
                    reason="INTERRUPTED_AFTER_COUNTED_ALLOCATION_NO_REDISPATCH",
                )
            for call in self.status()["unresolved_model_calls"]:
                self.journal.append(
                    "MODEL_TERMINAL",
                    call=call,
                    status="UNKNOWN",
                    reason="INTERRUPTED_MODEL_CALL_NO_RETRY",
                )
        return self.status()

    def reserve_model(self, *, family: str | None = None) -> str:
        with writer_lock(self.root):
            self.ensure_search()
            status = self.status()
            if status["unresolved_model_calls"]:
                raise ScaleError("AMBIGUOUS_MODEL_CALL_REQUIRES_RECOVERY")
            if status["model_calls"] >= self.manifest["budgets"]["model_calls"]:
                raise ScaleError("MODEL_BUDGET_EXHAUSTED")
            call = f"model-{status['model_calls'] + 1}"
            if family is not None and family not in self.manifest["budgets"]["families"]:
                raise ScaleError("FAMILY_NOT_PREDECLARED")
            self.journal.append("MODEL_RESERVED", call=call, family=family)
            return call

    def finish_model(
        self, call: str, *, status: str, ledger_id: str | None = None
    ) -> None:
        with writer_lock(self.root):
            if call not in self.status()["unresolved_model_calls"] or status not in (
                "RESPONSE",
                "ERROR",
            ):
                raise ScaleError("MODEL_TRANSITION_DENIED")
            self.journal.append(
                "MODEL_TERMINAL", call=call, status=status, ledger_id=ledger_id
            )

    def reserve_scan(self, rows: int, *, benchmark: bool = False) -> None:
        with writer_lock(self.root):
            if not benchmark:
                self.ensure_search()
            if (
                type(rows) is not int
                or rows <= 0
                or self.status()["data_rows_scanned"] + rows
                > self.manifest["budgets"]["data_rows_scanned"]
            ):
                raise ScaleError("DATA_SCAN_BUDGET_EXHAUSTED")
            self.journal.append("SCAN_RESERVED", rows=rows, benchmark=benchmark)

    def pause(self, paused: bool) -> dict[str, Any]:
        with writer_lock(self.root):
            if self.status()["benchmark_state"] != "SEALED":
                raise ScaleError("SEARCH_TERMINAL")
            self.journal.append("PAUSE", paused=bool(paused))
        return self.status()

    def close(self) -> None:
        self.journal.close()
