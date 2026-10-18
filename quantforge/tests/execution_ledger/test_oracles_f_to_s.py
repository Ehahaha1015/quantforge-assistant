from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from _ledger_support import (
    CLAIM_AT,
    CLAIM_EXPIRES_AT,
    LATE_AT,
    PREPARE_AT,
    RECOVER_AT,
    RECOVER_RESCAN_AT,
    advance_to_dispatching,
    build_eligibility,
    make_provenance,
    make_store,
    make_target,
    prepare_store,
    recover_store,
)
from _support import (
    OBSERVED_AT,
    make_observation,
)
from _support import (
    make_target as make_synthetic_target,
)

from quantforge.execstate import (
    ExecutionStateCorruptionError,
    SQLiteExecutionStateStore,
)
from quantforge.execution_ledger import (
    AttemptPhase,
    BrokerAdapterNotImplementedError,
    BrokerObservationStatus,
    ExecutionLedgerCorruptionError,
    ExecutionLedgerDeniedError,
    ExecutionLedgerPathError,
    ExecutionLedgerSchemaError,
    ReconciliationConflict,
    ReconciliationReason,
)
from quantforge.execution_ledger import adapter as adapter_module
from quantforge.execution_ledger import ledger as ledger_module
from quantforge.execution_ledger.adapter import (
    BrokerAdapter,
    LockedBrokerAdapter,
)
from quantforge.execution_ledger.identity import (
    derive_attempt_id,
    verify_attempt_binding,
)
from quantforge.execution_ledger.ledger import DurableExecutionLedger
from quantforge.execution_ledger.models import (
    observation_envelope_json,
    observation_from_envelope_json,
    observation_semantic_key,
)
from quantforge.execution_ledger.projection import (
    project_attempt,
)
from quantforge.execution_ledger.reconciliation import assess
from quantforge.execution_ledger.sidecar import SQLiteObservationStore
from quantforge.frontier import (
    ExecutionTargetBinding,
    evaluate_frontier_eligibility,
)

_READER_WORKER = Path(__file__).with_name("_oracle_reader_worker.py")
_CRASH_WINDOW_WORKER = Path(__file__).with_name(
    "_oracle_crash_window_worker.py"
)
_CRASH_WORKER = Path(__file__).with_name("_oracle_crash_worker.py")

_TRIGGER_UPDATE = (
    "CREATE TRIGGER observations_no_update BEFORE UPDATE ON observations "
    "BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END"
)


def _composed(tmp_path: Path, *, stage: str = "dispatching"):
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    state_dir = tmp_path / "state"
    store = make_store(state_dir)
    recover_store(store, target, observed_at=RECOVER_AT)
    if stage == "prepared":
        prepare_store(store, provenance, target)
    elif stage == "dispatching":
        advance_to_dispatching(store, provenance, target)
    else:
        raise ValueError(stage)
    execstate_path = state_dir / "execstate.sqlite3"
    sidecar_path = tmp_path / "ledger.sqlite3"
    ledger = DurableExecutionLedger(store, sidecar_path)
    return (
        store,
        provenance,
        target,
        ledger,
        execstate_path,
        sidecar_path,
    )


