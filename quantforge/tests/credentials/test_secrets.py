"""Secret-bearing object oracles: redaction, serialization, discard."""

from __future__ import annotations

import copy
import json
import pickle

import pytest
from _credential_fixtures import (
    CANARY,
    CANARY_BYTES,
    REQUESTED_AT,
    RecordingBackend,
    make_reference,
    make_scope,
)

from quantforge.credentials import (
    CredentialBoundary,
    CredentialLease,
    CredentialLeaseError,
    SecretMaterial,
)


def _lease() -> CredentialLease:
    return CredentialBoundary(backend=RecordingBackend()).acquire(
        make_scope(), make_reference(), requested_at=REQUESTED_AT
    )


def test_repr_and_str_are_redacted() -> None:
    lease = _lease()
    material = lease.reveal(evaluation_time=REQUESTED_AT)
    assert isinstance(material, SecretMaterial)
    for value in (lease, material):
        for rendering in (repr(value), str(value)):
            assert CANARY not in rendering
            assert "redacted" in rendering
    assert lease.facts.facts_id in repr(lease)
    assert CANARY not in repr(lease.facts)


def test_secret_objects_have_no_instance_dict() -> None:
    lease = _lease()
    material = lease.reveal(evaluation_time=REQUESTED_AT)
    for value in (lease, material):
        assert not hasattr(value, "__dict__")
        with pytest.raises(TypeError):
            vars(value)


def test_oracle_z_copy_pickle_and_json_paths_fail_closed() -> None:
    lease = _lease()
    material = lease.reveal(evaluation_time=REQUESTED_AT)
    for value in (lease, material):
        with pytest.raises(TypeError):
            copy.copy(value)
        with pytest.raises(TypeError):
            copy.deepcopy(value)
        with pytest.raises(TypeError):
            pickle.dumps(value)
        with pytest.raises(TypeError):
            json.dumps(value)
        with pytest.raises(AttributeError):
            value.model_dump()
        with pytest.raises(AttributeError):
            value.model_dump_json()
        with pytest.raises(AttributeError):
            value.to_dict()


def test_material_public_surface_is_exactly_as_bytes_and_discard() -> None:
    material = _lease().reveal(evaluation_time=REQUESTED_AT)
    public = sorted(
        name for name in dir(material) if not name.startswith("_")
    )
    assert public == ["as_bytes", "discard"]


def test_as_bytes_returns_fresh_copies_and_prior_copies_survive_discard() -> None:
    lease = _lease()
    material = lease.reveal(evaluation_time=REQUESTED_AT)
    first = material.as_bytes()
    second = material.as_bytes()
    assert first == second == CANARY_BYTES
    assert first is not second
    lease.discard()
    with pytest.raises(CredentialLeaseError):
        material.as_bytes()
    assert first == CANARY_BYTES
    assert second == CANARY_BYTES


def test_oracle_x_discard_is_idempotent_and_blocks_future_access() -> None:
    lease = _lease()
    material = lease.reveal(evaluation_time=REQUESTED_AT)
    lease.discard()
    lease.discard()
    with pytest.raises(CredentialLeaseError):
        lease.reveal(evaluation_time=REQUESTED_AT)
    with pytest.raises(CredentialLeaseError):
        material.as_bytes()
    material.discard()
    material.discard()
    with pytest.raises(CredentialLeaseError):
        material.as_bytes()


def test_material_discard_blocks_only_that_material() -> None:
    lease = _lease()
    material = lease.reveal(evaluation_time=REQUESTED_AT)
    material.discard()
    with pytest.raises(CredentialLeaseError):
        material.as_bytes()
    assert lease.reveal(evaluation_time=REQUESTED_AT) is material


def test_lease_identity_semantics_and_no_ordering() -> None:
    first = _lease()
    second = _lease()
    assert first == first  # noqa: PLR0124 - identity semantics on the lease
    assert first != second
    assert first.reveal(evaluation_time=REQUESTED_AT) is not second.reveal(
        evaluation_time=REQUESTED_AT
    )
    with pytest.raises(TypeError):
        _ = first < second


def test_facts_property_returns_the_bound_facts() -> None:
    lease = _lease()
    assert lease.facts.scope == make_scope()
    assert lease.facts.reference == make_reference()
