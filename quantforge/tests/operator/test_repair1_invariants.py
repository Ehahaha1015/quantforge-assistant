"""Mandatory repair #1 oracles for OPERATOR-001.

These cover the durable state machine (DT-1), the post-open integrity checks
(CH-4), the verifier trust boundary (CH-5), the verifier failure-family
preservation (CH-7) and the missing section-30 oracle families (CH-6).
"""

from __future__ import annotations

import importlib
import os
import runpy
import sqlite3
import subprocess
import sys
import sysconfig
import textwrap
from datetime import timedelta
from pathlib import Path

import pytest

from quantforge.frontier import (
    ExecutionTargetBinding,
    FrontierReason,
    evaluate_frontier_eligibility,
)
from quantforge.operator import (
    ControlAction,
    ControlState,
    OperatorArgumentError,
    OperatorAuthenticationError,
    OperatorBusyError,
    OperatorClosedError,
    OperatorCommitUnknownError,
    OperatorCorruptionError,
    OperatorNotFoundError,
    OperatorSchemaError,
    OperatorSequenceExhaustedError,
    SQLiteOperatorControlStore,
    VerifiedOperatorIdentity,
    operator_evidence_for_frontier,
)
from quantforge.operator.store import _CONTROL_SEQUENCE_MAX

from ._helpers import (
    COMMITTED_AT,
    AcceptingVerifier,
    make_policy,
    make_proof,
    make_request,
    make_scope,
    make_target,
    store_path,
)

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
_SENTINEL_A = "__OPCTL_SENTINEL_TESTVALUE__"
_SENTINEL_B = "_OPCTL-FIXTURE-SECRET-0123456789abcdef0123456789abcdef"


def _child_env() -> dict[str, str]:
    options = sysconfig.get_path("stdlib")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(_SRC), str(_ROOT), str(options)])
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _run_child(source: str, *args: object) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source), *map(str, args)],
        capture_output=True,
        text=True,
        env=_child_env(),
        cwd=_ROOT,
        check=False,
    )
    return completed


def _open(tmp_path, name: str = "operator.sqlite") -> tuple[Path, SQLiteOperatorControlStore]:
    path = store_path(tmp_path) if name == "operator.sqlite" else tmp_path / name
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    return path, store


def _enable(store: SQLiteOperatorControlStore) -> None:
    request = make_request()
    store.request_control_change(
        request, make_proof(request), committed_at=COMMITTED_AT
    )


def _disable(store: SQLiteOperatorControlStore) -> None:
    request = make_request(action=ControlAction.DISABLE)
    store.request_control_change(
        request, make_proof(request), committed_at=COMMITTED_AT
    )


# DT-1 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "state"),
    [
        (ControlAction.ENABLE, ControlState.ENABLED),
        (ControlAction.DISABLE, ControlState.DISABLED),
    ],
)
def test_same_action_new_request_is_zero_write_replay(
    tmp_path, action: ControlAction, state: ControlState
) -> None:
    _unused_path, store = _open(tmp_path)
    first = make_request(request_id="same-action-1", action=action)
    store.request_control_change(
        first, make_proof(first, proof_id="proof-1"), committed_at=COMMITTED_AT
    )
    before_state = store.read_control_state(make_scope())
    before_history = store.read_control_history()
    assert before_state is not None

    second = make_request(request_id="same-action-2", action=action)
    outcome = store.request_control_change(
        second,
        make_proof(second, proof_id="proof-2"),
        committed_at=COMMITTED_AT + timedelta(seconds=1),
    )
    after_state = store.read_control_state(make_scope())
    after_history = store.read_control_history()
    assert outcome.replay is True
    assert outcome.current_effective_state is state
    assert outcome.original_resulting_state is state
    assert outcome.original_control_sequence == 1
    assert outcome.original_state_version_id == before_state.state_version_id
    assert outcome.original_audit_id == before_history[0].audit_id
    assert len(after_history) == 1
    assert after_state == before_state
    assert after_history == before_history
    store.close()


