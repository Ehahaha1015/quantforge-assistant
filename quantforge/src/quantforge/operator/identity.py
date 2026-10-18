"""Deterministic OPERATOR-001 identity derivation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..frontier import ExecutionTargetBinding
from .canonical import (
    canonical_instant,
    canonical_json,
    digest32,
    duration_microseconds,
    sha256_hex_utf8,
)
from .models import (
    ControlAction,
    ControlState,
    OperatorControlRequest,
    OperatorControlScope,
)


def scope_payload(scope: OperatorControlScope) -> dict[str, Any]:
    return {
        "broker": scope.broker,
        "environment": scope.environment,
        "account_reference": scope.account_reference,
        "adapter_id": scope.adapter_id,
        "adapter_version": scope.adapter_version,
    }


def target_payload(target: ExecutionTargetBinding) -> dict[str, Any]:
    return {
        "schema": target.schema,
        "broker": target.broker,
        "environment": target.environment,
        "account_reference": target.account_reference,
        "adapter_id": target.adapter_id,
        "adapter_version": target.adapter_version,
        "submission_id": target.submission_id,
    }


def action_payload(request: OperatorControlRequest) -> dict[str, Any]:
    return {
        "action": request.action.value,
        "request_id": request.request_id,
        "scope": scope_payload(request.scope),
        "operator_reference": request.operator_reference,
        "control_source": request.control_source,
        "observed_at": canonical_instant(request.observed_at, name="observed_at"),
        "maximum_age_microseconds": duration_microseconds(
            request.maximum_age, name="maximum_age"
        ),
    }


def state_payload(
    *,
    action_id: str,
    scope: OperatorControlScope,
    state: ControlState,
    observed_at: datetime,
    maximum_age: timedelta,
) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "scope": scope_payload(scope),
        "state": state.value,
        "observed_at": canonical_instant(observed_at, name="observed_at"),
        "maximum_age_microseconds": duration_microseconds(
            maximum_age, name="maximum_age"
        ),
    }


def audit_payload(
    *,
    control_sequence: int,
    action_id: str,
    request_id: str,
    scope: OperatorControlScope,
    requested_action: ControlAction,
    resulting_state: ControlState,
    state_version_id: str,
    operator_reference: str,
    control_source: str,
    verifier_id: str,
    proof_id: str,
    verified_at: datetime,
    observed_at: datetime,
    maximum_age: timedelta,
    committed_at: datetime,
    request_commitment: str,
) -> dict[str, Any]:
    return {
        "control_sequence": control_sequence,
        "action_id": action_id,
        "request_id": request_id,
        "scope": scope_payload(scope),
        "requested_action": requested_action.value,
        "resulting_state": resulting_state.value,
        "state_version_id": state_version_id,
        "operator_reference": operator_reference,
        "control_source": control_source,
        "verifier_id": verifier_id,
        "proof_id": proof_id,
        "verified_at": canonical_instant(verified_at, name="verified_at"),
        "observed_at": canonical_instant(observed_at, name="observed_at"),
        "maximum_age_microseconds": duration_microseconds(
            maximum_age, name="maximum_age"
        ),
        "committed_at": canonical_instant(committed_at, name="committed_at"),
        "request_commitment": request_commitment,
    }


def evidence_payload(
    *,
    state_version_id: str,
    target: ExecutionTargetBinding,
    action_id: str,
    enabled: bool,
    observed_at: datetime,
    maximum_age: timedelta,
) -> dict[str, Any]:
    return {
        "state_version_id": state_version_id,
        "target": target_payload(target),
        "action_id": action_id,
        "enabled": enabled,
        "observed_at": canonical_instant(observed_at, name="observed_at"),
        "maximum_age_microseconds": duration_microseconds(
            maximum_age, name="maximum_age"
        ),
    }


def scope_key(scope: OperatorControlScope) -> str:
    return "opctl-scope-v1-" + digest32(canonical_json(scope_payload(scope)))


def action_id(request: OperatorControlRequest) -> str:
    return "opctl-action-v1-" + digest32(canonical_json(action_payload(request)))


def state_version_id(
    *,
    action_id_value: str,
    scope: OperatorControlScope,
    state: ControlState,
    observed_at: datetime,
    maximum_age: timedelta,
) -> str:
    return "opctl-state-v1-" + digest32(
        canonical_json(
            state_payload(
                action_id=action_id_value,
                scope=scope,
                state=state,
                observed_at=observed_at,
                maximum_age=maximum_age,
            )
        )
    )


def audit_id(
    *,
    control_sequence: int,
    action_id_value: str,
    request_id: str,
    scope: OperatorControlScope,
    requested_action: ControlAction,
    resulting_state: ControlState,
    state_version_id_value: str,
    operator_reference: str,
    control_source: str,
    verifier_id: str,
    proof_id: str,
    verified_at: datetime,
    observed_at: datetime,
    maximum_age: timedelta,
    committed_at: datetime,
    request_commitment: str,
) -> str:
    return "opctl-audit-v1-" + digest32(
        canonical_json(
            audit_payload(
                control_sequence=control_sequence,
                action_id=action_id_value,
                request_id=request_id,
                scope=scope,
                requested_action=requested_action,
                resulting_state=resulting_state,
                state_version_id=state_version_id_value,
                operator_reference=operator_reference,
                control_source=control_source,
                verifier_id=verifier_id,
                proof_id=proof_id,
                verified_at=verified_at,
                observed_at=observed_at,
                maximum_age=maximum_age,
                committed_at=committed_at,
                request_commitment=request_commitment,
            )
        )
    )


def evidence_id(
    *,
    state_version_id_value: str,
    target: ExecutionTargetBinding,
    action_id_value: str,
    enabled: bool,
    observed_at: datetime,
    maximum_age: timedelta,
) -> str:
    return "opctl-evidence-v1-" + digest32(
        canonical_json(
            evidence_payload(
                state_version_id=state_version_id_value,
                target=target,
                action_id=action_id_value,
                enabled=enabled,
                observed_at=observed_at,
                maximum_age=maximum_age,
            )
        )
    )


def request_commitment(request: OperatorControlRequest) -> str:
    return "sha256:" + sha256_hex_utf8(
        canonical_json(action_payload(request))
    )


__all__ = [
    "action_id",
    "action_payload",
    "audit_id",
    "audit_payload",
    "evidence_id",
    "evidence_payload",
    "request_commitment",
    "scope_key",
    "scope_payload",
    "state_payload",
    "state_version_id",
    "target_payload",
]
