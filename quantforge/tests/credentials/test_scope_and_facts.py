"""Safe-value oracles: scope, reference, facts, binding and determinism."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from _credential_fixtures import (
    CANARY,
    EXPIRES_AT,
    GENERATION,
    PROVIDER_ID,
    REQUESTED_AT,
    canonical_facts_id,
    canonical_json,
    facts_payload,
    make_facts,
    make_reference,
    make_scope,
    make_target,
    package_frames_containing_canary,
)
from pydantic import ValidationError

from quantforge.credentials import (
    CredentialArgumentError,
    CredentialPurpose,
    CredentialReference,
    CredentialScope,
)

SCOPE_FIELDS = ("provider", "backend", "account_reference", "consumer")


def test_scope_requires_every_field_with_no_default() -> None:
    with pytest.raises(CredentialArgumentError):
        CredentialScope(
            provider=PROVIDER_ID,
            backend="broker-main",
            environment="PAPER",
            account_reference="acct-001",
            consumer="broker-adapter",
            purpose=CredentialPurpose.ORDER_SUBMIT,
        )


@pytest.mark.parametrize("field", SCOPE_FIELDS)
@pytest.mark.parametrize(
    "bad", ["", "_leading", "has space", "a" * 129, "semi;colon", 5, None]
)
def test_oracle_p_malformed_scope_fields_fail_closed(
    field: str, bad: object
) -> None:
    fields: dict[str, object] = {
        "provider": PROVIDER_ID,
        "backend": "broker-main",
        "environment": "PAPER",
        "account_reference": "acct-001",
        "consumer": "broker-adapter",
        "purpose": CredentialPurpose.ORDER_SUBMIT,
    }
    fields[field] = bad
    with pytest.raises(CredentialArgumentError):
        CredentialScope(schema="credential-scope-v1", **fields)


def test_scope_is_strict_and_extra_forbidding() -> None:
    with pytest.raises(CredentialArgumentError):
        make_scope(environment="paper")
    with pytest.raises(CredentialArgumentError):
        make_scope(environment="TEST")
    with pytest.raises(CredentialArgumentError):
        CredentialScope(
            schema="credential-scope-v1",
            provider=PROVIDER_ID,
            backend="broker-main",
            environment="PAPER",
            account_reference="acct-001",
            consumer="broker-adapter",
            purpose=CredentialPurpose.ORDER_SUBMIT,
            extra_field="not-frozen",
        )
    with pytest.raises(CredentialArgumentError):
        CredentialScope(
            schema="credential-scope-v2",
            provider=PROVIDER_ID,
            backend="broker-main",
            environment="PAPER",
            account_reference="acct-001",
            consumer="broker-adapter",
            purpose=CredentialPurpose.ORDER_SUBMIT,
        )


def test_purpose_is_closed_flat_and_independent() -> None:
    purposes = list(CredentialPurpose)
    assert [purpose.value for purpose in purposes] == [
        "MARKET_DATA_READ",
        "ACCOUNT_READ",
        "ORDER_SUBMIT",
        "ORDER_QUERY",
        "ORDER_CANCEL",
    ]
    scopes = [make_scope(purpose=purpose) for purpose in purposes]
    for index, scope in enumerate(scopes):
        for other_index, other in enumerate(scopes):
            assert (scope == other) is (index == other_index)
    with pytest.raises(CredentialArgumentError):
        make_scope(purpose="ORDER_SUBMIT_EXTRA")


def test_scope_equality_is_exact_field_equality() -> None:
    base = make_scope()
    assert base == make_scope()
    differences = [
        make_scope(provider="other-provider-v1"),
        make_scope(backend="other-broker"),
        make_scope(environment="SANDBOX"),
        make_scope(account_reference="acct-002"),
        make_scope(consumer="other-consumer"),
        make_scope(purpose=CredentialPurpose.ORDER_CANCEL),
    ]
    for other in differences:
        assert base != other


def test_safe_values_are_frozen() -> None:
    scope = make_scope()
    reference = make_reference()
    with pytest.raises(ValidationError):
        scope.provider = "other-provider-v1"
    with pytest.raises(ValidationError):
        reference.value = "other-ref"


@pytest.mark.parametrize(
    "bad", ["", "_leading", "with space", "a" * 129, 5, None, ["values"]]
)
def test_oracle_o_malformed_reference_fails_closed(bad: object) -> None:
    with pytest.raises(CredentialArgumentError):
        CredentialReference(value=bad)


def test_canary_is_rejected_through_input_channels_without_echo() -> None:
    attempts = [
        lambda: make_scope(provider=CANARY),
        lambda: make_scope(backend=CANARY),
        lambda: make_scope(account_reference=CANARY),
        lambda: make_scope(consumer=CANARY),
        lambda: make_reference(CANARY),
    ]
    for attempt in attempts:
        with pytest.raises(CredentialArgumentError) as info:
            attempt()
        error = info.value
        assert CANARY not in str(error)
        assert CANARY not in repr(error)
        assert CANARY not in repr(error.args)
        assert error.__cause__ is None
        assert error.__context__ is None
        assert package_frames_containing_canary(error) == []


def test_oracle_d_from_target_copies_the_frozen_fields() -> None:
    target = make_target()
    scope = CredentialScope.from_target(
        target,
        provider=PROVIDER_ID,
        consumer="broker-adapter",
        purpose=CredentialPurpose.ORDER_SUBMIT,
    )
    assert scope.provider == PROVIDER_ID
    assert scope.backend == target.broker
    assert scope.environment == target.environment
    assert scope.account_reference == target.account_reference
    assert scope.consumer == "broker-adapter"
    assert scope.purpose is CredentialPurpose.ORDER_SUBMIT


def test_from_target_excludes_adapter_and_submission_identity() -> None:
    first = make_target(
        adapter_id="adapter-a",
        adapter_version="v1",
        submission_id="broker1-" + "1" * 32,
    )
    second = make_target(
        adapter_id="adapter-b",
        adapter_version="v9",
        submission_id="broker1-" + "2" * 32,
    )
    scope_first = CredentialScope.from_target(
        first,
        provider=PROVIDER_ID,
        consumer="broker-adapter",
        purpose=CredentialPurpose.ORDER_SUBMIT,
    )
    scope_second = CredentialScope.from_target(
        second,
        provider=PROVIDER_ID,
        consumer="broker-adapter",
        purpose=CredentialPurpose.ORDER_SUBMIT,
    )
    assert scope_first == scope_second
    payload = canonical_json(scope_first.model_dump(mode="json"))
    for field in ("adapter_id", "adapter_version", "submission_id"):
        assert field not in payload
    for value in ("adapter-a", "adapter-b", "v9", "1" * 32, "2" * 32):
        assert value not in payload


def test_oracle_e_from_target_rejects_market_data_purpose() -> None:
    with pytest.raises(CredentialArgumentError):
        CredentialScope.from_target(
            make_target(),
            provider=PROVIDER_ID,
            consumer="broker-adapter",
            purpose=CredentialPurpose.MARKET_DATA_READ,
        )


def test_from_target_rejects_non_binding_targets() -> None:
    with pytest.raises(CredentialArgumentError):
        CredentialScope.from_target(
            {"broker": "broker-main"},
            provider=PROVIDER_ID,
            consumer="broker-adapter",
            purpose=CredentialPurpose.ORDER_SUBMIT,
        )


def test_matches_is_exact_and_rejects_non_bindings() -> None:
    target = make_target()
    scope = CredentialScope.from_target(
        target,
        provider=PROVIDER_ID,
        consumer="broker-adapter",
        purpose=CredentialPurpose.ORDER_SUBMIT,
    )
    assert scope.matches(target) is True
    assert scope.matches(make_target(broker="other-broker")) is False
    assert scope.matches(make_target(environment="SANDBOX")) is False
    assert scope.matches(make_target(account_reference="acct-002")) is False
    assert scope.matches(None) is False
    assert scope.matches({"broker": "broker-main"}) is False


def test_facts_identity_is_deterministic_and_secret_independent() -> None:
    scope, reference = make_scope(), make_reference()
    first = canonical_facts_id(scope, reference)
    second = canonical_facts_id(scope, reference)
    assert first == second
    assert first.startswith("credential-facts-v1-")
    assert len(first) == len("credential-facts-v1-") + 64
    assert make_facts(scope, reference).facts_id == first
    assert canonical_facts_id(make_scope(consumer="other"), reference) != first
    assert canonical_facts_id(scope, make_reference("other-ref")) != first
    assert canonical_facts_id(scope, reference, generation=GENERATION + 1) != first
    assert (
        canonical_facts_id(
            scope, reference, expires_at=EXPIRES_AT + timedelta(seconds=1)
        )
        != first
    )
    assert (
        canonical_facts_id(
            scope,
            reference,
            acquired_at=REQUESTED_AT + timedelta(seconds=1),
        )
        != first
    )
    assert CANARY not in canonical_json(facts_payload(scope, reference))


def test_facts_model_rejects_malformed_fields() -> None:
    scope, reference = make_scope(), make_reference()
    valid_id = canonical_facts_id(scope, reference)
    with pytest.raises(CredentialArgumentError):
        make_facts(scope, reference, facts_id="credential-facts-v1-not-hex")
    with pytest.raises(CredentialArgumentError):
        make_facts(scope, reference, generation=0)
    with pytest.raises(CredentialArgumentError):
        make_facts(scope, reference, generation=True)
    with pytest.raises(CredentialArgumentError):
        make_facts(
            scope,
            reference,
            facts_id=valid_id,
            acquired_at=datetime(2030, 1, 1, 9, 30),  # noqa: DTZ001
        )
    with pytest.raises(CredentialArgumentError):
        make_facts(scope, reference, provider="_malformed")


def test_oracle_ac_facts_identity_is_hash_seed_independent() -> None:
    script = Path(__file__).with_name("_facts_identity_probe.py")
    expected = canonical_facts_id(make_scope(), make_reference())
    for seed in ("0", "12345"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, str(script)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=environment,
        )
        lines = completed.stdout.strip().splitlines()
        assert lines[0] == expected
        assert lines[1] == canonical_json(facts_payload(make_scope(), make_reference()))


def test_canonical_instants_are_utc_normalized() -> None:
    from _credential_fixtures import canonical_instant

    moment = datetime(2030, 1, 1, 17, 30, tzinfo=UTC)
    assert canonical_instant(moment) == "2030-01-01T17:30:00.000000Z"
    shifted = moment.astimezone(timezone(timedelta(hours=8)))
    assert canonical_instant(shifted) == "2030-01-01T17:30:00.000000Z"
