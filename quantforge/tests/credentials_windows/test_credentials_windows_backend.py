"""C2 backend oracles: address, generation, facts, failures, composition."""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from _credentials_windows_fixtures import (
    CANARY_BYTES,
    GENERATION,
    LEASE_SECONDS,
    PROVIDER_ID,
    REQUESTED_AT,
    TARGET_PREFIX,
    RecordingSeam,
    canonical_facts_id,
    compose_target,
    make_failure,
    make_record,
    make_reference,
    make_scope,
)

import quantforge.credentials_windows as package
from quantforge.credentials import (
    CredentialArgumentError,
    CredentialBoundary,
    CredentialProviderError,
    CredentialProviderIdentityError,
    CredentialPurpose,
    CredentialReference,
    CredentialScope,
    CredentialUnavailableError,
)
from quantforge.credentials_windows.backend import (
    WindowsCredentialManagerBackend,
)
from quantforge.credentials_windows.native import _CredentialFailureKind

ADDRESS_MESSAGE = "credential store address is malformed"
MALFORMED_MESSAGE = "credential store entry is malformed"
MISSING_MESSAGE = "credential store entry is missing"
IDENTITY_MESSAGE = (
    "credential store entry is not available to this logon identity"
)
UNAVAILABLE_MESSAGE = "credential store is unavailable"
READ_MESSAGE = "credential store read failed"


def _backend(outcome: Any) -> tuple[WindowsCredentialManagerBackend, RecordingSeam]:
    seam = RecordingSeam(outcome)
    return WindowsCredentialManagerBackend(seam=seam), seam


def _invalid_scope(**overrides: Any) -> CredentialScope:
    fields: dict[str, Any] = {
        "schema": "credential-scope-v1",
        "provider": PROVIDER_ID,
        "backend": "broker-main",
        "environment": "PAPER",
        "account_reference": "acct-001",
        "consumer": "broker-adapter",
        "purpose": CredentialPurpose.ORDER_SUBMIT,
    }
    fields.update(overrides)
    return CredentialScope.model_construct(**fields)


@pytest.mark.parametrize(
    "overrides",
    (
        {"provider": ""},
        {"provider": "bad value"},
        {"provider": "x" * 129},
        {"provider": "bad|value"},
        {"provider": 7},
        {"backend": ""},
        {"backend": "bad|value"},
        {"backend": "x" * 129},
        {"environment": "LIVE"},
        {"account_reference": "bad|value"},
        {"consumer": "x" * 129},
        {"purpose": "NOPE"},
    ),
)
def test_malformed_components_fail_before_any_native_read(
    overrides: dict[str, Any],
) -> None:
    backend, seam = _backend(make_record())
    scope = _invalid_scope(**overrides)
    with pytest.raises(CredentialArgumentError) as info:
        backend.acquire(scope, make_reference(), requested_at=REQUESTED_AT)
    assert str(info.value) == ADDRESS_MESSAGE
    assert seam.read_calls == 0


@pytest.mark.parametrize(
    "value",
    ("", "bad value", "x" * 129, "bad|value", 7),
)
def test_malformed_reference_fails_before_any_native_read(value: Any) -> None:
    backend, seam = _backend(make_record())
    reference = CredentialReference.model_construct(value=value)
    with pytest.raises(CredentialArgumentError) as info:
        backend.acquire(make_scope(), reference, requested_at=REQUESTED_AT)
    assert str(info.value) == ADDRESS_MESSAGE
    assert seam.read_calls == 0


def test_naive_or_offset_instant_fails_before_any_native_read() -> None:
    backend, seam = _backend(make_record())
    naive = datetime(2030, 1, 1, 9, 30)  # noqa: DTZ001 - naive is the case
    with pytest.raises(CredentialArgumentError):
        backend.acquire(make_scope(), make_reference(), requested_at=naive)
    offset = datetime(
        2030, 1, 1, 9, 30, tzinfo=timezone(timedelta(hours=8))
    )
    with pytest.raises(CredentialArgumentError):
        backend.acquire(make_scope(), make_reference(), requested_at=offset)
    assert seam.read_calls == 0


def test_exact_target_is_composed_byte_for_byte() -> None:
    scope = make_scope()
    reference = make_reference()
    target = compose_target(scope, reference)
    backend, seam = _backend(make_record(scope, reference))
    backend.acquire(scope, reference, requested_at=REQUESTED_AT)
    assert seam.targets == [target]
    assert target.count("|") == 7
    assert target.split("|")[0] == TARGET_PREFIX
    assert len(TARGET_PREFIX) == 17
    assert len(target) == 117


def test_maximum_grammar_valid_vector_is_exactly_690() -> None:
    opaque = "a" + "b" * 127
    scope = make_scope(
        provider=opaque,
        backend=opaque,
        environment="PRODUCTION",
        account_reference=opaque,
        consumer=opaque,
        purpose=CredentialPurpose.MARKET_DATA_READ,
    )
    reference = make_reference("c" + "d" * 127)
    target = compose_target(scope, reference)
    assert target.count("|") == 7
    assert len(target) == 690
    assert len(target) == len(target.encode("utf-16-le")) // 2
    backend, seam = _backend(make_record(scope, reference))
    backend.acquire(scope, reference, requested_at=REQUESTED_AT)
    assert seam.targets == [target]