def _observation(store, target, **overrides):
    record, _receipt = store.read(target.submission_id, target)
    values = {
        "submission_id": target.submission_id,
        "target": target,
        "generation": record.generation,
        "recovery_epoch": record.recovery_epoch,
        "observed_at": OBSERVED_AT,
        "broker_reference": "broker-ref-1",
    }
    values.update(overrides)
    return make_observation(**values)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _db_snapshot(path: Path) -> dict[str, object]:
    connection = _connect(path)
    try:
        tables = sorted(
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if str(row["name"]) != "sqlite_sequence"
        )
        rows = {
            table: [
                tuple(row)
                for row in connection.execute(
                    f"SELECT * FROM {table}"
                ).fetchall()
            ]
            for table in tables
        }
        objects = sorted(
            (
                str(row["type"]),
                str(row["name"]),
                str(row["sql"]) if row["sql"] is not None else "",
            )
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master"
            ).fetchall()
        )
        version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
    finally:
        connection.close()
    return {
        "tables": tables,
        "rows": rows,
        "objects": objects,
        "user_version": version,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _sidecar_count(path: Path) -> int:
    if not path.exists():
        return 0
    connection = _connect(path)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _sidecar_kinds(path: Path) -> list[str]:
    connection = _connect(path)
    try:
        return [
            str(row["observation_kind"])
            for row in connection.execute(
                "SELECT observation_kind FROM observations "
                "ORDER BY observation_sequence"
            )
        ]
    finally:
        connection.close()


def _independent_attempt_id(
    submission_id: str,
    target_json: str,
    generation: int,
    recovery_epoch: int,
) -> str:
    payload = {
        "submission_id": submission_id,
        "target_json": target_json,
        "generation": generation,
        "recovery_epoch": recovery_epoch,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "execstate-attempt-v1-" + hashlib.sha256(canonical).hexdigest()[
        :32
    ]


def _evaluate_with_record(
    provenance,
    target,
    record,
    receipt,
    recovery_evidence,
    claim,
    decision,
    *,
    durable_record,
):
    return evaluate_frontier_eligibility(
        provenance,
        decision.dispatch_timestamp,
        target,
        price_evidence=decision.price_evidence,
        account_evidence=decision.account_evidence,
        durable_record=durable_record,
        commit_receipt=receipt,
        recovery_evidence=recovery_evidence,
        operator_evidence=decision.operator_evidence,
        capability=decision.capability,
        claim=claim,
    )


def _tamper_sidecar_status(path: Path) -> None:
    connection = _connect(path)
    try:
        connection.execute("DROP TRIGGER observations_no_update")
        connection.execute(
            "UPDATE observations SET status = 'REJECTED'"
        )
        connection.execute(_TRIGGER_UPDATE)
    finally:
        connection.close()


def _rewrite_schema(path: Path, *, name: str, old: str, new: str) -> None:
    connection = _connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        assert row is not None
        sql = str(row["sql"])
        assert old in sql
        version = int(
            connection.execute("PRAGMA schema_version").fetchone()[0]
        )
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE name = ?",
            (sql.replace(old, new, 1), name),
        )
        connection.execute(f"PRAGMA schema_version = {version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
    finally:
        connection.close()


def _drop_object(path: Path, kind: str, name: str) -> None:
    connection = _connect(path)
    try:
        connection.execute(f"DROP {kind} {name}")
    finally:
        connection.close()


def _run_reader(
    database: Path,
    target: ExecutionTargetBinding,
    sidecar: Path,
    moment: datetime,
    output: Path,
) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(_READER_WORKER),
            str(database),
            target.model_dump_json(),
            str(sidecar),
            moment.isoformat(),
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def test_oracle_f_corruption_family_separation(tmp_path: Path) -> None:
    store, _provenance, target, ledger, execstate_path, _sidecar = _composed(
        tmp_path
    )
    connection = _connect(execstate_path)
    try:
        connection.execute("UPDATE submissions SET state = 'REJECTED'")
    finally:
        connection.close()
    before = _db_snapshot(execstate_path)
    with pytest.raises(ExecutionStateCorruptionError) as execstate_failure:
        ledger.read_attempt(target.submission_id, target)
    assert not isinstance(
        execstate_failure.value, ExecutionLedgerCorruptionError
    )
    assert not isinstance(execstate_failure.value, ReconciliationConflict)
    assert _db_snapshot(execstate_path) == before
    ledger.close()
    store.close()

    second = tmp_path / "sidecar-branch"
    store, _provenance, target, ledger, execstate_path, sidecar = _composed(
        second
    )
    ledger.record_observation(_observation(store, target))
    _tamper_sidecar_status(sidecar)
    before_sidecar = _db_snapshot(sidecar)
    before_execstate = _db_snapshot(execstate_path)
    with pytest.raises(ExecutionLedgerCorruptionError) as ledger_failure:
        ledger.read_attempt(target.submission_id, target)
    assert not isinstance(
        ledger_failure.value, ExecutionStateCorruptionError
    )
    assert not isinstance(ledger_failure.value, ReconciliationConflict)
    assert _db_snapshot(sidecar) == before_sidecar
    assert _db_snapshot(execstate_path) == before_execstate
    ledger.close()
    store.close()


def test_oracle_g_locked_adapter_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired: list[str] = []

    def tripwire(*_args: object, **_kwargs: object) -> object:
        fired.append("fired")
        raise AssertionError("forbidden capability was invoked")

    monkeypatch.setattr("socket.socket", tripwire)
    monkeypatch.setattr("socket.create_connection", tripwire)
    monkeypatch.setattr("urllib.request.urlopen", tripwire)
    monkeypatch.setattr("subprocess.Popen", tripwire)
    monkeypatch.setattr(os, "getenv", tripwire)
    monkeypatch.setattr(random, "random", tripwire)
    monkeypatch.setattr(uuid, "uuid4", tripwire)

    class Incomplete(BrokerAdapter):
        pass

    target = make_synthetic_target()
    secret = "leak-sentinel-7"
    for adapter in (LockedBrokerAdapter(), Incomplete()):
        for call in (
            lambda adapter=adapter: adapter.submit(object()),  # type: ignore[arg-type]
            lambda adapter=adapter: adapter.query_order(  # type: ignore[arg-type]
                secret, target
            ),
            lambda adapter=adapter: adapter.cancel_order(  # type: ignore[arg-type]
                secret, target
            ),
        ):
            with pytest.raises(BrokerAdapterNotImplementedError) as excinfo:
                call()
            assert secret not in str(excinfo.value)
            assert secret not in repr(excinfo.value.args)
    assert fired == []
    assert LockedBrokerAdapter.__slots__ == ()
    adapter = LockedBrokerAdapter()
    with pytest.raises(AttributeError):
        adapter.extra = 1  # type: ignore[attr-defined]
    source = Path(adapter_module.__file__).read_text(encoding="utf-8")
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
    assert roots <= {"__future__", "typing", "quantforge"}
    assert relative <= {"errors", "models"}
    for token in (
        "socket",
        "urllib",
        "requests",
        "subprocess",
        "os.environ",
        "getenv",
        "random",
        "uuid4",
    ):
        assert token not in source
def test_oracle_h_replay_and_contradiction(tmp_path: Path) -> None:
    store, _provenance, target, ledger, execstate_path, sidecar = _composed(
        tmp_path
    )
    observation = _observation(store, target)
    first = ledger.record_observation(observation)
    view_before = ledger.read_attempt(target.submission_id, target)
    replay = ledger.record_observation(
        dataclasses.replace(observation, observation_id=first.observation_id)
    )
    assert replay.observation_id == "execstate-observation-v1-2"
    view_after = ledger.read_attempt(target.submission_id, target)
    assert view_after.phase is view_before.phase
    assert view_after.observation_ids == (
        first.observation_id,
        replay.observation_id,
    )
    assert _sidecar_kinds(sidecar) == [
        "BROKER_OBSERVATION",
        "OBSERVATION_REPLAY",
    ]

    rejected = _observation(
        store,
        target,
        status=BrokerObservationStatus.REJECTED,
        broker_reference="broker-ref-2",
    )
    before = (_db_snapshot(execstate_path), _db_snapshot(sidecar))
    with pytest.raises(ReconciliationConflict) as conflict:
        ledger.record_observation(rejected)
    assert conflict.value.assessment.reason is (
        ReconciliationReason.CONFLICT_CONTRADICTORY_OBSERVATION
    )
    assert (_db_snapshot(execstate_path), _db_snapshot(sidecar)) == before

    raw_sidecar = SQLiteObservationStore(sidecar)
    try:
        raw_sidecar.append(
            _observation(
                store,
                target,
                status=BrokerObservationStatus.REJECTED,
                broker_reference="broker-ref-3",
            )
        )
    finally:
        raw_sidecar.close()
    before_read = (_db_snapshot(execstate_path), _db_snapshot(sidecar))
    with pytest.raises(ExecutionLedgerCorruptionError):
        ledger.read_attempt(target.submission_id, target)
    assert (_db_snapshot(execstate_path), _db_snapshot(sidecar)) == (
        before_read
    )
    ledger.close()
    store.close()


def test_oracle_i_unknown_sticky_restart_owner_epoch(
    tmp_path: Path,
) -> None:
    store, _provenance, target, ledger, execstate_path, sidecar = _composed(
        tmp_path
    )
    ledger.record_observation(
        _observation(
            store,
            target,
            status=BrokerObservationStatus.UNKNOWN,
            broker_reference="broker-ref-unknown",
        )
    )
    assert ledger.read_attempt(target.submission_id, target).phase is (
        AttemptPhase.UNKNOWN
    )
    ledger.close()
    store.close()
    reopened = SQLiteExecutionStateStore(execstate_path)
    recover_store(reopened, target, observed_at=LATE_AT)
    second = DurableExecutionLedger(reopened, sidecar)
    try:
        assert second.read_attempt(target.submission_id, target).phase is (
            AttemptPhase.UNKNOWN
        )
        second.record_observation(
            _observation(
                reopened,
                target,
                broker_reference="broker-ref-late",
            )
        )
        current = second.read_attempt(target.submission_id, target)
        assert current.phase is AttemptPhase.UNKNOWN
        assert current.phase not in {
            AttemptPhase.PREPARED,
            AttemptPhase.CLAIMED,
            AttemptPhase.DISPATCHING,
        }
    finally:
        second.close()
        reopened.close()
    reader = _run_reader(
        execstate_path,
        target,
        sidecar,
        LATE_AT + timedelta(hours=1),
        tmp_path / "owner-change.json",
    )
    assert reader["current"]["phase"] == "UNKNOWN"
    assert reader["current"]["execstate_state"] == "OUTCOME_UNKNOWN"


def test_oracle_j_sidecar_schema_path_matrix(tmp_path: Path) -> None:
    database = tmp_path / "sidecar" / "ledger.sqlite3"
    database.parent.mkdir()
    SQLiteObservationStore(database).close()
    connection = _connect(database)
    try:
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM metadata")
        }
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if str(row["name"]) != "sqlite_sequence"
        }
        triggers = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    finally:
        connection.close()
    assert metadata["schema_name"] == "quantforge.execution-ledger.sqlite"
    assert metadata["schema_version"] == "1"
    assert tables == {"metadata", "observations"}
    assert triggers == {
        "observations_no_update",
        "observations_no_delete",
    }

    before_repair = _db_snapshot(database)
    _drop_object(database, "TRIGGER", "observations_no_delete")
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(database)
    assert _db_snapshot(database)["rows"] == before_repair["rows"]

    weakened = tmp_path / "weak.sqlite3"
    SQLiteObservationStore(weakened).close()
    _rewrite_schema(
        weakened,
        name="observations",
        old="generation >= 1",
        new="generation >= 0",
    )
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(weakened)

    path_cases = {
        "relative": "relative/ledger.sqlite3",
        "memory": ":memory:",
        "uri": "file:///D:/ledger.sqlite3",
        "empty": "",
        "nul": str(tmp_path / "a\x00b.sqlite3"),
        "too_long": "D:\\" + "x" * 4100 + ".sqlite3",
        "missing": str(tmp_path / "missing" / "ledger.sqlite3"),
        "reserved": str(tmp_path / "CON.sqlite3"),
    }
    for value in path_cases.values():
        with pytest.raises(ExecutionLedgerPathError):
            SQLiteObservationStore(value)
    zero = tmp_path / "zero.sqlite3"
    zero.write_bytes(b"")
    with pytest.raises(ExecutionLedgerCorruptionError):
        SQLiteObservationStore(zero)
    non_database = tmp_path / "not-a-database.sqlite3"
    non_database.write_bytes(b"not a database")
    with pytest.raises(ExecutionLedgerSchemaError):
        SQLiteObservationStore(non_database)
    valid = tmp_path / "valid.sqlite3"
    SQLiteObservationStore(valid).close()
    assert valid.is_file()
    with pytest.raises(ExecutionLedgerPathError):
        SQLiteObservationStore(r"\\server\share\ledger.sqlite3")
    target_directory = tmp_path / "junction-target"
    target_directory.mkdir()
    junction = tmp_path / "junction-parent"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target_directory)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode == 0:
        try:
            with pytest.raises(ExecutionLedgerPathError):
                SQLiteObservationStore(junction / "ledger.sqlite3")
        finally:
            subprocess.run(
                ["cmd", "/c", "rmdir", str(junction)],
                check=False,
                capture_output=True,
            )
    symlink_target = tmp_path / "symlink-target"
    symlink_target.mkdir()
    symlink_parent = tmp_path / "symlink-parent"
    try:
        os.symlink(symlink_target, symlink_parent)
    except OSError:
        pass
    else:
        with pytest.raises(ExecutionLedgerPathError):
            SQLiteObservationStore(symlink_parent / "ledger.sqlite3")