@pytest.mark.parametrize(
    ("first_action", "expected"),
    [
        (ControlAction.ENABLE, ControlState.DISABLED),
        (ControlAction.DISABLE, ControlState.ENABLED),
    ],
)
def test_opposing_action_is_a_real_transition(
    tmp_path, first_action: ControlAction, expected: ControlState
) -> None:
    _unused_path, store = _open(tmp_path)
    first = make_request(request_id="opposing-1", action=first_action)
    store.request_control_change(
        first, make_proof(first, proof_id="proof-1"), committed_at=COMMITTED_AT
    )
    opposing = ControlAction.DISABLE if first_action is ControlAction.ENABLE else ControlAction.ENABLE
    second = make_request(request_id="opposing-2", action=opposing)
    outcome = store.request_control_change(
        second, make_proof(second, proof_id="proof-2"), committed_at=COMMITTED_AT
    )
    assert outcome.replay is False
    assert outcome.current_effective_state is expected
    assert len(store.read_control_history()) == 2
    store.close()


# CH-4 ---------------------------------------------------------------------


def _external(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.isolation_level = None
    return connection


def test_post_open_trigger_drop_is_detected(tmp_path) -> None:
    path, store = _open(tmp_path)
    _enable(store)
    connection = _external(path)
    try:
        connection.execute("DROP TRIGGER control_audit_no_update")
    finally:
        connection.close()
    with pytest.raises(OperatorSchemaError):
        store.read_control_state(make_scope())
    store.close()


def test_post_open_trigger_body_change_is_detected(tmp_path) -> None:
    path, store = _open(tmp_path)
    _enable(store)
    connection = _external(path)
    try:
        connection.execute("DROP TRIGGER control_audit_no_delete")
        connection.execute(
            "CREATE TRIGGER control_audit_no_delete BEFORE DELETE ON control_audit "
            "BEGIN SELECT RAISE(ABORT, 'tampered'); END"
        )
    finally:
        connection.close()
    with pytest.raises(OperatorSchemaError):
        store.read_control_history()
    store.close()


def test_post_open_extra_object_is_detected(tmp_path) -> None:
    path, store = _open(tmp_path)
    _enable(store)
    connection = _external(path)
    try:
        connection.execute("CREATE TABLE extra(a)")
    finally:
        connection.close()
    with pytest.raises(OperatorSchemaError):
        store.read_control_state(make_scope())
    store.close()


def test_post_open_metadata_tamper_is_detected(tmp_path) -> None:
    path, store = _open(tmp_path)
    _enable(store)
    connection = _external(path)
    try:
        connection.execute("UPDATE metadata SET value = 'changed' WHERE key = 'schema_name'")
    finally:
        connection.close()
    with pytest.raises(OperatorSchemaError):
        store.produce_operator_evidence(make_target())
    store.close()


def test_post_open_projection_tamper_is_detected(tmp_path) -> None:
    path, store = _open(tmp_path)
    _enable(store)
    connection = _external(path)
    try:
        connection.execute("UPDATE control_state SET state = 'DISABLED'")
    finally:
        connection.close()
    with pytest.raises(OperatorCorruptionError):
        store.read_control_state(make_scope())
    store.close()


# CH-5 / CH-7 --------------------------------------------------------------


class _HostileVerifier:
    def __init__(self, verifier_id: str, *, failure: BaseException | None = None) -> None:
        self._verifier_id = verifier_id
        self._failure = failure
        self.calls = 0

    @property
    def verifier_id(self) -> str:
        return self._verifier_id

    def verify(self, request, proof):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        return VerifiedOperatorIdentity.model_construct(
            schema="operator-verified-v1",
            operator_reference=request.operator_reference,
            control_source=request.control_source,
            verifier_id=self._verifier_id,
            verified_at=request.observed_at + timedelta(seconds=1),
            proof_id=proof.proof_id,
        )


def test_invalid_verifier_id_is_rejected_before_write(tmp_path) -> None:
    path = store_path(tmp_path)
    verifier = _HostileVerifier("__OPCTL_SENTINEL_VERIFIERID__")
    store = SQLiteOperatorControlStore(path, policy=make_policy(), verifier=verifier)
    request = make_request()
    proof = make_proof(request)
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert verifier.calls == 1
    assert store.read_control_history() == ()
    assert b"__OPCTL_SENTINEL_VERIFIERID__" not in path.read_bytes()
    store.close()


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("verifier exploded"), KeyboardInterrupt()],
)
def test_verifier_failure_family_is_preserved(tmp_path, failure: BaseException) -> None:
    path = store_path(tmp_path)
    verifier = _HostileVerifier("operator-test-verifier-v1", failure=failure)
    store = SQLiteOperatorControlStore(path, policy=make_policy(), verifier=verifier)
    request = make_request()
    proof = make_proof(request)
    with pytest.raises(type(failure)) as info:
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert info.value is failure
    assert store.read_control_history() == ()
    store.close()


