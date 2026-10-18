"""Oracle R/T/Y and the repaired exception-context regression."""

from __future__ import annotations

import json

import pytest
from _credential_fixtures import (
    CANARY,
    CANARY_BYTES,
    EXPIRES_AT,
    REQUESTED_AT,
    RecordingBackend,
    make_facts,
    make_reference,
    make_scope,
    package_frames_containing_canary,
)

from quantforge.credentials import (
    CredentialBoundary,
    CredentialProviderError,
)


class SecretBearingBackendError(RuntimeError):
    """Synthetic backend failure that carries the canary everywhere."""

    def __init__(self) -> None:
        super().__init__("backend failure carrying " + CANARY)
        self.secret_attribute = CANARY
        try:
            self.add_note("note carrying " + CANARY)
        except AttributeError:  # pragma: no cover - Python >= 3.11 has notes
            pass

    def __repr__(self) -> str:
        return "SecretBearingBackendError(" + repr(CANARY) + ")"

    def __str__(self) -> str:
        return "str carrying " + CANARY


def _inside_handler_wrap() -> None:
    """The rejected mechanism: raise from None inside the handler."""
    try:
        raise SecretBearingBackendError()
    except Exception:  # noqa: BLE001 - reproduces the rejected mechanism
        raise CredentialProviderError(
            "credential provider qf-test-provider-v1 failed"
        ) from None


def test_repaired_finding_inside_handler_wrapping_retains_context() -> None:
    with pytest.raises(CredentialProviderError) as info:
        _inside_handler_wrap()
    error = info.value
    assert error.__context__ is not None
    assert type(error.__context__) is SecretBearingBackendError
    assert CANARY in repr(error.__context__)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_oracle_r_backend_exception_is_sanitized_on_every_surface() -> None:
    backend = RecordingBackend(error=SecretBearingBackendError())
    with pytest.raises(CredentialProviderError) as info:
        CredentialBoundary(backend=backend).acquire(
            make_scope(), make_reference(), requested_at=REQUESTED_AT
        )
    error = info.value
    assert type(error) is CredentialProviderError
    assert error.args == (
        "credential provider qf-test-provider-v1 failed",
    )
    assert CANARY not in str(error)
    assert CANARY not in repr(error)
    assert CANARY not in repr(error.args)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert not getattr(error, "__notes__", None)
    assert package_frames_containing_canary(error) == []
    assert backend.acquire_calls == 1


def test_oracle_y_canary_absence_on_success_and_failure_paths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("DEBUG"):
        lease = CredentialBoundary(backend=RecordingBackend()).acquire(
            make_scope(), make_reference(), requested_at=REQUESTED_AT
        )
        material = lease.reveal(evaluation_time=REQUESTED_AT)
        surfaces = [
            repr(lease),
            str(lease),
            repr(material),
            str(material),
            repr(lease.facts),
            json.dumps(
                lease.facts.scope.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            json.dumps(
                {"reference": lease.facts.reference.value},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
        ]
        failing = RecordingBackend(error=SecretBearingBackendError())
        with pytest.raises(CredentialProviderError) as info:
            CredentialBoundary(backend=failing).acquire(
                make_scope(), make_reference(), requested_at=REQUESTED_AT
            )
        surfaces.extend(
            [
                str(info.value),
                repr(info.value),
                repr(info.value.args),
                repr(info.value.__cause__),
                repr(info.value.__context__),
                repr(getattr(info.value, "__notes__", None)),
            ]
        )
    for surface in surfaces:
        assert CANARY not in surface
    assert [
        record for record in caplog.records
        if record.name.startswith("quantforge")
    ] == []


def test_sanitized_error_carries_only_the_fixed_message() -> None:
    backend = RecordingBackend(error=RuntimeError("generic failure"))
    with pytest.raises(CredentialProviderError) as info:
        CredentialBoundary(backend=backend).acquire(
            make_scope(), make_reference(), requested_at=REQUESTED_AT
        )
    assert info.value.args == (
        "credential provider qf-test-provider-v1 failed",
    )
    assert "generic failure" not in repr(info.value)


def test_material_bytes_do_not_leak_through_validated_paths() -> None:
    lease = CredentialBoundary(backend=RecordingBackend()).acquire(
        make_scope(), make_reference(), requested_at=REQUESTED_AT
    )
    assert lease.reveal(evaluation_time=REQUESTED_AT).as_bytes() == CANARY_BYTES
    assert lease.facts.expires_at == EXPIRES_AT
    assert CANARY not in repr(lease)


def test_post_return_failure_frames_do_not_expose_raw_material() -> None:
    scope, reference = make_scope(), make_reference()
    facts = make_facts(
        scope,
        reference,
        acquired_at=REQUESTED_AT,
        expires_at=EXPIRES_AT,
        facts_id="credential-facts-v1-" + "0" * 64,
    )
    backend = RecordingBackend(result=(facts, CANARY_BYTES))
    with pytest.raises(CredentialProviderError) as info:
        CredentialBoundary(backend=backend).acquire(
            scope, reference, requested_at=REQUESTED_AT
        )
    assert package_frames_containing_canary(info.value) == []
