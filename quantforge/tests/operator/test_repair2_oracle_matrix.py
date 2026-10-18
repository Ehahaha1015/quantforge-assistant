"""Mandatory section-30 oracle families that were still absent after Review #2."""

from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quantforge.operator import (
    ControlAction,
    OperatorAuthenticationError,
    OperatorAuthenticationProof,
    OperatorConflictError,
    OperatorSchemaError,
    SQLiteOperatorControlStore,
    VerifiedOperatorIdentity,
    operator_evidence_for_frontier,
)
from quantforge.operator.identity import request_commitment
from quantforge.operator.static_checks import scan_tripwire_a, scan_tripwire_b

from ._helpers import (
    COMMITTED_AT,
    AcceptingVerifier,
    RejectingDouble,
    make_policy,
    make_proof,
    make_request,
    make_target,
    store_path,
)

_ROOT = Path(__file__).resolve().parents[2]
_P = "src/quantforge/transport/probe.py"
_SENTINEL_A = "__OPCTL_SENTINEL_TESTVALUE__"
_SENTINEL_B = "_OPCTL-FIXTURE-SECRET-0123456789abcdef0123456789abcdef"


def _fresh(path: Path) -> SQLiteOperatorControlStore:
    return SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=AcceptingVerifier()
    )


# FR02 ---------------------------------------------------------------------


def test_fr02_a_literal_ddl_produces_eleven_rows(tmp_path) -> None:
    path = store_path(tmp_path)
    store = _fresh(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 11
    tables = {row[1] for row in rows if row[0] == "table"}
    explicit_indexes = {
        row[1] for row in rows if row[0] == "index" and row[3] is not None
    }
    autoindexes = {row[1] for row in rows if row[0] == "index" and row[3] is None}
    triggers = {row[1] for row in rows if row[0] == "trigger"}
    assert tables == {"metadata", "control_state", "control_audit"}
    assert explicit_indexes == {"control_audit_scope_sequence"}
    assert autoindexes == {
        "sqlite_autoindex_metadata_1",
        "sqlite_autoindex_control_state_1",
        "sqlite_autoindex_control_state_2",
        "sqlite_autoindex_control_audit_1",
        "sqlite_autoindex_control_audit_2",
    }
    assert triggers == {"control_audit_no_update", "control_audit_no_delete"}


def test_fr02_b_automatic_indexes_exact_identity(tmp_path) -> None:
    path = store_path(tmp_path)
    store = _fresh(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NULL"
        ).fetchall()
    finally:
        connection.close()
    expected = {
        "sqlite_autoindex_metadata_1": "metadata",
        "sqlite_autoindex_control_state_1": "control_state",
        "sqlite_autoindex_control_state_2": "control_state",
        "sqlite_autoindex_control_audit_1": "control_audit",
        "sqlite_autoindex_control_audit_2": "control_audit",
    }
    assert {row[1]: row[2] for row in rows} == expected
    assert all(row[0] == "index" and row[3] is None for row in rows)


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE extra_table (a TEXT)",
        "CREATE INDEX extra_index ON control_state (action_id)",
        "CREATE TRIGGER extra_trigger AFTER INSERT ON control_state BEGIN SELECT 1; END",
    ],
)
def test_fr02_c_unexpected_object_is_schema_error(tmp_path, ddl: str) -> None:
    path = store_path(tmp_path)
    store = _fresh(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(ddl)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(OperatorSchemaError):
        SQLiteOperatorControlStore(path, policy=make_policy())


def test_fr02_c_extra_table_with_automatic_index_is_schema_error(tmp_path) -> None:
    # A new automatic index cannot be introduced without a new table, so the
    # closed object-set rule rejects the extra autoindex together with it.
    path = store_path(tmp_path)
    store = _fresh(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE extra (a TEXT PRIMARY KEY)")
        connection.commit()
        autoindexes = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NULL"
        ).fetchall()
    finally:
        connection.close()
    assert len(autoindexes) == 6
    with pytest.raises(OperatorSchemaError):
        SQLiteOperatorControlStore(path, policy=make_policy())


# FR03 ---------------------------------------------------------------------


def test_fr03_a_tests_and_fixtures_may_construct_evidence() -> None:
    fixture = (_ROOT / "tests/frontier/_frontier_fixtures.py").read_text(
        encoding="utf-8"
    )
    violations = scan_tripwire_a({"tests/frontier/_frontier_fixtures.py": fixture})
    assert violations == ()


def test_fr03_b_repository_tripwire_a_is_green() -> None:
    sources = {
        str(path): path.read_text(encoding="utf-8")
        for path in (_ROOT / "src").rglob("*.py")
    }
    assert scan_tripwire_a(sources) == ()


def test_fr03_b_other_production_construction_sites_fail(tmp_path) -> None:
    source = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "def build(values):\n"
        "    return OperatorExecutionEnableEvidence(**values)\n"
    )
    assert scan_tripwire_a({"src/quantforge/broker/bad.py": source})