def test_authentication_denial_uses_authentication_error(tmp_path) -> None:
    path = store_path(tmp_path)
    verifier = _HostileVerifier(
        "operator-test-verifier-v1",
        failure=OperatorAuthenticationError("denied"),
    )
    store = SQLiteOperatorControlStore(path, policy=make_policy(), verifier=verifier)
    request = make_request()
    proof = make_proof(request)
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    store.close()


# CH-3 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("verified_at", "not-an-instant"),
        ("observed_at", "2030-01-01T12:00:00Z"),
        ("committed_at", ""),
        ("operator_reference", "bad value"),
        ("verifier_id", "bad value"),
        ("proof_id", "bad value"),
        ("scope_key", "opctl-scope-v1-00000000000000000000000000000000"),
        ("action_id", "opctl-action-v1-00000000000000000000000000000000"),
        ("state_version_id", "opctl-state-v1-00000000000000000000000000000000"),
        ("audit_id", "opctl-audit-v1-00000000000000000000000000000000"),
        ("maximum_age_microseconds", float("inf")),
        (
            "request_commitment",
            "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        ),
    ],
)
def test_stored_audit_corruption_is_corruption_error(
    tmp_path, column: str, value: object
) -> None:
    path, store = _open(tmp_path)
    _enable(store)
    store.close()
    connection = _external(path)
    try:
        connection.execute("DROP TRIGGER control_audit_no_update")
        connection.execute(
            f"UPDATE control_audit SET {column} = ?",
            (value,),
        )
        connection.execute(
            "CREATE TRIGGER control_audit_no_update BEFORE UPDATE ON control_audit "
            "BEGIN SELECT RAISE(ABORT, 'control_audit is append-only'); END"
        )
    finally:
        connection.close()
    with pytest.raises(OperatorCorruptionError):
        SQLiteOperatorControlStore(path, policy=make_policy())


def test_caller_argument_errors_are_not_remapped() -> None:
    with pytest.raises(OperatorArgumentError):
        make_policy(timedelta(hours=2))


# CH-6 ---------------------------------------------------------------------


def test_two_process_write_race_has_one_winner(tmp_path) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    store.close()
    child = """
        import sys, time
        from tests.operator._helpers import (
            AcceptingVerifier, make_policy,
        )
        from quantforge.operator import SQLiteOperatorControlStore

        path = sys.argv[1]
        store = SQLiteOperatorControlStore(
            path, policy=make_policy(), verifier=AcceptingVerifier()
        )
        store._connection.execute("BEGIN IMMEDIATE")  # type: ignore[union-attr]
        print("HOLDER-LOCKED", flush=True)
        time.sleep(3.0)
        store._connection.execute("ROLLBACK")  # type: ignore[union-attr]
        store.close()
        print("HOLDER-DONE")
    """
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(child), str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=_child_env(),
        cwd=_ROOT,
    )
    assert holder.stdout is not None
    signal = holder.stdout.readline().strip()
    assert signal == "HOLDER-LOCKED", signal
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    request = make_request(request_id="race-challenger")
    with pytest.raises(OperatorBusyError):
        store.request_control_change(
            request,
            make_proof(request, proof_id="proof-challenger"),
            committed_at=COMMITTED_AT,
        )
    store.close()
    stdout, _stderr = holder.communicate(timeout=120)
    assert holder.returncode == 0
    assert stdout.strip() == "HOLDER-DONE"
    reopened = SQLiteOperatorControlStore(path, policy=make_policy())
    assert reopened.read_control_history() == ()
    reopened.close()


