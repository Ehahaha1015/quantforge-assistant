"""Acquisition oracles A..V and AF/AG for the deterministic boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import pytest
from _credential_fixtures import (
    CANARY_BYTES,
    EXPIRES_AT,
    GENERATION,
    PROVIDER_ID,
    REJECTING_PROVIDER_ID,
    REQUESTED_AT,
    RecordingBackend,
    canonical_facts_id,
    make_facts,
    make_reference,
    make_scope,
)

import quantforge.credentials as credentials_package
from quantforge.credentials import (
    CredentialArgumentError,
    CredentialBoundary,
    CredentialFacts,
    CredentialLease,
    CredentialLeaseError,
    CredentialLeaseExpiredError,
    CredentialProviderError,
    CredentialProviderIdentityError,
    CredentialPurpose,
    CredentialReference,
    CredentialRejectedError,
    CredentialScope,
    CredentialScopeMismatchError,
    CredentialUnavailableError,
)


def _acquire(
    boundary: CredentialBoundary,
    scope: Any = None,
    reference: Any = None,
    requested_at: Any = REQUESTED_AT,
) -> CredentialLease:
    resolved_scope = make_scope() if scope is None else scope
    resolved_reference = make_reference() if reference is None else reference
    return boundary.acquire(
        resolved_scope, resolved_reference, requested_at=requested_at
    )


def test_oracle_a_rejecting_default_fails_closed() -> None:
    boundary = CredentialBoundary()
    scope = make_scope(provider=REJECTING_PROVIDER_ID)
    with pytest.raises(CredentialUnavailableError) as info:
        _acquire(boundary, scope=scope)
    assert info.value.args == ("no credential backend is installed",)


def test_oracle_b_positive_scoped_acquisition() -> None:
    backend = RecordingBackend()
    scope, reference = make_scope(), make_reference()
    lease = CredentialBoundary(backend=backend).acquire(
        scope, reference, requested_at=REQUESTED_AT
    )
    assert isinstance(lease, CredentialLease)
    assert lease.facts.scope == scope
    assert lease.facts.reference == reference
    assert lease.facts.provider == PROVIDER_ID
    assert lease.facts.acquired_at == REQUESTED_AT
    assert lease.facts.expires_at == EXPIRES_AT
    assert lease.facts.generation == GENERATION
    material = lease.reveal(evaluation_time=REQUESTED_AT)
    assert material.as_bytes() == CANARY_BYTES
    assert backend.acquire_calls == 1
    assert backend.provider_id_reads == 1


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("backend", "other-broker"),
        ("environment", "SANDBOX"),
        ("account_reference", "acct-002"),
        ("consumer", "other-consumer"),
        ("purpose", CredentialPurpose.ORDER_QUERY),
    ],
)
def test_oracle_c_i_m_wrong_scope_fields_fail_closed(
    mutation: str, value: object
) -> None:
    scope, reference = make_scope(), make_reference()
    served_fields: dict[str, object] = {
        "provider": scope.provider,
        "backend": scope.backend,
        "environment": scope.environment,
        "account_reference": scope.account_reference,
        "consumer": scope.consumer,
        "purpose": scope.purpose,
    }
    served_fields[mutation] = value
    served_scope = make_scope(**served_fields)
    facts = make_facts(
        served_scope,
        reference,
        acquired_at=REQUESTED_AT,
        expires_at=EXPIRES_AT,
    )
    backend = RecordingBackend(result=(facts, CANARY_BYTES))
    with pytest.raises(CredentialScopeMismatchError):
        CredentialBoundary(backend=backend).acquire(
            scope, reference, requested_at=REQUESTED_AT
        )
    assert backend.acquire_calls == 1


def test_oracle_f_non_conforming_backends_fail_before_any_call() -> None:
    scope, reference = make_scope(), make_reference()

    class NonCallableAcquire:
        provider_id = PROVIDER_ID
        acquire = "not-callable"

    class MissingProviderId:
        def acquire(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("must never be called")

    for backend in (None, object(), 5, NonCallableAcquire(), MissingProviderId()):
        with pytest.raises(CredentialArgumentError):
            CredentialBoundary(backend=backend).acquire(
                scope, reference, requested_at=REQUESTED_AT
            )


@pytest.mark.parametrize("bad", ["", "_leading", "has space", 5, None])
def test_oracle_g_malformed_provider_identity(bad: object) -> None:
    backend = RecordingBackend(provider_id=bad)
    with pytest.raises(CredentialProviderIdentityError):
        _acquire(CredentialBoundary(backend=backend))
    assert backend.acquire_calls == 0


def test_oracle_h_provider_contradiction_before_any_call() -> None:
    backend = RecordingBackend(provider_id="other-provider-v1")
    with pytest.raises(CredentialProviderIdentityError) as info:
        _acquire(CredentialBoundary(backend=backend))
    assert info.value.args
    assert backend.acquire_calls == 0


def test_wrong_provider_in_returned_facts_fails_closed() -> None:
    scope, reference = make_scope(), make_reference()
    facts = make_facts(
        scope,
        reference,
        provider="other-provider-v1",
        acquired_at=REQUESTED_AT,
        expires_at=EXPIRES_AT,
    )
    backend = RecordingBackend(result=(facts, CANARY_BYTES))
    with pytest.raises(CredentialProviderIdentityError):
        CredentialBoundary(backend=backend).acquire(
            scope, reference, requested_at=REQUESTED_AT
        )


def test_oracle_n_reference_substitution_fails_closed() -> None:
    scope = make_scope()
    requested = make_reference("deploy-ref-001")
    served = make_reference("deploy-ref-002")
    facts = make_facts(
        scope, served, acquired_at=REQUESTED_AT, expires_at=EXPIRES_AT
    )
    backend = RecordingBackend(result=(facts, CANARY_BYTES))
    with pytest.raises(CredentialProviderError) as info:
        CredentialBoundary(backend=backend).acquire(
            scope, requested, requested_at=REQUESTED_AT
        )
    assert type(info.value) is CredentialProviderError


def test_oracle_q_malformed_instants_fail_before_any_call() -> None:
    class OutOfRangeTZ(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta:
            return timedelta(hours=25)

        def dst(self, dt: datetime | None) -> timedelta:
            return timedelta(0)

        def tzname(self, dt: datetime | None) -> str:
            return "OUT-OF-RANGE"

    backend = RecordingBackend()
    boundary = CredentialBoundary(backend=backend)
    with pytest.raises(CredentialArgumentError):
        _acquire(
            boundary,
            requested_at=datetime(2030, 1, 1, 9, 30),  # noqa: DTZ001
        )
    with pytest.raises(CredentialArgumentError):
        _acquire(
            boundary,
            requested_at=datetime(2030, 1, 1, 9, 30, tzinfo=OutOfRangeTZ()),
        )
    assert backend.acquire_calls == 0
    with pytest.raises(TypeError):
        _acquire(boundary, requested_at="2030-01-01T09:30:00Z")
    assert backend.acquire_calls == 0


@pytest.mark.parametrize(
    "error",
    [
        CredentialUnavailableError("missing"),
        CredentialRejectedError("refused"),
        CredentialProviderIdentityError("bad identity"),
    ],
)
def test_oracle_s_typed_backend_errors_propagate_unchanged(
    error: CredentialProviderError,
) -> None:
    backend = RecordingBackend(error=error)
    with pytest.raises(type(error)) as info:
        _acquire(CredentialBoundary(backend=backend))
    assert info.value is error
    assert backend.acquire_calls == 1


def test_oracle_t_non_exception_baseexception_propagates_unchanged() -> None:
    class Interrupt(BaseException):
        pass

    error = Interrupt("interrupted")
    backend = RecordingBackend(error=error)
    with pytest.raises(Interrupt) as info:
        _acquire(CredentialBoundary(backend=backend))
    assert info.value is error
    assert backend.acquire_calls == 1


def test_oracle_u_exact_call_counts_and_no_retry() -> None:
    success = RecordingBackend()
    _acquire(CredentialBoundary(backend=success))
    assert success.acquire_calls == 1

    failing = RecordingBackend(error=RuntimeError("boom"))
    with pytest.raises(CredentialProviderError):
        _acquire(CredentialBoundary(backend=failing))
    assert failing.acquire_calls == 1

    pre_call = RecordingBackend()
    with pytest.raises(TypeError):
        CredentialBoundary(backend=pre_call).acquire(
            {"not": "a scope"}, make_reference(), requested_at=REQUESTED_AT
        )
    assert pre_call.acquire_calls == 0


def test_oracle_v_tampered_facts_identity_fails_closed() -> None:
    scope, reference = make_scope(), make_reference()
    facts = make_facts(
        scope,
        reference,
        acquired_at=REQUESTED_AT,
        expires_at=EXPIRES_AT,
        facts_id="credential-facts-v1-" + "0" * 64,
    )
    assert facts.facts_id != canonical_facts_id(scope, reference)
    backend = RecordingBackend(result=(facts, CANARY_BYTES))
    with pytest.raises(CredentialProviderError) as info:
        CredentialBoundary(backend=backend).acquire(
            scope, reference, requested_at=REQUESTED_AT
        )
    assert type(info.value) is CredentialProviderError


@pytest.mark.parametrize(
    "result",
    [
        None,
        "not-a-tuple",
        (),
        (object(),),
        (object(), CANARY_BYTES, "extra"),
    ],
)
def test_malformed_backend_results_fail_closed(result: object) -> None:
    backend = RecordingBackend(result=result)
    with pytest.raises(CredentialProviderError):
        _acquire(CredentialBoundary(backend=backend))


def test_backend_facts_must_be_exactly_credential_facts() -> None:
    class LookalikeFacts:
        provider = PROVIDER_ID
        facts_id = "credential-facts-v1-" + "a" * 64

    backend = RecordingBackend(result=(LookalikeFacts(), CANARY_BYTES))
    with pytest.raises(CredentialProviderError):
        _acquire(CredentialBoundary(backend=backend))


def test_facts_subclass_is_rejected_by_the_exact_type_check() -> None:
    class FactsSubclass(CredentialFacts):
        pass

    scope, reference = make_scope(), make_reference()
    valid = make_facts(scope, reference, acquired_at=REQUESTED_AT)
    subclass_facts = FactsSubclass(
        schema=valid.schema,
        facts_id=valid.facts_id,
        provider=valid.provider,
        scope=valid.scope,
        reference=valid.reference,
        generation=valid.generation,
        acquired_at=valid.acquired_at,
        expires_at=valid.expires_at,
    )
    backend = RecordingBackend(result=(subclass_facts, CANARY_BYTES))
    with pytest.raises(CredentialProviderError):
        CredentialBoundary(backend=backend).acquire(
            scope, reference, requested_at=REQUESTED_AT
        )


@pytest.mark.parametrize("material", ["", b"", 5, None, ["bytes"]])
def test_empty_or_non_bytes_material_fails_closed(material: object) -> None:
    facts = make_facts(
        make_scope(),
        make_reference(),
        acquired_at=REQUESTED_AT,
        expires_at=EXPIRES_AT,
    )
    backend = RecordingBackend(result=(facts, material))
    with pytest.raises(CredentialProviderError):
        _acquire(CredentialBoundary(backend=backend))


def test_oracle_w_lease_window_boundaries() -> None:
    lease = _acquire(CredentialBoundary(backend=RecordingBackend()))
    assert lease.reveal(evaluation_time=REQUESTED_AT).as_bytes() == CANARY_BYTES
    assert (
        lease.reveal(
            evaluation_time=EXPIRES_AT - timedelta(microseconds=1)
        ).as_bytes()
        == CANARY_BYTES
    )
    with pytest.raises(CredentialLeaseError):
        lease.reveal(evaluation_time=REQUESTED_AT - timedelta(microseconds=1))
    with pytest.raises(CredentialLeaseExpiredError):
        lease.reveal(evaluation_time=EXPIRES_AT)
    with pytest.raises(CredentialLeaseExpiredError):
        lease.reveal(evaluation_time=EXPIRES_AT + timedelta(hours=1))
    with pytest.raises(CredentialArgumentError):
        lease.reveal(
            evaluation_time=datetime(2030, 1, 1, 9, 30)  # noqa: DTZ001
        )


def test_oracle_w_rotation_never_mutates_an_existing_lease() -> None:
    scope, reference = make_scope(), make_reference()
    first = _acquire(CredentialBoundary(backend=RecordingBackend()))
    rotated_facts = make_facts(
        scope,
        reference,
        generation=GENERATION + 1,
        acquired_at=REQUESTED_AT,
        expires_at=EXPIRES_AT,
    )
    rotated = CredentialBoundary(
        backend=RecordingBackend(result=(rotated_facts, CANARY_BYTES))
    ).acquire(scope, reference, requested_at=REQUESTED_AT)
    assert rotated.facts.generation == GENERATION + 1
    assert rotated.facts.facts_id != first.facts.facts_id
    assert first.facts.generation == GENERATION
    assert first.facts.facts_id != rotated.facts.facts_id
    assert first.reveal(evaluation_time=REQUESTED_AT).as_bytes() == CANARY_BYTES


def test_oracle_af_wrapper_backend_behaves_identically() -> None:
    class Wrapper:
        def __init__(self, inner: RecordingBackend) -> None:
            self._inner = inner

        @property
        def provider_id(self) -> str:
            return self._inner.provider_id

        def acquire(
            self,
            scope: CredentialScope,
            reference: CredentialReference,
            *,
            requested_at: datetime,
        ) -> tuple[CredentialFacts, bytes | str]:
            return self._inner.acquire(
                scope, reference, requested_at=requested_at
            )

    inner = RecordingBackend()
    scope, reference = make_scope(), make_reference()
    lease = CredentialBoundary(backend=Wrapper(inner)).acquire(
        scope, reference, requested_at=REQUESTED_AT
    )
    assert inner.acquire_calls == 1
    assert lease.facts.scope == scope
    assert lease.reveal(evaluation_time=REQUESTED_AT).as_bytes() == CANARY_BYTES


def test_oracle_ag_duck_typed_and_subclass_values_are_rejected() -> None:
    class LookalikeScope:
        provider = PROVIDER_ID

    class ReferenceSubclass(CredentialReference):
        pass

    class ScopeSubclass(CredentialScope):
        pass

    boundary = CredentialBoundary(backend=RecordingBackend())
    with pytest.raises(TypeError):
        boundary.acquire(
            LookalikeScope(), make_reference(), requested_at=REQUESTED_AT
        )
    with pytest.raises(TypeError):
        boundary.acquire(
            ScopeSubclass(
                schema="credential-scope-v1",
                provider=PROVIDER_ID,
                backend="broker-main",
                environment="PAPER",
                account_reference="acct-001",
                consumer="broker-adapter",
                purpose=CredentialPurpose.ORDER_SUBMIT,
            ),
            make_reference(),
            requested_at=REQUESTED_AT,
        )
    with pytest.raises(TypeError):
        boundary.acquire(
            make_scope(),
            ReferenceSubclass(value="deploy-ref-001"),
            requested_at=REQUESTED_AT,
        )


def test_oracle_ad_zero_mutation_on_success_and_failure() -> None:
    scope, reference = make_scope(), make_reference()
    scope_snapshot = scope.model_dump()
    reference_snapshot = reference.value
    module_state = {
        name: id(value)
        for name, value in vars(credentials_package).items()
    }
    lease = _acquire(CredentialBoundary(backend=RecordingBackend()))
    failing = RecordingBackend(error=RuntimeError("boom"))
    with pytest.raises(CredentialProviderError):
        CredentialBoundary(backend=failing).acquire(
            scope, reference, requested_at=REQUESTED_AT
        )
    assert scope.model_dump() == scope_snapshot
    assert reference.value == reference_snapshot
    assert lease.facts.scope == scope
    assert {
        name: id(value)
        for name, value in vars(credentials_package).items()
    } == module_state


def test_requested_at_is_normalized_to_utc_for_the_backend() -> None:
    captured: list[datetime] = []

    class CapturingBackend(RecordingBackend):
        def acquire(
            self,
            scope: CredentialScope,
            reference: CredentialReference,
            *,
            requested_at: datetime,
        ) -> tuple[CredentialFacts, bytes | str]:
            captured.append(requested_at)
            return super().acquire(
                scope, reference, requested_at=requested_at
            )

    shifted = datetime(2030, 1, 1, 17, 30, tzinfo=UTC)
    CredentialBoundary(backend=CapturingBackend()).acquire(
        make_scope(), make_reference(), requested_at=shifted
    )
    assert captured == [shifted]
    assert captured[0].tzinfo is UTC