def test_oracle_k_cross_database_crash_windows(tmp_path: Path) -> None:
    record_window = tmp_path / "record-window"
    store, _provenance, target, ledger, execstate_path, sidecar = _composed(
        record_window
    )
    ledger.close()
    store.close()
    completed = subprocess.run(
        [
            sys.executable,
            str(_CRASH_WINDOW_WORKER),
            "record",
            str(execstate_path),
            target.model_dump_json(),
            str(sidecar),
            LATE_AT.isoformat(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert _sidecar_count(sidecar) == 1
    reopened = SQLiteExecutionStateStore(execstate_path)
    recover_store(reopened, target, observed_at=LATE_AT + timedelta(hours=1))
    first_ledger = DurableExecutionLedger(reopened, sidecar)
    try:
        history = first_ledger.read_attempt_history(
            target.submission_id, target
        )
        current = first_ledger.read_attempt(target.submission_id, target)
    finally:
        first_ledger.close()
        reopened.close()
    assert current.phase is AttemptPhase.UNKNOWN
    assert any(view.observation_ids for view in history)
    assert all(view.phase is not AttemptPhase.FINALIZED for view in history)

    assess_window = tmp_path / "assess-window"
    store, _provenance, target, ledger, execstate_path, sidecar = _composed(
        assess_window
    )
    ledger.close()
    store.close()
    completed = subprocess.run(
        [
            sys.executable,
            str(_CRASH_WINDOW_WORKER),
            "assess",
            str(execstate_path),
            target.model_dump_json(),
            str(sidecar),
            LATE_AT.isoformat(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert _sidecar_count(sidecar) == 0
    reopened = SQLiteExecutionStateStore(execstate_path)
    recover_store(reopened, target, observed_at=LATE_AT + timedelta(hours=1))
    second_ledger = DurableExecutionLedger(reopened, sidecar)
    try:
        history = second_ledger.read_attempt_history(
            target.submission_id, target
        )
    finally:
        second_ledger.close()
        reopened.close()
    assert all(view.observation_ids == () for view in history)

    dispatch_window = tmp_path / "dispatch-window"
    directory = dispatch_window / "state"
    directory.mkdir(parents=True)
    database = directory / "execstate.sqlite3"
    sidecar = dispatch_window / "ledger.sqlite3"
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(_CRASH_WORKER),
            str(database),
            target.model_dump_json(),
            target.submission_id,
            RECOVER_AT.isoformat(),
            PREPARE_AT.isoformat(),
            CLAIM_AT.isoformat(),
            CLAIM_EXPIRES_AT.isoformat(),
            RECOVER_RESCAN_AT.isoformat(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    connection = _connect(database)
    try:
        state = str(
            connection.execute(
                "SELECT state FROM submissions"
            ).fetchone()["state"]
        )
    finally:
        connection.close()
    assert state == "DISPATCHING"
    assert _sidecar_count(sidecar) == 0
    reopened = SQLiteExecutionStateStore(database)
    recover_store(reopened, target, observed_at=LATE_AT)
    third_ledger = DurableExecutionLedger(reopened, sidecar)
    try:
        history = third_ledger.read_attempt_history(
            target.submission_id, target
        )
        current = third_ledger.read_attempt(target.submission_id, target)
    finally:
        third_ledger.close()
        reopened.close()
    assert current.phase is AttemptPhase.UNKNOWN
    assert all(view.observation_ids == () for view in history)


_DETERMINISM_SCRIPT = r"""
import json
from datetime import UTC, datetime

from quantforge.execution_ledger import (
    BrokerObservationStatus,
    BrokerOrderObservation,
)
from quantforge.execution_ledger.identity import derive_attempt_id
from quantforge.execution_ledger.models import observation_envelope_json
from quantforge.execution_ledger.projection import AuditFact, project_attempt
from quantforge.execution_ledger.reconciliation import assess
from quantforge.frontier import ExecutionTargetBinding

target = ExecutionTargetBinding(
    schema="frontier-target-v1",
    broker="broker-x",
    environment="SANDBOX",
    account_reference="acct-7",
    adapter_id="adapter-x",
    adapter_version="1",
    submission_id="broker1-" + "a" * 32,
)
moment = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
observation = BrokerOrderObservation(
    submission_id=target.submission_id,
    target=target,
    generation=1,
    recovery_epoch=1,
    status=BrokerObservationStatus.ACCEPTED,
    broker_reference="broker-ref-1",
    broker_message=None,
    observed_at=moment,
)
prepare = AuditFact(
    event_sequence=1,
    event_kind="PREPARED",
    reason="NEW_PREPARE",
    submission_id=target.submission_id,
    target_json=target.model_dump_json(),
    generation=1,
    recovery_epoch=1,
    new_state="PREPARED",
    observed_at=moment,
)
mark = AuditFact(
    event_sequence=2,
    event_kind="DISPATCHING",
    reason="MARK_DISPATCHING",
    submission_id=target.submission_id,
    target_json=target.model_dump_json(),
    generation=1,
    recovery_epoch=1,
    new_state="DISPATCHING",
    observed_at=moment,
)
view = project_attempt(
    submission_id=target.submission_id,
    target=target,
    generation=1,
    recovery_epoch=1,
    execstate_state="DISPATCHING",
    audit_facts=(prepare, mark),
)
assessment = assess(execstate_state="DISPATCHING", observation=observation)
print(
    json.dumps(
        {
            "attempt": derive_attempt_id(
                target.submission_id,
                target.model_dump_json(),
                1,
                1,
            ),
            "envelope": observation_envelope_json(
                observation,
                observation_id="execstate-observation-v1-1",
            ),
            "assessment": {
                "result": assessment.result.value,
                "reason": assessment.reason.value,
                "phase": assessment.required_phase.value,
            },
            "view": {
                "attempt": view.attempt_id,
                "phase": view.phase.value,
                "reason": view.phase_reason,
            },
        },
        sort_keys=True,
    )
)
"""


def test_oracle_l_pythonhashseed_determinism() -> None:
    repository = Path(__file__).resolve().parents[2]
    outputs = []
    for seed in ("0", "1", "42", "12345"):
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        completed = subprocess.run(
            [sys.executable, "-c", _DETERMINISM_SCRIPT],
            cwd=str(repository),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(completed.stdout)
    assert len(set(outputs)) == 1
    assert "execstate-attempt-v1-" in outputs[0]
    assert "execstate-observation-v1-1" in outputs[0]


def test_oracle_m_epoch_rebind_separation(tmp_path: Path) -> None:
    store, _provenance, target, ledger, execstate_path, sidecar = _composed(
        tmp_path
    )
    record, _receipt = store.read(target.submission_id, target)
    first = ledger.record_observation(_observation(store, target))
    first_view = ledger.read_attempt(target.submission_id, target)
    ledger.close()
    store.close()
    reopened = SQLiteExecutionStateStore(execstate_path)
    recover_store(reopened, target, observed_at=LATE_AT)
    second = DurableExecutionLedger(reopened, sidecar)
    second_record, _receipt = reopened.read(target.submission_id, target)
    second_observation = _observation(
        reopened,
        target,
        status=BrokerObservationStatus.UNKNOWN,
        broker_reference="broker-ref-epoch-2",
    )
    recorded = second.record_observation(second_observation)
    history = second.read_attempt_history(target.submission_id, target)
    second.close()
    reopened.close()
    assert first_view.attempt_id == derive_attempt_id(
        target.submission_id,
        target.model_dump_json(),
        record.generation,
        record.recovery_epoch,
    )
    current_id = derive_attempt_id(
        target.submission_id,
        target.model_dump_json(),
        second_record.generation,
        second_record.recovery_epoch,
    )
    assert current_id != first_view.attempt_id
    first_slice = next(
        view for view in history if view.attempt_id == first_view.attempt_id
    )
    current_slice = next(
        view for view in history if view.attempt_id == current_id
    )
    assert first_slice.observation_ids == (first.observation_id,)
    assert current_slice.observation_ids == (recorded.observation_id,)
    assert first_slice.execstate_state == "DISPATCHING"
    assert current_slice.execstate_state == "OUTCOME_UNKNOWN"
    assert first_slice.phase is AttemptPhase.OBSERVED
    assert current_slice.phase is AttemptPhase.UNKNOWN
    third_store = SQLiteExecutionStateStore(execstate_path)
    recover_store(third_store, target, observed_at=LATE_AT + timedelta(hours=1))
    third = DurableExecutionLedger(third_store, sidecar)
    try:
        third_history = third.read_attempt_history(
            target.submission_id, target
        )
    finally:
        third.close()
        third_store.close()
    newest = third_history[-1]
    assert newest.attempt_id not in {
        view.attempt_id for view in history
    }
    assert newest.phase is AttemptPhase.UNKNOWN
    assert newest.observation_ids == ()


def test_oracle_n_observation_hygiene(tmp_path: Path) -> None:
    target = make_synthetic_target()
    make_observation(broker_reference="a" * 256)
    make_observation(broker_message="m" * 2048)
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(broker_reference="a" * 257)
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(broker_reference="")
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(broker_reference="bad\nreference")
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(broker_message="m" * 2049)
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(
            observed_at=datetime(  # noqa: DTZ001 - intentional naive input
                2030, 1, 1, 12, 0
            )
        )
    with pytest.raises(TypeError):
        make_observation(observed_at="not-a-datetime")  # type: ignore[arg-type]
    normalized = make_observation(
        observed_at=datetime(
            2030, 1, 1, 20, 0, tzinfo=timezone(timedelta(hours=8))
        )
    )
    assert normalized.observed_at == datetime(
        2030, 1, 1, 12, 0, tzinfo=UTC
    )
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(submission_id="not-a-submission", target=target)
    with pytest.raises(ExecutionLedgerDeniedError):
        make_observation(generation=0)
    with pytest.raises(TypeError):
        make_observation(generation=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        make_observation(status="ACCEPTED")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        make_observation(raw_response="forbidden")  # type: ignore[call-arg]
    observation = make_observation()
    with pytest.raises(dataclasses.FrozenInstanceError):
        observation.generation = 2  # type: ignore[misc]

    envelope = observation_envelope_json(
        observation, observation_id="execstate-observation-v1-1"
    )
    parsed = json.loads(envelope)
    assert set(parsed) == {
        "adapter_id",
        "adapter_version",
        "broker_message",
        "broker_reference",
        "generation",
        "observation_id",
        "observation_kind",
        "observed_at",
        "recovery_epoch",
        "status",
        "submission_id",
        "target_json",
    }
    with pytest.raises(ExecutionLedgerCorruptionError):
        observation_from_envelope_json(
            json.dumps(
                {**parsed, "raw_response": "forbidden"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    with pytest.raises(ExecutionLedgerCorruptionError):
        observation_from_envelope_json(
            envelope.replace(
                '"adapter_id":"adapter-x"',
                '"adapter_id":"adapter-x","adapter_id":"adapter-x"',
                1,
            )
        )
    with pytest.raises(ExecutionLedgerCorruptionError):
        observation_from_envelope_json(
            envelope.replace('"generation":1', '"generation":NaN', 1)
        )
    with pytest.raises(ExecutionLedgerCorruptionError):
        observation_from_envelope_json(
            json.dumps(parsed, indent=2, sort_keys=True)
        )
    oversized = json.dumps(
        {**parsed, "broker_message": "m" * 20000},
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(ExecutionLedgerCorruptionError):
        observation_from_envelope_json(oversized)
    assert observation_semantic_key(observation) == observation_semantic_key(
        dataclasses.replace(
            observation, observation_id="execstate-observation-v1-2"
        )
    )
    assert list(tmp_path.iterdir()) == []


def test_oracle_o_scope_purity(tmp_path: Path) -> None:
    package = Path(ledger_module.__file__).resolve().parent
    files = {path.name for path in package.glob("*.py")}
    assert files == {
        "__init__.py",
        "_canonical.py",
        "adapter.py",
        "errors.py",
        "identity.py",
        "ledger.py",
        "models.py",
        "projection.py",
        "reconciliation.py",
        "sidecar.py",
    }
    forbidden = (
        "socket",
        "urllib",
        "requests",
        "aiohttp",
        "websocket",
        "subprocess",
        "os.environ",
        "getenv",
        "keyring",
        "random",
        "datetime.now",
        "utcnow",
        "time.time",
        "uuid4",
        "secrets",
    )
    for path in sorted(package.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, (path.name, token)
    ledger_public = {
        name
        for name in dir(DurableExecutionLedger)
        if not name.startswith("_")
        and callable(getattr(DurableExecutionLedger, name))
    }
    assert ledger_public == {
        "assess_observation",
        "record_observation",
        "read_attempt",
        "read_attempt_history",
        "read_observations",
        "close",
    }
    sidecar_public = {
        name
        for name in dir(SQLiteObservationStore)
        if not name.startswith("_")
        and callable(getattr(SQLiteObservationStore, name))
    }
    assert sidecar_public == {"append", "read_observations", "close"}
    adapter_public = {
        name
        for name in dir(LockedBrokerAdapter)
        if not name.startswith("_")
        and callable(getattr(LockedBrokerAdapter, name))
    }
    assert adapter_public == {"submit", "query_order", "cancel_order"}

    fired: list[str] = []

    def tripwire(*_args: object, **_kwargs: object) -> object:
        fired.append("fired")
        raise AssertionError("forbidden capability was invoked")

    import socket

    original_socket = socket.socket
    original_urlopen = urllib.request.urlopen
    original_popen = subprocess.Popen
    original_getenv = os.getenv
    original_random = random.random
    original_uuid4 = uuid.uuid4
    socket.socket = tripwire  # type: ignore[assignment]
    urllib.request.urlopen = tripwire  # type: ignore[assignment]
    subprocess.Popen = tripwire  # type: ignore[assignment]
    os.getenv = tripwire  # type: ignore[assignment]
    random.random = tripwire  # type: ignore[assignment]
    uuid.uuid4 = tripwire  # type: ignore[assignment]
    try:
        store, _provenance, target, ledger, _execstate, _sidecar = _composed(
            tmp_path
        )
        observation = _observation(store, target)
        ledger.assess_observation(observation)
        ledger.record_observation(observation)
        view = ledger.read_attempt(target.submission_id, target)
        ledger.read_attempt_history(target.submission_id, target)
        ledger.read_observations(view.attempt_id)
        ledger.close()
        store.close()
    finally:
        socket.socket = original_socket  # type: ignore[assignment]
        urllib.request.urlopen = original_urlopen  # type: ignore[assignment]
        subprocess.Popen = original_popen  # type: ignore[assignment]
        os.getenv = original_getenv  # type: ignore[assignment]
        random.random = original_random  # type: ignore[assignment]
        uuid.uuid4 = original_uuid4  # type: ignore[assignment]
    assert fired == []


def test_oracle_p_derived_id_collision(tmp_path: Path) -> None:
    provenance = make_provenance()
    target = make_target(
        provenance.pre_dispatch_decision.envelope.submission_id
    )
    target_json = target.model_dump_json()
    independent = _independent_attempt_id(
        target.submission_id, target_json, 1, 1
    )
    assert derive_attempt_id(target.submission_id, target_json, 1, 1) == (
        independent
    )
    assert derive_attempt_id(target.submission_id, target_json, 1, 1) == (
        independent
    )
    assert (
        verify_attempt_binding(
            independent, target.submission_id, target_json, 1, 1
        )
        is None
    )
    with pytest.raises(ExecutionLedgerCorruptionError) as first:
        verify_attempt_binding(
            independent, target.submission_id, target_json, 2, 1
        )
    assert not isinstance(first.value, ReconciliationConflict)
    with pytest.raises(ExecutionLedgerCorruptionError) as second:
        verify_attempt_binding(
            independent,
            target.submission_id,
            target_json,
            1,
            2,
        )
    assert not isinstance(second.value, ReconciliationConflict)
    observation = make_observation(
        submission_id=target.submission_id, target=target
    )
    other = derive_attempt_id(
        target.submission_id, target_json, 2, 1
    )
    with pytest.raises(ExecutionLedgerCorruptionError) as mismatch:
        assess(
            execstate_state="DISPATCHING",
            observation=observation,
            attempt_id=other,
        )
    assert not isinstance(mismatch.value, ReconciliationConflict)
    assert other not in str(mismatch.value)


def test_oracle_q_payload_boundary(tmp_path: Path) -> None:
    store, _provenance, target, ledger, _execstate, sidecar = _composed(
        tmp_path
    )
    observation = _observation(store, target)
    first = ledger.record_observation(observation)
    connection = _connect(sidecar)
    try:
        columns = [
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(observations)"
            )
        ]
        payloads = [
            str(row["payload_json"])
            for row in connection.execute(
                "SELECT payload_json FROM observations ORDER BY "
                "observation_sequence"
            )
        ]
    finally:
        connection.close()
    assert columns == [
        "observation_sequence",
        "observation_id",
        "attempt_id",
        "submission_id",
        "target_json",
        "generation",
        "recovery_epoch",
        "observation_kind",
        "status",
        "broker_reference",
        "broker_message",
        "observed_at",
        "payload_json",
        "recorded_at",
    ]
    assert len(payloads) == 1
    parsed = json.loads(payloads[0])
    assert set(parsed) == {
        "adapter_id",
        "adapter_version",
        "broker_message",
        "broker_reference",
        "generation",
        "observation_id",
        "observation_kind",
        "observed_at",
        "recovery_epoch",
        "status",
        "submission_id",
        "target_json",
    }
    assert parsed["observation_id"] == first.observation_id
    assert payloads[0] == observation_envelope_json(
        dataclasses.replace(
            observation, observation_id=first.observation_id
        ),
        observation_id=first.observation_id,
        observation_kind="BROKER_OBSERVATION",
    )
    ledger.close()
    store.close()


def test_oracle_r_authority_non_substitution(tmp_path: Path) -> None:
    store, provenance, target, ledger, _execstate, _sidecar = _composed(
        tmp_path
    )
    record, _receipt = store.read(target.submission_id, target)
    view = ledger.read_attempt(target.submission_id, target)
    with pytest.raises(TypeError):
        store.claim(target.submission_id, view)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        assess(execstate_state="DISPATCHING", observation=view)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        project_attempt(
            submission_id=target.submission_id,
            target=target,
            generation=record.generation,
            recovery_epoch=record.recovery_epoch,
            execstate_state="DISPATCHING",
            audit_facts=(view,),  # type: ignore[arg-type]
        )
    ledger.close()
    store.close()

    second = tmp_path / "frontier"
    provenance = make_provenance()
    envelope = provenance.pre_dispatch_decision.envelope
    target = make_target(envelope.submission_id)
    store = make_store(second)
    recover_store(store, target, observed_at=RECOVER_AT)
    record, receipt = prepare_store(store, provenance, target)
    claim = store.claim(
        envelope.submission_id,
        target,
        owner_id="worker-1",
        token="token-1",
        acquired_at=CLAIM_AT,
        expires_at=CLAIM_EXPIRES_AT,
    )
    cover = recover_store(store, target, observed_at=RECOVER_RESCAN_AT)
    decision = build_eligibility(
        provenance, target, record, receipt, cover, claim
    )
    ledger = DurableExecutionLedger(store, second / "ledger.sqlite3")
    try:
        view = ledger.read_attempt(target.submission_id, target)
    finally:
        ledger.close()
        store.close()
    with pytest.raises(Exception) as failure:
        _evaluate_with_record(
            provenance,
            target,
            record,
            receipt,
            cover,
            claim,
            decision,
            durable_record=view,
        )
    assert not isinstance(failure.value, ReconciliationConflict)
    assert failure.value.__class__.__name__ in {
        "FrontierError",
        "TypeError",
        "ValueError",
    }


def test_oracle_s_persistence_minimization(tmp_path: Path) -> None:
    store, _provenance, target, ledger, execstate_path, sidecar = _composed(
        tmp_path
    )
    first = ledger.record_observation(_observation(store, target))
    ledger.record_observation(
        dataclasses.replace(
            _observation(store, target), observation_id=first.observation_id
        )
    )
    connection = _connect(sidecar)
    try:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if str(row["name"]) != "sqlite_sequence"
        }
        indexes = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND sql IS NOT NULL"
            )
        }
        triggers = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    finally:
        connection.close()
    assert tables == {"metadata", "observations"}
    assert indexes == {"observations_attempt_lookup"}
    assert triggers == {
        "observations_no_update",
        "observations_no_delete",
    }
    execstate_connection = _connect(execstate_path)
    try:
        execstate_tables = {
            str(row["name"])
            for row in execstate_connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if str(row["name"]) != "sqlite_sequence"
        }
    finally:
        execstate_connection.close()
    assert execstate_tables == {
        "metadata",
        "submissions",
        "commit_receipts",
        "claims",
        "operational_payloads",
        "recovery_runs",
        "audit_events",
    }
    view_one = ledger.read_attempt(target.submission_id, target)
    view_two = ledger.read_attempt(target.submission_id, target)
    assert view_one == view_two
    assert view_one is not view_two
    source = Path(ledger_module.__file__).read_text(encoding="utf-8")
    for token in ("INSERT", "UPDATE", "DELETE", "sqlite3"):
        assert token not in source
    assert _sidecar_count(sidecar) == 2
    ledger.close()
    store.close()