def test_fresh_process_crash_before_commit_leaves_no_change(tmp_path) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(path, policy=make_policy(), verifier=AcceptingVerifier())
    store.close()
    child = """
        import sys
        from tests.operator._helpers import (
            COMMITTED_AT, AcceptingVerifier, make_policy, make_proof, make_request,
        )
        from quantforge.operator import SQLiteOperatorControlStore

        path = sys.argv[1]
        store = SQLiteOperatorControlStore(
            path, policy=make_policy(), verifier=AcceptingVerifier()
        )
        store._before_commit = lambda: (_ for _ in ()).throw(RuntimeError("crash"))
        request = make_request(request_id="crash-1")
        try:
            store.request_control_change(
                request, make_proof(request), committed_at=COMMITTED_AT
            )
        except RuntimeError:
            pass
        store.close()
    """
    completed = _run_child(child, path)
    assert completed.returncode == 0, completed.stderr
    reopened = SQLiteOperatorControlStore(path, policy=make_policy())
    assert reopened.read_control_history() == ()
    assert reopened.read_control_state(make_scope()) is None
    reopened.close()


def test_fresh_process_crash_after_commit_is_visible(tmp_path) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(path, policy=make_policy(), verifier=AcceptingVerifier())
    store.close()
    child = """
        import sys
        from tests.operator._helpers import (
            COMMITTED_AT, AcceptingVerifier, make_policy, make_proof, make_request,
        )
        from quantforge.operator import SQLiteOperatorControlStore

        path = sys.argv[1]
        store = SQLiteOperatorControlStore(
            path, policy=make_policy(), verifier=AcceptingVerifier()
        )
        real_commit = store._commit_connection
        def commit_then_fail() -> None:
            real_commit()
            raise RuntimeError("crash after commit")
        store._commit_connection = commit_then_fail
        request = make_request(request_id="crash-2")
        try:
            store.request_control_change(
                request, make_proof(request), committed_at=COMMITTED_AT
            )
        except SystemExit:
            raise
        except BaseException:
            pass
        store.close()
    """
    completed = _run_child(child, path)
    assert completed.returncode == 0, completed.stderr
    reopened = SQLiteOperatorControlStore(path, policy=make_policy())
    history = reopened.read_control_history()
    assert len(history) == 1
    assert history[0].request_id == "crash-2"
    reopened.close()


