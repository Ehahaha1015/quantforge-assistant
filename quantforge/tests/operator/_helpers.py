"""Deterministic test helpers for OPERATOR-001."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from quantforge.frontier import ExecutionTargetBinding
from quantforge.operator import (
    ControlAction,
    OperatorAuthenticationProof,
    OperatorControlPolicy,
    OperatorControlRequest,
    OperatorControlScope,
    VerifiedOperatorIdentity,
)
from quantforge.operator.identity import request_commitment

OBSERVED_AT = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
COMMITTED_AT = datetime(2030, 1, 1, 12, 0, 2, tzinfo=UTC)


class AcceptingVerifier:
    """Deterministic local verifier double used only in tests."""

    @property
    def verifier_id(self) -> str:
        return "operator-test-verifier-v1"

    def verify(
        self,
        request: OperatorControlRequest,
        proof: OperatorAuthenticationProof,
    ) -> VerifiedOperatorIdentity:
        return VerifiedOperatorIdentity(
            schema="operator-verified-v1",
            operator_reference=request.operator_reference,
            control_source=request.control_source,
            verifier_id=self.verifier_id,
            verified_at=request.observed_at + timedelta(seconds=1),
            proof_id=proof.proof_id,
        )


class RejectingDouble:
    @property
    def verifier_id(self) -> str:
        return "operator-test-rejecting-v1"

    def verify(self, request: object, proof: object) -> VerifiedOperatorIdentity:
        from quantforge.operator import OperatorAuthenticationError

        raise OperatorAuthenticationError("test rejection")


def make_policy(
    bound: timedelta = timedelta(minutes=5),
) -> OperatorControlPolicy:
    return OperatorControlPolicy(
        schema="operator-policy-v1",
        maximum_age_upper_bound=bound,
    )


def make_scope(
    *,
    broker: str = "broker-x",
    environment: str = "SANDBOX",
    account_reference: str = "acct-7",
    adapter_id: str = "adapter-x",
    adapter_version: str = "1",
) -> OperatorControlScope:
    return OperatorControlScope(
        schema="operator-scope-v1",
        broker=broker,
        environment=environment,  # type: ignore[arg-type]
        account_reference=account_reference,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
    )


def make_target(
    *,
    submission_id: str = "broker1-cd2ee7ab6550b22073eb4317083ff179",
) -> ExecutionTargetBinding:
    return ExecutionTargetBinding(
        schema="frontier-target-v1",
        broker="broker-x",
        environment="SANDBOX",
        account_reference="acct-7",
        adapter_id="adapter-x",
        adapter_version="1",
        submission_id=submission_id,
    )


def make_request(
    *,
    request_id: str = "request-1",
    action: ControlAction = ControlAction.ENABLE,
    scope: OperatorControlScope | None = None,
    operator_reference: str = "operator-1",
    control_source: str = "control-source-1",
    observed_at: datetime = OBSERVED_AT,
    maximum_age: timedelta = timedelta(minutes=1),
) -> OperatorControlRequest:
    return OperatorControlRequest(
        schema="operator-request-v1",
        request_id=request_id,
        action=action,
        scope=scope or make_scope(),
        operator_reference=operator_reference,
        control_source=control_source,
        observed_at=observed_at,
        maximum_age=maximum_age,
    )


def make_proof(
    request: OperatorControlRequest,
    *,
    proof_id: str = "proof-1",
) -> OperatorAuthenticationProof:
    return OperatorAuthenticationProof(
        schema="operator-proof-v1",
        proof_id=proof_id,
        operator_reference=request.operator_reference,
        control_source=request.control_source,
        action=request.action,
        scope=request.scope,
        method="LOCAL_VERIFIER",
        request_commitment=request_commitment(request),
    )


def store_path(tmp_path: Path) -> Path:
    return tmp_path / "operator.sqlite"