def test_fr03_i_wildcard_exemption_is_not_honoured() -> None:
    source = (
        "from quantforge.frontier import OperatorExecutionEnableEvidence\n"
        "def build(values):\n"
        "    return OperatorExecutionEnableEvidence(**values)\n"
    )
    # A new helper module in the same package as a permitted site is still
    # rejected: only the exact path+qualname pair is allow-listed.
    assert scan_tripwire_a({"src/quantforge/operator/helper.py": source})


_FRONTIER_MODELS = "src/quantforge/frontier/models.py"
_DIRECT_CONSTRUCTION = (
    "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
    "def build(values):\n"
    "    return OperatorExecutionEnableEvidence(**values)\n"
)


def test_fr03_c_frozen_frontier_site_is_category_2_reconstruction_only(
    tmp_path,
) -> None:
    real_source = (_ROOT / _FRONTIER_MODELS).read_text(encoding="utf-8")
    # The frozen reconstruction site is allow-listed as CATEGORY 2 ...
    assert scan_tripwire_a({_FRONTIER_MODELS: real_source}) == ()
    # ... the reconstruction symbol is exactly _parse_frontier_model ...
    reconstruction = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "def _parse_frontier_model(values):\n"
        "    return OperatorExecutionEnableEvidence(**values)\n"
    )
    assert scan_tripwire_a({_FRONTIER_MODELS: reconstruction}) == ()
    # ... and the same reconstruction outside that exact path is production.
    assert scan_tripwire_a(
        {"src/quantforge/frontier/reconstruction.py": reconstruction}
    )
    # A CATEGORY 2 reconstruction is never a fresh-authority origin for a
    # production evaluator call.
    evidence_source = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "from quantforge.frontier import evaluate_frontier_eligibility\n"
        "def compose(payload, **kw):\n"
        "    evidence = OperatorExecutionEnableEvidence.from_json(payload)\n"
        "    return evaluate_frontier_eligibility(\n"
        "        ..., operator_evidence=evidence, **kw\n"
        "    )\n"
    )
    assert scan_tripwire_b({_P: evidence_source})
    store = _fresh(tmp_path / "fr03c.sqlite")
    original = _produce_enabled_evidence(store)
    reconstructed = type(original).from_json(original.model_dump_json())
    assert reconstructed == original
    store.close()


def _produce_enabled_evidence(
    store: SQLiteOperatorControlStore,
):
    request = make_request(request_id="fr03-evidence")
    store.request_control_change(
        request,
        make_proof(request, proof_id="proof-fr03-evidence"),
        committed_at=COMMITTED_AT,
    )
    evidence = operator_evidence_for_frontier(store, make_target())
    assert evidence is not None and evidence.enabled is True
    return evidence


def test_fr03_d_frozen_reconstruction_preserves_fields_without_operator_store(
    tmp_path,
) -> None:
    source = (_ROOT / _FRONTIER_MODELS).read_text(encoding="utf-8")
    tree = ast.parse(source)
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "OperatorExecutionEnableEvidence"
    ]
    assert len(constructions) == 1
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(child is constructions[0] for child in ast.walk(node))
    )
    assert function.name == "_parse_frontier_model"
    assert "quantforge.operator" not in source
    assert "produce_operator_evidence" not in source
    assert "request_control_change" not in source
    bare_calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "action_id" not in bare_calls
    assert "evidence_id" not in bare_calls

    store = _fresh(tmp_path / "fr03d.sqlite")
    original = _produce_enabled_evidence(store)
    reconstructed = type(original).from_json(original.model_dump_json())
    assert reconstructed.evidence_id == original.evidence_id
    assert reconstructed.action_id == original.action_id
    assert reconstructed.enabled is original.enabled
    assert reconstructed.target == original.target
    assert reconstructed.observed_at == original.observed_at
    assert reconstructed.maximum_age == original.maximum_age
    store.close()


