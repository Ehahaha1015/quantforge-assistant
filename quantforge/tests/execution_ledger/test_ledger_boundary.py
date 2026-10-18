from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest
from _ledger_support import (
    RECOVER_AT,
    advance_to_dispatching,
    make_provenance,
    make_store,
    make_target,
    recover_store,
)
from _support import make_observation

from quantforge.execution_ledger import ledger as ledger_module
from quantforge.execution_ledger.ledger import DurableExecutionLedger

_ALLOWED_METHODS = {
    "assess_observation",
    "record_observation",
    "read_attempt",
    "read_attempt_history",
    "read_observations",
    "close",
}

_FORBIDDEN_METHODS = {
    "submit",
    "send",
    "dispatch",
    "finalize",
    "retry",
    "resubmit",
    "cancel",
    "clear_unknown",
    "force_dispatch",
    "override_state",
    "auto_reconcile",
    "repair",
    "migrate",
    "rebind",
}

_MUTATION_CALL = re.compile(
    r"\.(prepare|claim|mark_dispatching|persist_transport_outcome|"
    r"persist_pre_dispatch_no_effect|apply_reconciliation|recover)\("
)


def _composed(tmp_path: Path):
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    store = make_store(tmp_path / "state")
    recover_store(store, target, observed_at=RECOVER_AT)
    advance_to_dispatching(store, provenance, target)
    ledger = DurableExecutionLedger(store, tmp_path / "ledger.sqlite3")
    return store, provenance, target, ledger


def test_public_surface_is_exactly_allowed() -> None:
    public = {
        name
        for name in dir(DurableExecutionLedger)
        if not name.startswith("_")
        and callable(getattr(DurableExecutionLedger, name))
    }
    assert public == _ALLOWED_METHODS
    assert public.isdisjoint(_FORBIDDEN_METHODS)


def test_source_avoids_mutations_and_capabilities() -> None:
    source = Path(ledger_module.__file__).read_text(encoding="utf-8")
    assert _MUTATION_CALL.search(source) is None
    for token in (
        "adapter",
        "sqlite3",
        "socket",
        "urllib",
        "subprocess",
        "os.environ",
        "getenv",
        "random",
        "uuid4",
        "datetime.now",
        "time.time",
    ):
        assert token not in source, token


def test_imports_stay_within_the_composition_boundary() -> None:
    source = Path(ledger_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 0:
                roots.add(node.module.split(".", 1)[0])
            else:
                relative.add(node.module)
    assert roots <= {"__future__", "json", "pathlib", "typing", "quantforge"}
    assert relative <= {
        "errors",
        "identity",
        "models",
        "projection",
        "reconciliation",
        "sidecar",
    }


def test_execstate_mutations_are_never_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _provenance, target, ledger = _composed(tmp_path)
    fired: list[str] = []

    def tripwire(name: str):
        def _raise(*_args: object, **_kwargs: object) -> object:
            fired.append(name)
            raise AssertionError(f"forbidden EXECSTATE mutation: {name}")

        return _raise

    for name in (
        "prepare",
        "claim",
        "mark_dispatching",
        "persist_transport_outcome",
        "persist_pre_dispatch_no_effect",
        "apply_reconciliation",
        "recover",
    ):
        monkeypatch.setattr(store, name, tripwire(name))
    record, _receipt = store.read(target.submission_id, target)
    observation = make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
    )
    ledger.assess_observation(observation)
    ledger.record_observation(observation)
    view = ledger.read_attempt(target.submission_id, target)
    ledger.read_attempt_history(target.submission_id, target)
    ledger.read_observations(view.attempt_id)
    assert fired == []
    ledger.close()
    store.close()


def test_no_hidden_persistence(tmp_path: Path) -> None:
    store, _provenance, target, ledger = _composed(tmp_path)
    sidecar_path = tmp_path / "ledger.sqlite3"
    execstate_path = tmp_path / "state" / "execstate.sqlite3"

    def snapshot(path: Path) -> tuple[list[str], int, int]:
        connection = sqlite3.connect(str(path), isolation_level=None)
        try:
            tables = sorted(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
                if str(row[0]) != "sqlite_sequence"
            )
            observations = (
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM observations"
                    ).fetchone()[0]
                )
                if "observations" in tables
                else 0
            )
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            return tables, observations, version
        finally:
            connection.close()

    before_sidecar = snapshot(sidecar_path)
    before_execstate = snapshot(execstate_path)
    record, _receipt = store.read(target.submission_id, target)
    observation = make_observation(
        submission_id=target.submission_id,
        target=target,
        generation=record.generation,
        recovery_epoch=record.recovery_epoch,
    )
    ledger.assess_observation(observation)
    ledger.read_attempt(target.submission_id, target)
    ledger.read_attempt_history(target.submission_id, target)
    assert snapshot(sidecar_path) == before_sidecar
    assert snapshot(execstate_path) == before_execstate
    ledger.record_observation(observation)
    after_sidecar = snapshot(sidecar_path)
    assert after_sidecar[0] == before_sidecar[0]
    assert after_sidecar[1] == before_sidecar[1] + 1
    assert snapshot(execstate_path) == before_execstate
    ledger.close()
    store.close()