def test_identity_determinism_across_pythonhashseed(tmp_path) -> None:
    child = """
        import sys
        from tests.operator._helpers import (
            COMMITTED_AT, AcceptingVerifier, make_policy, make_proof, make_request,
        )
        from quantforge.operator import SQLiteOperatorControlStore

        path = sys.argv[1]
        store = SQLiteOperatorControlStore(
            path, policy=make_policy(), verifier=AcceptingVerifier()
        )
        request = make_request(request_id="hash-1")
        outcome = store.request_control_change(
            request, make_proof(request), committed_at=COMMITTED_AT
        )
        history = store.read_control_history()
        print(outcome.action_id)
        print(history[0].audit_id)
        print(history[0].state_version_id)
        store.close()
    """
    outputs = []
    for index, seed in enumerate(("0", "1", "524287")):
        path = tmp_path / f"hash-{index}.sqlite"
        parent = SQLiteOperatorControlStore(
            path, policy=make_policy(), verifier=AcceptingVerifier()
        )
        parent.close()
        env = _child_env()
        env["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(child), str(path)],
            capture_output=True,
            text=True,
            env=env,
            cwd=_ROOT,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(completed.stdout)
    assert len(set(outputs)) == 1, outputs


def _load_frontier_template():
    # The frozen frontier fixture is test-only code; loading it by spec keeps
    # the operator package free of any frontier-test dependency.
    return runpy.run_path(str(_ROOT / "tests/frontier/_frontier_fixtures.py"))


def test_frontier_integration_matrix_six_rows(tmp_path) -> None:
    fixture = _load_frontier_template()
    complete_inputs = fixture["complete_inputs"]
    assert callable(complete_inputs)
    values = complete_inputs()
    target: ExecutionTargetBinding = values["target"]

    # row 1: no committed state -> OPERATOR_EVIDENCE_MISSING
    decision = evaluate_frontier_eligibility(**{**values, "operator_evidence": None})
    assert FrontierReason.OPERATOR_EVIDENCE_MISSING in decision.reasons

    # row 2: committed DISABLED -> OPERATOR_DISABLED
    values["operator_evidence"] = values["operator_evidence"].model_copy(
        update={"enabled": False, "evidence_id": "disabled-evidence"}
    )
    decision = evaluate_frontier_eligibility(**values)
    assert FrontierReason.OPERATOR_DISABLED in decision.reasons

    # row 3: committed ENABLED and fresh -> operator condition satisfied
    values["operator_evidence"] = values["operator_evidence"].model_copy(
        update={"enabled": True, "evidence_id": "fresh-evidence"}
    )
    decision = evaluate_frontier_eligibility(**values)
    assert FrontierReason.OPERATOR_DISABLED not in decision.reasons
    assert FrontierReason.OPERATOR_EVIDENCE_STALE not in decision.reasons
    assert FrontierReason.OPERATOR_EVIDENCE_MISSING not in decision.reasons

    # row 4: ENABLED but aged past maximum_age -> OPERATOR_EVIDENCE_STALE
    values["operator_evidence"] = values["operator_evidence"].model_copy(
        update={
            "observed_at": values["dispatch_timestamp"] - timedelta(seconds=5),
            "evidence_id": "stale-evidence",
        }
    )
    decision = evaluate_frontier_eligibility(**values)
    assert FrontierReason.OPERATOR_EVIDENCE_STALE in decision.reasons

    # row 5: future-stamped ENABLED -> OPERATOR_EVIDENCE_STALE
    values["operator_evidence"] = values["operator_evidence"].model_copy(
        update={
            "observed_at": values["dispatch_timestamp"] + timedelta(seconds=5),
            "evidence_id": "future-evidence",
        }
    )
    decision = evaluate_frontier_eligibility(**values)
    assert FrontierReason.OPERATOR_EVIDENCE_STALE in decision.reasons

    # row 6: evidence bound to another target -> ENVIRONMENT_ACCOUNT_MISMATCH
    other_target = target.model_copy(update={"submission_id": "other-submission"})
    values["operator_evidence"] = values["operator_evidence"].model_copy(
        update={
            "target": other_target,
            "observed_at": values["dispatch_timestamp"],
            "evidence_id": "mismatch-evidence",
        }
    )
    decision = evaluate_frontier_eligibility(**values)
    assert FrontierReason.ENVIRONMENT_ACCOUNT_MISMATCH in decision.reasons


def test_operator_evidence_alone_does_not_dispatch(tmp_path) -> None:
    _unused_path, store = _open(tmp_path)
    _enable(store)
    evidence = operator_evidence_for_frontier(store, make_target())
    assert evidence is not None and evidence.enabled is True
    # OPERATOR exposes no dispatch surface at all.
    assert not hasattr(store, "prepare")
    assert not hasattr(store, "claim")
    assert not hasattr(store, "mark_dispatching")
    assert not hasattr(store, "persist_transport_outcome")
    store.close()


def test_operator_package_does_not_import_execstate_or_live() -> None:
    for module_name in (
        "quantforge.operator",
        "quantforge.operator.store",
        "quantforge.operator.models",
    ):
        module = importlib.import_module(module_name)
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        for forbidden in (
            "quantforge.execstate",
            "quantforge.execution_ledger",
            "quantforge.live",
            "quantforge.broker",
        ):
            assert forbidden not in source, (module_name, forbidden)


def test_sentinel_sweep_across_rejected_channels(tmp_path, caplog) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )
    for sentinel in (_SENTINEL_A, _SENTINEL_B):
        with caplog.at_level("DEBUG"):
            with pytest.raises(OperatorArgumentError):
                make_request(request_id=sentinel)
            with pytest.raises(OperatorArgumentError):
                make_request(scope=make_scope(broker=sentinel))
            with pytest.raises(OperatorArgumentError):
                make_request(scope=make_scope(account_reference=sentinel))
            with pytest.raises(OperatorArgumentError):
                make_request(scope=make_scope(adapter_id=sentinel))
            with pytest.raises(OperatorArgumentError):
                make_request(scope=make_scope(adapter_version=sentinel))
            with pytest.raises(OperatorArgumentError):
                make_request(operator_reference=sentinel)
            with pytest.raises(OperatorArgumentError):
                make_request(control_source=sentinel)
            with pytest.raises(OperatorAuthenticationError):
                make_proof(make_request(), proof_id=sentinel)
    assert store.read_control_history() == ()
    store.close()
    payloads = [path.read_bytes()]
    for suffix in ("-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            payloads.append(candidate.read_bytes())
    for sentinel in (_SENTINEL_A, _SENTINEL_B):
        needle = sentinel.encode("utf-8")
        assert all(needle not in payload for payload in payloads)
        assert needle not in caplog.text.encode("utf-8")


def test_sentinel_families_fail_opaque_grammar() -> None:
    import re

    pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    for sentinel in (_SENTINEL_A, _SENTINEL_B):
        assert pattern.fullmatch(sentinel) is None


def test_commit_unknown_poisons_every_surface(tmp_path) -> None:
    path, store = _open(tmp_path)
    request = make_request(request_id="unknown-1")

    def fail_commit() -> None:
        raise RuntimeError("injected commit failure")

    store._commit_connection = fail_commit
    with pytest.raises(OperatorCommitUnknownError):
        store.request_control_change(
            request, make_proof(request), committed_at=COMMITTED_AT
        )
    with pytest.raises(OperatorCommitUnknownError):
        store.read_control_state(make_scope())
    with pytest.raises(OperatorCommitUnknownError):
        store.read_control_history()
    with pytest.raises(OperatorCommitUnknownError):
        store.produce_operator_evidence(make_target())
    store.close()
    reopened = SQLiteOperatorControlStore(path, policy=make_policy())
    assert reopened.read_control_history() == ()
    reopened.close()


def test_consumer_error_mapping(tmp_path) -> None:
    path, store = _open(tmp_path)
    # OperatorNotFoundError -> the helper maps it to None.
    with pytest.raises(OperatorNotFoundError):
        store.produce_operator_evidence(make_target())
    assert operator_evidence_for_frontier(store, make_target()) is None

    # Corruption is never collapsed into a normal missing/disabled denial.
    _enable(store)
    connection = _external(path)
    try:
        connection.execute("UPDATE control_state SET state = 'DISABLED'")
    finally:
        connection.close()
    with pytest.raises(OperatorCorruptionError):
        operator_evidence_for_frontier(store, make_target())
    store.close()

    # A closed store raises rather than fabricating a denial.
    with pytest.raises(OperatorClosedError):
        operator_evidence_for_frontier(store, make_target())


def test_sequence_exhaustion_before_write(tmp_path) -> None:
    _unused_path, store = _open(tmp_path)
    maximum = _CONTROL_SEQUENCE_MAX
    store._connection.execute(  # type: ignore[union-attr]
        "INSERT INTO control_audit("
        "control_sequence, audit_id, request_id, action_id, scope_key, broker, "
        "environment, account_reference, adapter_id, adapter_version, "
        "requested_action, resulting_state, state_version_id, operator_reference, "
        "control_source, verifier_id, proof_id, verified_at, observed_at, "
        "maximum_age_microseconds, committed_at, request_commitment"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            maximum,
            "opctl-audit-v1-00000000000000000000000000000000",
            "exhaustion-seed",
            "opctl-action-v1-00000000000000000000000000000000",
            "opctl-scope-v1-00000000000000000000000000000000",
            "broker-x",
            "SANDBOX",
            "acct-7",
            "adapter-x",
            "1",
            "ENABLE",
            "ENABLED",
            "opctl-state-v1-00000000000000000000000000000000",
            "operator-1",
            "control-source-1",
            "operator-test-verifier-v1",
            "proof-exhaustion-seed",
            "2030-01-01T12:00:01.000000Z",
            "2030-01-01T12:00:00.000000Z",
            60_000_000,
            "2030-01-01T12:00:02.000000Z",
            "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        ),
    )
    store._verify_all = lambda **kwargs: None  # type: ignore[method-assign]
    request = make_request(request_id="exhausted-1", action=ControlAction.DISABLE)
    with pytest.raises(OperatorSequenceExhaustedError):
        store.request_control_change(
            request,
            make_proof(request, proof_id="proof-exhausted"),
            committed_at=COMMITTED_AT,
        )
    remaining = store._connection.execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) FROM control_audit"
    ).fetchone()[0]
    assert remaining == 1
    store.close()