@pytest.mark.parametrize(
    "path",
    [
        "src/quantforge/broker/extra.py",
        "src/quantforge/live/extra.py",
        "src/quantforge/transport/extra.py",
        "src/quantforge/operator/extra.py",
    ],
)
def test_fr03_e_non_allowlisted_production_construction_is_detected(
    path: str,
) -> None:
    violations = scan_tripwire_a({path: _DIRECT_CONSTRUCTION})
    assert len(violations) == 1
    assert violations[0].path == path
    assert violations[0].line == 3
    assert violations[0].code == "TRIPWIRE-A"


def test_fr03_f_new_frontier_helper_inherits_no_category_2() -> None:
    source = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "def _parse_frontier_model(values):\n"
        "    return OperatorExecutionEnableEvidence(**values)\n"
        "def new_frontier_helper(values):\n"
        "    return OperatorExecutionEnableEvidence(**values)\n"
    )
    violations = scan_tripwire_a({_FRONTIER_MODELS: source})
    assert [violation.line for violation in violations] == [5]
    assert violations[0].code == "TRIPWIRE-A"


def test_fr03_g_operator_producer_sites_are_category_1_allowed() -> None:
    helper = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "def operator_evidence_for_frontier(store, target):\n"
        "    return OperatorExecutionEnableEvidence(**values)\n"
    )
    method = (
        "from quantforge.frontier.models import OperatorExecutionEnableEvidence\n"
        "class SQLiteOperatorControlStore:\n"
        "    def produce_operator_evidence(self, target):\n"
        "        return OperatorExecutionEnableEvidence(**values)\n"
    )
    assert scan_tripwire_a({"src/quantforge/operator/store.py": helper}) == ()
    assert scan_tripwire_a({"src/quantforge/operator/store.py": method}) == ()
    real_source = (_ROOT / "src/quantforge/operator/store.py").read_text(
        encoding="utf-8"
    )
    assert scan_tripwire_a({"src/quantforge/operator/store.py": real_source}) == ()


@pytest.mark.parametrize(
    "path",
    [
        "tests/operator/probe.py",
        "tests/frontier/_frontier_fixtures.py",
        ".autolab/reviews/OPERATOR-001/probe.py",
        "docs/examples/operator.py",
    ],
)
def test_fr03_h_non_production_scopes_are_outside_tripwire_a(
    path: str,
) -> None:
    assert scan_tripwire_a({path: _DIRECT_CONSTRUCTION}) == ()


# FR04 / FR05 / CR01 -------------------------------------------------------


def test_fr04_a_existing_request_with_invalid_auth(tmp_path) -> None:
    path = store_path(tmp_path)
    store = _fresh(path)
    request = make_request(request_id="fr04a")
    store.request_control_change(
        request, make_proof(request, proof_id="proof-fr04a"), committed_at=COMMITTED_AT
    )
    store.close()
    rejecting = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=RejectingDouble()
    )
    before = rejecting.read_control_history()
    with pytest.raises(OperatorAuthenticationError):
        rejecting.request_control_change(
            request,
            make_proof(request, proof_id="proof-fr04a"),
            committed_at=COMMITTED_AT,
        )
    assert rejecting.read_control_history() == before
    rejecting.close()


def test_fr04_b_exact_prior_request_replays(tmp_path) -> None:
    path = store_path(tmp_path)
    store = _fresh(path)
    request = make_request(request_id="fr04b")
    proof = make_proof(request, proof_id="proof-fr04b")
    store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    before = store.read_control_history()
    outcome = store.request_control_change(request, proof, committed_at=COMMITTED_AT)
    assert outcome.replay is True
    assert store.read_control_history() == before
    store.close()


def test_fr04_c_conflicting_request_raises_conflict(tmp_path) -> None:
    path = store_path(tmp_path)
    store = _fresh(path)
    request = make_request(request_id="fr04c")
    store.request_control_change(
        request, make_proof(request, proof_id="proof-fr04c"), committed_at=COMMITTED_AT
    )
    before = store.read_control_history()
    conflict = make_request(request_id="fr04c", action=ControlAction.DISABLE)
    with pytest.raises(OperatorConflictError):
        store.request_control_change(
            conflict,
            make_proof(conflict, proof_id="proof-fr04c-conflict"),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == before
    store.close()


class _SentinelProofVerifier:
    @property
    def verifier_id(self) -> str:
        return "operator-test-sentinel-v1"

    def verify(self, request, proof):  # type: ignore[no-untyped-def]
        return VerifiedOperatorIdentity.model_construct(
            schema="operator-verified-v1",
            operator_reference=request.operator_reference,
            control_source=request.control_source,
            verifier_id=self.verifier_id,
            verified_at=request.observed_at + timedelta(seconds=1),
            proof_id=proof.proof_id,
        )


@pytest.mark.parametrize("sentinel", [_SENTINEL_A, _SENTINEL_B])
def test_fr05_c_proof_id_sentinel_rejected_before_write(
    tmp_path, sentinel: str
) -> None:
    path = store_path(tmp_path)
    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=_SentinelProofVerifier()
    )
    request = make_request(request_id=f"fr05c-{sentinel[15:25]}")
    bad_proof = OperatorAuthenticationProof.model_construct(
        schema="operator-proof-v1",
        proof_id=sentinel,
        operator_reference=request.operator_reference,
        control_source=request.control_source,
        action=request.action,
        scope=request.scope,
        method="LOCAL_VERIFIER",
        request_commitment=request_commitment(request),
    )
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(request, bad_proof, committed_at=COMMITTED_AT)
    assert store.read_control_history() == ()
    store.close()
    assert sentinel.encode("utf-8") not in path.read_bytes()


