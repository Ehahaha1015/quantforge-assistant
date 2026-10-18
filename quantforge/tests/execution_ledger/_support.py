from __future__ import annotations

from datetime import UTC, datetime

from quantforge.execution_ledger import (
    BrokerObservationStatus,
    BrokerOrderObservation,
)
from quantforge.frontier import ExecutionTargetBinding

SUBMISSION_ID = "broker1-" + "a" * 32

TARGET_JSON = (
    '{"account_reference":"acct-7","adapter_id":"adapter-x",'
    '"adapter_version":"1","broker":"broker-x","environment":"SANDBOX",'
    '"schema":"frontier-target-v1","submission_id":"'
    + SUBMISSION_ID
    + '"}'
)

GOLDEN_ATTEMPT_ID = "execstate-attempt-v1-07b50e34a2bf6e4bb036583bb20a7bd1"

OBSERVED_AT = datetime(2030, 1, 1, 12, 2, 1, tzinfo=UTC)


def make_target(
    submission_id: str = SUBMISSION_ID,
    *,
    account_reference: str = "acct-7",
    adapter_id: str = "adapter-x",
    adapter_version: str = "1",
) -> ExecutionTargetBinding:
    return ExecutionTargetBinding(
        schema="frontier-target-v1",
        broker="broker-x",
        environment="SANDBOX",
        account_reference=account_reference,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        submission_id=submission_id,
    )


def make_observation(
    *,
    submission_id: str = SUBMISSION_ID,
    target: ExecutionTargetBinding | None = None,
    generation: int = 1,
    recovery_epoch: int = 1,
    status: BrokerObservationStatus = BrokerObservationStatus.ACCEPTED,
    broker_reference: str | None = "broker-ref-1",
    broker_message: str | None = None,
    observed_at: datetime = OBSERVED_AT,
    observation_id: str | None = None,
) -> BrokerOrderObservation:
    return BrokerOrderObservation(
        submission_id=submission_id,
        target=target if target is not None else make_target(submission_id),
        generation=generation,
        recovery_epoch=recovery_epoch,
        status=status,
        broker_reference=broker_reference,
        broker_message=broker_message,
        observed_at=observed_at,
        observation_id=observation_id,
    )