def test_fresh_process_crash_windows_before_commit(tmp_path) -> None:
    for name in ("after-begin", "after-verify", "after-sequence", "before-commit"):
        path = tmp_path / f"crash-{name}.sqlite"
        seed = SQLiteOperatorControlStore(
            path, policy=make_policy(), verifier=AcceptingVerifier()
        )
        seed.close()
        child = """
        import sys
        from tests.operator._helpers import (
            COMMITTED_AT, AcceptingVerifier, make_policy, make_proof, make_request,
        )
        from quantforge.operator import SQLiteOperatorControlStore

        path = sys.argv[1]
        store = SQLiteOperatorControlStore(
            path, policy=make_policy(), verifier=AcceptingVerifier()
        )
        request = make_request(request_id="crash-window")
        proof = make_proof(request, proof_id="proof-crash-window")
        store._begin_immediate()
        try:
            store._verify_request(request, proof)
            store._next_sequence()
            raise RuntimeError("injected crash")
        except RuntimeError:
            store._rollback_if_open()
        store.close()
        """
        completed = _run_child(child, path)
        assert completed.returncode == 0, (name, completed.stderr)
        reopened = SQLiteOperatorControlStore(path, policy=make_policy())
        assert reopened.read_control_history() == (), name
        assert reopened.read_control_state(make_scope()) is None, name
        reopened.close()


def test_verification_points_detect_post_open_schema_changes(tmp_path) -> None:
    path, store = _open(tmp_path)
    _enable(store)

    # V4: an extra object is rejected.
    connection = _external(path)
    try:
        connection.execute("CREATE INDEX extra_index ON control_state(scope_key)")
    finally:
        connection.close()
    with pytest.raises(OperatorSchemaError):
        store.read_control_state(make_scope())
    store.close()

    # V6/V7: integrity and foreign-key checks are exercised on a healthy store.
    _unused_path, store2 = _open(tmp_path, "healthy.sqlite")
    _enable(store2)
    assert store2._require_connection().execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store2._require_connection().execute("PRAGMA foreign_key_check").fetchall() == []
    store2.close()
