"""Offline PAPER transport fixtures; no production broker/credential evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from quantforge.broker import (
    BrokerSubmissionOutcome,
    create_broker_submission_envelope,
)
from quantforge.credentials import CredentialBoundary, CredentialReference
from quantforge.credentials_windows.backend import WindowsCredentialManagerBackend
from quantforge.credentials_windows.native import _NativeCredentialRecord
from quantforge.domain import OrderIntent
from quantforge.execution_ledger import BrokerObservationStatus, BrokerOrderObservation
from quantforge.frontier import BrokerCapabilityDeclaration, ExecutionTargetBinding
from quantforge.live import authorize_live_order, create_live_authorization
from quantforge.operator import (
    ControlAction,
    OperatorAuthenticationProof,
    OperatorControlPolicy,
    OperatorControlRequest,
    OperatorControlScope,
    SQLiteOperatorControlStore,
    VerifiedOperatorIdentity,
)
from quantforge.operator.identity import request_commitment
from quantforge.portfolio import PortfolioState
from quantforge.risk import RiskDecision, RiskLimits

from qf_platform.accounting_book import CoreAccountingBook
from qf_platform.broker_execution import (
    BrokerCancelCapability,
    BrokerExecutionSession,
    BrokerRiskObservation,
)
from qf_platform.storage import PlatformError

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
LIMITS = RiskLimits(
    max_order_notional=Decimal("100"),
    max_position_notional=Decimal("100"),
    max_gross_exposure=Decimal("100"),
)


class FixtureVerifier:
    verifier_id = "offline-operator-fixture"

    def verify(self, request: Any, proof: Any) -> VerifiedOperatorIdentity:
        return VerifiedOperatorIdentity(
            schema="operator-verified-v1",
            operator_reference=request.operator_reference,
            control_source=request.control_source,
            verifier_id=self.verifier_id,
            verified_at=request.observed_at,
            proof_id=proof.proof_id,
        )


class FixtureVault:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read(self, target: str) -> _NativeCredentialRecord:
        self.calls.append(target)
        return _NativeCredentialRecord(
            type_value=1,
            target_name=target,
            persist=2,
            user_name="quantforge-crd-v1:g1",
            blob=b"OFFLINE-PAPER-SECRET",
        )


class FixtureGateway:
    def __init__(self, observation: BrokerRiskObservation) -> None:
        self.observation = observation
        self.submit_calls = 0
        self.cancel_calls = 0
        self.unknown = False
        self.crash = False
        self.leases: list[Any] = []

    def observe(self, envelope: Any, target: Any, lease: Any) -> BrokerRiskObservation:
        assert lease.reveal(evaluation_time=NOW).as_bytes() == b"OFFLINE-PAPER-SECRET"
        self.leases.append(lease)
        return self.observation

    def submit(
        self, envelope: Any, observation: Any, lease: Any
    ) -> BrokerSubmissionOutcome:
        self.submit_calls += 1
        self.leases.append(lease)
        assert lease.reveal(evaluation_time=NOW).as_bytes() == b"OFFLINE-PAPER-SECRET"
        if self.crash:
            raise KeyboardInterrupt("offline simulated death at SDK frontier")
        if self.unknown:
            raise OSError("OFFLINE-PAPER-SECRET uncertain SDK result")
        return BrokerSubmissionOutcome(
            submission_id=envelope.submission_id,
            status="ACCEPTED",
            broker_message="OFFLINE-PAPER-SECRET",
            broker_reference="raw-account-id",
        )

    def query_attempt(
        self, sid: Any, target: Any, generation: Any, epoch: Any, lease: Any
    ) -> BrokerOrderObservation:
        self.leases.append(lease)
        return BrokerOrderObservation(
            submission_id=sid,
            target=target,
            generation=generation,
            recovery_epoch=epoch,
            status=BrokerObservationStatus.ACCEPTED,
            broker_reference="raw-account-id",
            broker_message="OFFLINE-PAPER-SECRET",
            observed_at=NOW,
        )

    def cancel_order(self, sid: Any, target: Any, lease: Any) -> None:
        self.leases.append(lease)
        self.cancel_calls += 1

    def cancel_capability(self, target: Any, lease: Any) -> BrokerCancelCapability:
        return BrokerCancelCapability(target, True, NOW, "d" * 64)


def fixture(tmp_path: Path, *, enabled: bool = True) -> tuple[Any, ...]:
    order = OrderIntent(
        strategy_id="offline-plumbing",
        symbol="FIXTURE",
        timestamp=NOW,
        side="BUY",
        quantity=Decimal("100"),
    )
    auth = create_live_authorization(order, "offline-operator", NOW)
    envelope = create_broker_submission_envelope(
        order,
        authorize_live_order(order, RiskDecision(approved=True), auth),
    )
    target = ExecutionTargetBinding(
        schema="frontier-target-v1",
        broker="fixture-broker",
        environment="PAPER",
        account_reference="redacted-account-1",
        adapter_id="offline-fixture",
        adapter_version="1",
        submission_id=envelope.submission_id,
    )
    capability = BrokerCapabilityDeclaration(
        schema="frontier-capability-v1",
        declaration_id="offline-capability-1",
        source_reference="offline-official-protocol-fixture",
        target=target,
        client_identity_mode="BROKER_ENFORCED",
        uniqueness_scope="ACCOUNT",
        uniqueness_window=timedelta(days=1),
        duplicate_behavior="RETURN_EXISTING",
        lookup_mode="BY_CLIENT_ID",
        reconciliation_mode="SUPPORTED",
        timeout_semantics="OUTCOME_UNKNOWN",
        frontier_classification="CONSERVATIVE",
        acknowledgement_semantics="ACK_NOT_FILL",
    )
    observation = BrokerRiskObservation(
        target=target,
        portfolio=PortfolioState(initial_cash=Decimal("100"), cash=Decimal("100")),
        prices={"FIXTURE": Decimal("0.50")},
        available_cash=Decimal("100"),
        estimated_fee=Decimal("5"),
        observed_at=NOW,
        account_hash="a" * 64,
        price_hash="b" * 64,
        rule_hash="c" * 64,
        lot_size=100,
        tick_size=Decimal("0.001"),
        limit_price=Decimal("0.501"),
        session_open=True,
        trading_permission=True,
        capability=capability,
    )
    operator = SQLiteOperatorControlStore(
        tmp_path / "operator.sqlite",
        policy=OperatorControlPolicy(
            schema="operator-policy-v1", maximum_age_upper_bound=timedelta(minutes=5)
        ),
        verifier=FixtureVerifier(),
    )
    if enabled:
        request = OperatorControlRequest(
            schema="operator-request-v1",
            request_id="offline-enable-1",
            action=ControlAction.ENABLE,
            scope=OperatorControlScope.from_target(target),
            operator_reference="offline-operator",
            control_source="offline-fixture",
            observed_at=NOW,
            maximum_age=timedelta(seconds=30),
        )
        proof = OperatorAuthenticationProof(
            schema="operator-proof-v1",
            proof_id="offline-proof-1",
            operator_reference=request.operator_reference,
            control_source=request.control_source,
            action=request.action,
            scope=request.scope,
            method="LOCAL_VERIFIER",
            request_commitment=request_commitment(request),
        )
        operator.request_control_change(request, proof, committed_at=NOW)
    vault = FixtureVault()
    gateway = FixtureGateway(observation)
    state = {"paused": False, "revoked": False}

    def guard(_target: ExecutionTargetBinding) -> None:
        if state["revoked"]:
            raise PlatformError("OFFLINE_BINDING_REVOKED")

    accounting = CoreAccountingBook(tmp_path / "independent-accounting")
    accounting.declare(target, Decimal("100"), "d" * 64)
    arguments = dict(
        binding_guard=guard,
        paused=lambda: state["paused"],
        credentials=CredentialBoundary(
            backend=WindowsCredentialManagerBackend(seam=vault)
        ),
        clock=lambda: NOW,
        accounting=accounting,
    )
    reference = CredentialReference(value="offline-reference")
    session = BrokerExecutionSession(
        tmp_path, gateway, operator, reference, **arguments
    )
    return (
        session,
        envelope,
        target,
        gateway,
        state,
        vault,
        operator,
        reference,
        arguments,
    )


def test_ack_duplicate_cancel_query_and_secret_lease_discard(tmp_path: Path) -> None:
    session, envelope, target, gateway, *rest = fixture(tmp_path)
    try:
        assert session.dispatch(envelope, target, LIMITS)["state"] == "ACKNOWLEDGED"
        with pytest.raises(PlatformError, match="PRIOR_ATTEMPT"):
            session.dispatch(envelope, target, LIMITS)
        assert gateway.submit_calls == 1
        assert (
            session.cancel(envelope.submission_id, target)["cancel"] == "QUERY_REQUIRED"
        )
        assert (
            session.reconcile(envelope.submission_id, target)["state"] == "ACKNOWLEDGED"
        )
        assert gateway.cancel_calls == 1
        for lease in gateway.leases:
            with pytest.raises(ValueError):
                lease.reveal(evaluation_time=NOW)
        for filename in ("execstate.sqlite3", "observations.sqlite3"):
            assert b"OFFLINE-PAPER-SECRET" not in (tmp_path / filename).read_bytes()
    finally:
        session.close()
        rest[2].close()


@pytest.mark.parametrize("crash", [False, True])
def test_unknown_crash_restart_no_resend_and_verified_reconciliation(
    tmp_path: Path, crash: bool
) -> None:
    session, envelope, target, gateway, state, vault, operator, ref, args = fixture(
        tmp_path
    )
    gateway.unknown, gateway.crash = not crash, crash
    if crash:
        with pytest.raises(KeyboardInterrupt):
            session.dispatch(envelope, target, LIMITS)
    else:
        assert session.dispatch(envelope, target, LIMITS)["state"] == "OUTCOME_UNKNOWN"
    session.close()
    restarted = BrokerExecutionSession(tmp_path, gateway, operator, ref, **args)
    try:
        with pytest.raises(PlatformError, match="PRIOR_ATTEMPT"):
            restarted.dispatch(envelope, target, LIMITS)
        assert gateway.submit_calls == 1
        assert (
            restarted.reconcile(envelope.submission_id, target)["state"]
            == "ACKNOWLEDGED"
        )
    finally:
        restarted.close()
        operator.close()


@pytest.mark.parametrize(
    "change",
    [
        {"observed_at": NOW - timedelta(seconds=31)},
        {"available_cash": Decimal("10")},
        {"lot_size": 200},
        {"tick_size": Decimal("0.01")},
        {"session_open": False},
        {"trading_permission": False},
        {"price_hash": "UNKNOWN"},
    ],
)
def test_bad_official_facts_never_prepare_or_send(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    session, envelope, target, gateway, _, _, operator, *_ = fixture(tmp_path)
    gateway.observation = replace(gateway.observation, **change)
    try:
        with pytest.raises(PlatformError, match="RISK_INPUT_INVALID"):
            session.dispatch(envelope, target, LIMITS)
        assert gateway.submit_calls == 0
    finally:
        session.close()
        operator.close()


def test_disabled_operator_kill_revocation_and_production_remain_locked(
    tmp_path: Path,
) -> None:
    session, envelope, target, gateway, state, vault, operator, *_ = fixture(
        tmp_path, enabled=False
    )
    try:
        with pytest.raises(PlatformError, match="FRONTIER_DENIED"):
            session.dispatch(envelope, target, LIMITS)
        state["paused"] = True
        with pytest.raises(PlatformError, match="PAUSED"):
            session.dispatch(envelope, target, LIMITS)
        production = ExecutionTargetBinding(
            **{**target.model_dump(), "environment": "PRODUCTION"}
        )
        state["paused"] = False
        with pytest.raises(PlatformError, match="PAUSED"):
            session.dispatch(envelope, production, LIMITS)
        state["revoked"] = True
        with pytest.raises(PlatformError, match="REVOKED"):
            session.dispatch(envelope, target, LIMITS)
        assert gateway.submit_calls == 0
        assert len(vault.calls) == 1
    finally:
        session.close()
        operator.close()


def test_real_clock_progression_accepts_fresh_pre_send_snapshot(tmp_path: Path) -> None:
    session, envelope, target, gateway, state, vault, operator, *_ = fixture(tmp_path)
    instants = [NOW + timedelta(microseconds=i) for i in range(30)]
    session.clock = lambda: instants.pop(0)
    # SDK fixture lease is revealed at the actual acquisition instant.
    gateway.observe = lambda e, t, lease: gateway.observation  # type: ignore[method-assign]
    gateway.submit = lambda e, o, lease: BrokerSubmissionOutcome(
        submission_id=e.submission_id, status="ACCEPTED"
    )  # type: ignore[method-assign]
    try:
        assert session.dispatch(envelope, target, LIMITS)["state"] == "ACKNOWLEDGED"
    finally:
        session.close()
        operator.close()


def test_pre_mark_revocation_never_writes_invalid_no_effect(tmp_path: Path) -> None:
    from quantforge.credentials import CredentialPurpose

    session, envelope, target, gateway, state, _, operator, *_ = fixture(tmp_path)
    original = session._lease

    def lease_then_revoke(target: Any, purpose: Any) -> Any:
        lease = original(target, purpose)
        if purpose == CredentialPurpose.ORDER_SUBMIT:
            state["paused"] = True
        return lease

    session._lease = lease_then_revoke  # type: ignore[method-assign]
    try:
        with pytest.raises(PlatformError, match="PRE_MARK_AUTHORITY_REVOKED"):
            session.dispatch(envelope, target, LIMITS)
        assert session.store.read(envelope.submission_id, target)[0].state == "PREPARED"
        assert gateway.submit_calls == 0
        assert (
            session.account_checks.read()[0]["derivation"]
            == "CORE_CREATE_PORTFOLIO_AND_APPLY_FILL"
        )
    finally:
        session.close()
        operator.close()


def test_pre_send_clock_jump_records_proven_no_effect(tmp_path: Path) -> None:
    session, envelope, target, gateway, state, vault, operator, *_ = fixture(tmp_path)
    # recover, observation lease, risk instant, submit lease, immediate pre-send
    instants = [
        NOW,
        NOW,
        NOW,
        NOW,
        NOW + timedelta(seconds=6),
        NOW + timedelta(seconds=6),
    ]
    session.clock = lambda: instants.pop(0)
    try:
        with pytest.raises(PlatformError, match="HOST_PRE_SEND_EVIDENCE_EXPIRED"):
            session.dispatch(envelope, target, LIMITS)
        assert gateway.submit_calls == 0
        assert (
            session.store.read(envelope.submission_id, target)[0].state
            == "NO_EFFECT_CONFIRMED"
        )
    finally:
        session.close()
        operator.close()


def test_two_second_vault_latency_with_fresh_facts_can_dispatch_paper(
    tmp_path: Path,
) -> None:
    session, envelope, target, gateway, _, _, operator, *_ = fixture(tmp_path)
    instants = [NOW, NOW, NOW, NOW + timedelta(seconds=2)] + [
        NOW + timedelta(seconds=2)
    ] * 10
    session.clock = lambda: instants.pop(0)

    def submit(e: Any, observation: Any, lease: Any) -> BrokerSubmissionOutcome:
        assert (
            lease.reveal(evaluation_time=NOW + timedelta(seconds=2)).as_bytes()
            == b"OFFLINE-PAPER-SECRET"
        )
        return BrokerSubmissionOutcome(submission_id=e.submission_id, status="ACCEPTED")

    gateway.submit = submit  # type: ignore[method-assign]
    try:
        assert session.dispatch(envelope, target, LIMITS)["state"] == "ACKNOWLEDGED"
        row = session.account_checks.read()[0]
        assert row["lot_size"] == 100 and row["estimated_fee"] == "5"
        assert row["observed_rule_hash"] == "c" * 64
    finally:
        session.close()
        operator.close()


def test_unknown_cancel_capability_and_bad_risk_limit_never_send(
    tmp_path: Path,
) -> None:
    session, envelope, target, gateway, _, _, operator, *_ = fixture(tmp_path)
    try:
        restrictive = RiskLimits(
            max_order_notional=Decimal("10"),
            max_position_notional=Decimal("10"),
            max_gross_exposure=Decimal("10"),
        )
        with pytest.raises(PlatformError, match="CORE_FRESH_RISK"):
            session.dispatch(envelope, target, restrictive)
        assert gateway.submit_calls == 0
        session.dispatch(envelope, target, LIMITS)
        gateway.cancel_capability = lambda t, lease: BrokerCancelCapability(
            t, False, NOW, "d" * 64
        )  # type: ignore[method-assign]
        with pytest.raises(PlatformError, match="CAPABILITY_UNKNOWN"):
            session.cancel(envelope.submission_id, target)
        assert gateway.cancel_calls == 0
    finally:
        session.close()
        operator.close()


@pytest.mark.parametrize(
    "change",
    [
        {"generation": 999},
        {"recovery_epoch": 999},
        {"observed_at": NOW - timedelta(seconds=1)},
    ],
)
def test_reconcile_pins_current_attempt_and_timestamp_before_recording(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    session, envelope, target, gateway, _, _, operator, *_ = fixture(tmp_path)
    gateway.unknown = True
    original = gateway.query_attempt
    try:
        session.dispatch(envelope, target, LIMITS)
        gateway.query_attempt = lambda *args: replace(original(*args), **change)  # type: ignore[method-assign]
        with pytest.raises(PlatformError, match="TARGET_OR_FRESHNESS"):
            session.reconcile(envelope.submission_id, target)
        assert (
            session.store.read(envelope.submission_id, target)[0].state
            == "OUTCOME_UNKNOWN"
        )
        view = session.ledger.read_attempt(envelope.submission_id, target)
        assert session.ledger.read_observations(view.attempt_id) == ()
    finally:
        session.close()
        operator.close()


def test_expected_portfolio_mismatch_and_snapshot_mutation_are_denied(
    tmp_path: Path,
) -> None:
    session, envelope, target, gateway, _, _, operator, *_ = fixture(tmp_path)
    try:
        with pytest.raises(TypeError):
            gateway.observation.prices["FIXTURE"] = Decimal("0.1")  # type: ignore[index]
        gateway.observation = replace(
            gateway.observation,
            portfolio=PortfolioState(initial_cash=Decimal("99"), cash=Decimal("99")),
        )
        with pytest.raises(PlatformError, match="ACCOUNT_RECONCILIATION"):
            session.dispatch(envelope, target, LIMITS)
        assert gateway.submit_calls == 0
    finally:
        session.close()
        operator.close()


def test_contradictory_query_is_visible_without_committing_false_observation(
    tmp_path: Path,
) -> None:
    session, envelope, target, gateway, _, _, operator, *_ = fixture(tmp_path)
    original = gateway.query_attempt
    try:
        session.dispatch(envelope, target, LIMITS)
        gateway.query_attempt = lambda *args: replace(
            original(*args), status=BrokerObservationStatus.REJECTED
        )  # type: ignore[method-assign]
        result = session.reconcile(envelope.submission_id, target)
        assert result["reconciliation"] == "UNKNOWN"
        assert result["classification"] == "BROKER_LOCAL_STATE_CONFLICT"
        assert (
            session.store.read(envelope.submission_id, target)[0].state
            == "ACKNOWLEDGED"
        )
        attempt = session.ledger.read_attempt(envelope.submission_id, target)
        assert session.ledger.read_observations(attempt.attempt_id) == ()
    finally:
        session.close()
        operator.close()


def test_paused_reconciliation_remains_available_and_missing_attempt_is_bounded(
    tmp_path: Path,
) -> None:
    session, envelope, target, gateway, state, _, operator, *_ = fixture(tmp_path)
    try:
        with pytest.raises(PlatformError, match="CORE_BOUNDARY"):
            session.reconcile(envelope.submission_id, target)
        gateway.unknown = True
        session.dispatch(envelope, target, LIMITS)
        state["paused"] = True
        assert (
            session.reconcile(envelope.submission_id, target)["state"] == "ACKNOWLEDGED"
        )
        with pytest.raises(PlatformError, match="PAUSED"):
            session.cancel(envelope.submission_id, target)
    finally:
        session.close()
        operator.close()