def test_fr05_d_verifier_id_sentinel_rejected_before_write(tmp_path) -> None:
    path = store_path(tmp_path)

    class BadVerifierId(_SentinelProofVerifier):
        @property
        def verifier_id(self) -> str:
            return _SENTINEL_A

    store = SQLiteOperatorControlStore(
        path, policy=make_policy(), verifier=BadVerifierId()
    )
    request = make_request(request_id="fr05d")
    with pytest.raises(OperatorAuthenticationError):
        store.request_control_change(
            request,
            make_proof(request, proof_id="proof-fr05d"),
            committed_at=COMMITTED_AT,
        )
    assert store.read_control_history() == ()
    store.close()
    assert _SENTINEL_A.encode("utf-8") not in path.read_bytes()


class _FixedInstantVerifier:
    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    @property
    def verifier_id(self) -> str:
        return "operator-test-fixed-instant-v1"

    def verify(self, request, proof):  # type: ignore[no-untyped-def]
        return VerifiedOperatorIdentity(
            schema="operator-verified-v1",
            operator_reference=request.operator_reference,
            control_source=request.control_source,
            verifier_id=self.verifier_id,
            verified_at=self._instant,
            proof_id=proof.proof_id,
        )


def test_cr01_a_injected_fixed_instant_is_deterministic(tmp_path) -> None:
    instant = datetime(2030, 1, 1, 12, 0, 1, tzinfo=UTC)
    request = make_request(request_id="cr01a")
    proof = make_proof(request, proof_id="proof-cr01a")
    results = []
    for index in range(2):
        path = tmp_path / f"cr01a-{index}.sqlite"
        store = SQLiteOperatorControlStore(
            path, policy=make_policy(), verifier=_FixedInstantVerifier(instant)
        )
        store.request_control_change(request, proof, committed_at=COMMITTED_AT)
        results.append(store.read_control_history()[0])
        store.close()
    assert results[0].verified_at == results[1].verified_at == instant
    assert results[0].audit_id == results[1].audit_id


def test_cr01_a_store_consults_no_clock() -> None:
    source = (_ROOT / "src/quantforge/operator/store.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "datetime.now",
        "utcnow",
        "time.time",
        "monotonic",
        "perf_counter",
    ):
        assert forbidden not in source, forbidden


# INJECT-K and DISPATCH-H --------------------------------------------------


def test_inject_k_patched_decision_is_rejected_by_tripwire_c() -> None:
    source = (
        "from quantforge.frontier.evaluate import evaluate_frontier_eligibility\n"
        "from quantforge.execstate.store import SQLiteExecutionStateStore\n"
        "from quantforge.operator import operator_evidence_for_frontier\n"
        "def dispatch(store: SQLiteExecutionStateStore, target, **kw):\n"
        "    decision = evaluate_frontier_eligibility(\n"
        "        ..., operator_evidence=operator_evidence_for_frontier(store, target), **kw\n"
        "    )\n"
        "    decision = decision.model_copy(update={'eligible': True, 'reasons': ()})\n"
        "    return store.mark_dispatching(decision, owner_id='o', token='t')\n"
    )
    assert scan_tripwire_b({_P: source}) == ()
    from quantforge.operator.static_checks import scan_tripwire_c

    assert scan_tripwire_c({_P: source})


def test_dispatch_h_historical_read_is_outside_tripwire_c() -> None:
    source = (_ROOT / "src/quantforge/execstate/store.py").read_text(
        encoding="utf-8"
    )
    assert "FrontierEligibilityDecision.from_json" in source
    from quantforge.operator.static_checks import scan_tripwire_c

    assert scan_tripwire_c({str(_ROOT / "src/quantforge/execstate/store.py"): source}) == ()