def test_overlong_component_is_rejected_not_truncated() -> None:
    backend, seam = _backend(make_record())
    scope = _invalid_scope(consumer="x" * 129)
    with pytest.raises(CredentialArgumentError):
        backend.acquire(scope, make_reference(), requested_at=REQUESTED_AT)
    assert seam.read_calls == 0


@pytest.mark.parametrize("generation", (1, 42, 9999999999))
def test_generation_is_parsed_into_frozen_facts(generation: int) -> None:
    scope = make_scope()
    reference = make_reference()
    record = make_record(scope, reference, generation=generation)
    backend, seam = _backend(record)
    facts, material = backend.acquire(
        scope, reference, requested_at=REQUESTED_AT
    )
    assert facts.generation == generation
    assert material == CANARY_BYTES
    assert seam.read_calls == 1


@pytest.mark.parametrize(
    "user_name",
    (
        "quantforge-crd-v1:g0",
        "quantforge-crd-v1:g01",
        "quantforge:g1",
        "quantforge-crd-v1:g1 ",
        "quantforge-crd-v1:g10000000000",
    ),
)
def test_malformed_user_name_fails_closed(user_name: str) -> None:
    scope = make_scope()
    reference = make_reference()
    record = make_record(scope, reference, user_name=user_name)
    backend, _seam = _backend(record)
    with pytest.raises(CredentialProviderError) as info:
        backend.acquire(scope, reference, requested_at=REQUESTED_AT)
    assert str(info.value) == MALFORMED_MESSAGE


def test_returned_target_name_mismatch_fails_closed() -> None:
    scope = make_scope()
    reference = make_reference()
    target = compose_target(scope, reference)
    record = make_record(scope, reference, target_name=target.lower())
    backend, _seam = _backend(record)
    with pytest.raises(CredentialProviderError) as info:
        backend.acquire(scope, reference, requested_at=REQUESTED_AT)
    assert str(info.value) == MALFORMED_MESSAGE


@pytest.mark.parametrize(
    ("kind", "error", "message"),
    (
        (
            _CredentialFailureKind.MISSING,
            CredentialUnavailableError,
            MISSING_MESSAGE,
        ),
        (
            _CredentialFailureKind.ACCESS_DENIED,
            CredentialUnavailableError,
            IDENTITY_MESSAGE,
        ),
        (
            _CredentialFailureKind.STORE_UNAVAILABLE,
            CredentialUnavailableError,
            UNAVAILABLE_MESSAGE,
        ),
        (
            _CredentialFailureKind.MALFORMED,
            CredentialProviderError,
            MALFORMED_MESSAGE,
        ),
        (
            _CredentialFailureKind.NATIVE_ERROR,
            CredentialProviderError,
            READ_MESSAGE,
        ),
    ),
)
def test_failure_kinds_map_to_fixed_sanitized_errors(
    kind: _CredentialFailureKind, error: type[Exception], message: str
) -> None:
    backend, seam = _backend(make_failure(kind))
    with pytest.raises(error) as info:
        backend.acquire(make_scope(), make_reference(), requested_at=REQUESTED_AT)
    assert str(info.value) == message
    assert seam.read_calls == 1
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert not getattr(info.value, "__notes__", None)


def test_valid_acquisition_returns_frozen_facts_and_material() -> None:
    scope = make_scope()
    reference = make_reference()
    record = make_record(scope, reference)
    seam = RecordingSeam(record)
    backend = WindowsCredentialManagerBackend(seam=seam)
    lease = CredentialBoundary(backend=backend).acquire(
        scope, reference, requested_at=REQUESTED_AT
    )
    facts = lease.facts
    assert facts.schema == "credential-facts-v1"
    assert facts.provider == PROVIDER_ID
    assert facts.scope == scope
    assert facts.reference == reference
    assert facts.generation == GENERATION
    assert facts.acquired_at == REQUESTED_AT
    assert facts.expires_at == REQUESTED_AT + timedelta(seconds=LEASE_SECONDS)
    assert facts.facts_id == canonical_facts_id(scope, reference)
    assert lease.reveal(evaluation_time=REQUESTED_AT).as_bytes() == CANARY_BYTES
    assert seam.read_calls == 1


def test_facts_identity_is_secret_independent() -> None:
    scope = make_scope()
    reference = make_reference()
    first, _ = _backend(make_record(scope, reference, blob=b"first-secret"))
    second, _ = _backend(make_record(scope, reference, blob=b"other-secret"))
    first_facts, _ = first.acquire(scope, reference, requested_at=REQUESTED_AT)
    second_facts, _ = second.acquire(scope, reference, requested_at=REQUESTED_AT)
    assert first_facts.facts_id == second_facts.facts_id
    assert first_facts.facts_id == canonical_facts_id(scope, reference)
    changed_scope = make_scope(consumer="other-consumer")
    changed, _ = _backend(make_record(changed_scope, reference))
    changed_facts, _ = changed.acquire(
        changed_scope, reference, requested_at=REQUESTED_AT
    )
    assert changed_facts.facts_id != first_facts.facts_id
    assert changed_facts.facts_id == canonical_facts_id(
        changed_scope, reference
    )


def test_rotation_reads_each_acquisition_independently() -> None:
    scope = make_scope()
    reference = make_reference()
    seam = RecordingSeam(
        make_record(scope, reference, generation=3, blob=b"gen-3")
    )
    backend = WindowsCredentialManagerBackend(seam=seam)
    boundary = CredentialBoundary(backend=backend)
    first = boundary.acquire(scope, reference, requested_at=REQUESTED_AT)
    seam._outcome = make_record(
        scope, reference, generation=4, blob=b"gen-4"
    )
    second = boundary.acquire(scope, reference, requested_at=REQUESTED_AT)
    third = boundary.acquire(scope, reference, requested_at=REQUESTED_AT)
    assert first.facts.generation == 3
    assert second.facts.generation == 4
    assert third.facts.generation == 4
    assert first.facts.facts_id != second.facts.facts_id
    assert first.reveal(evaluation_time=REQUESTED_AT).as_bytes() == b"gen-3"
    assert second.reveal(evaluation_time=REQUESTED_AT).as_bytes() == b"gen-4"
    assert seam.read_calls == 3


def test_no_retry_after_a_failure() -> None:
    seam = RecordingSeam(make_failure(_CredentialFailureKind.MISSING))
    backend = WindowsCredentialManagerBackend(seam=seam)
    with pytest.raises(CredentialUnavailableError):
        backend.acquire(make_scope(), make_reference(), requested_at=REQUESTED_AT)
    assert seam.read_calls == 1


def test_provider_mismatch_fails_before_any_backend_call() -> None:
    scope = make_scope(provider="some-other-provider")
    seam = RecordingSeam(make_record(scope))
    backend = WindowsCredentialManagerBackend(seam=seam)
    with pytest.raises(CredentialProviderIdentityError):
        CredentialBoundary(backend=backend).acquire(
            scope, make_reference(), requested_at=REQUESTED_AT
        )
    assert seam.read_calls == 0


def test_rejecting_default_is_unchanged() -> None:
    with pytest.raises(CredentialUnavailableError) as info:
        CredentialBoundary().acquire(
            make_scope(provider="rejecting-credential-backend-v1"),
            make_reference(),
            requested_at=REQUESTED_AT,
        )
    assert str(info.value) == "no credential backend is installed"


def test_empty_blob_fails_closed_at_the_boundary() -> None:
    scope = make_scope()
    reference = make_reference()
    record = make_record(scope, reference, blob=b"")
    backend, seam = _backend(record)
    with pytest.raises(CredentialProviderError):
        CredentialBoundary(backend=backend).acquire(
            scope, reference, requested_at=REQUESTED_AT
        )
    assert seam.read_calls == 1


def test_concurrent_acquisitions_hold_no_shared_state() -> None:
    scope = make_scope()
    reference = make_reference()
    target = compose_target(scope, reference)
    seam = RecordingSeam(make_record(scope, reference))
    backend = WindowsCredentialManagerBackend(seam=seam)
    boundary = CredentialBoundary(backend=backend)
    results: list[int] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            lease = boundary.acquire(
                scope, reference, requested_at=REQUESTED_AT
            )
            results.append(lease.facts.generation)
        except BaseException as exc:  # noqa: BLE001 - recorded for assertion
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert results == [GENERATION] * 8
    assert seam.read_calls == 8
    assert seam.targets == [target] * 8
    assert not hasattr(backend, "__dict__")


def test_public_surface_is_exactly_one_supported_name() -> None:
    assert package.__all__ == ("build_credential_boundary",)
    assert callable(package.build_credential_boundary)


def test_composition_is_lazy_and_returns_fresh_boundaries() -> None:
    from quantforge.credentials_windows import composition, native

    created: list[int] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        created.append(1)
        raise AssertionError("no handle may be created before the call")

    original = native.ctypes.WinDLL
    native.ctypes.WinDLL = forbidden
    try:
        assert created == []
        with pytest.raises(CredentialUnavailableError):
            composition.build_credential_boundary()
    finally:
        native.ctypes.WinDLL = original
    assert created == [1]
    first = composition.build_credential_boundary()
    second = composition.build_credential_boundary()
    assert first is not second
    assert type(first).__name__ == "CredentialBoundary"
    assert type(second).__name__ == "CredentialBoundary"


def test_import_side_effects_are_absent_in_a_fresh_process() -> None:
    probe = Path(__file__).with_name("_credentials_windows_import_probe.py")
    completed = subprocess.run(
        [sys.executable, str(probe)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert completed.stdout.strip() == "CREDENTIAL_WINDOWS_TRIPWIRE_OK"
