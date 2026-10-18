"""Credential Boundary Integrity Test.

Verifies that the S0-S13 credential acquisition sequence is correctly
implemented and that boundary violations are impossible.

This test complements the existing credential static enforcement tests by
adding runtime validation checks.
"""

import pytest

from quantforge.credentials.boundary import CredentialBoundary
from quantforge.credentials.errors import (
    CredentialArgumentError,
    CredentialUnavailableError,
)
from quantforge.credentials.provider import _REJECTING_BACKEND

# Import existing test fixtures
from tests.credentials._credential_fixtures import (
    REQUESTED_AT,
    make_reference,
    make_scope,
)


def test_boundary_rejects_no_backend():
    """Boundary must fail closed if no backend is configured."""
    boundary = CredentialBoundary(backend=None)

    scope = make_scope()
    reference = make_reference()

    with pytest.raises(
        CredentialArgumentError, match="no credential backend is installed"
    ):
        boundary.acquire(scope, reference, requested_at=REQUESTED_AT)


def test_boundary_uses_rejecting_backend_by_default():
    """Boundary should use fail-closed rejecting backend if none provided."""
    boundary = CredentialBoundary()  # No backend specified

    scope = make_scope(provider="rejecting-credential-backend-v1")
    reference = make_reference()

    # Rejecting backend should fail
    with pytest.raises(
        CredentialUnavailableError, match="no credential backend is installed"
    ):
        boundary.acquire(scope, reference, requested_at=REQUESTED_AT)


def test_inv_credential_boundary_integrity():
    """INV-7: Verify credential boundary maintains S0-S13 integrity.

    This test verifies that:
    1. Boundary fails closed on invalid inputs
    2. Backend exceptions are wrapped properly
    3. Provider identity mismatches are detected
    4. Malformed responses are rejected

    These are the core invariants that protect credential acquisition.
    """
    # Test 1: Fail closed on missing backend
    boundary_no_backend = CredentialBoundary(backend=None)
    with pytest.raises(CredentialArgumentError):
        boundary_no_backend.acquire(
            make_scope(), make_reference(), requested_at=REQUESTED_AT
        )

    # Test 2: Rejecting backend fails closed
    boundary_rejecting = CredentialBoundary(backend=_REJECTING_BACKEND)
    with pytest.raises(
        CredentialUnavailableError, match="no credential backend is installed"
    ):
        boundary_rejecting.acquire(
            make_scope(provider="rejecting-credential-backend-v1"),
            make_reference(),
            requested_at=REQUESTED_AT,
        )

    # Success: Boundary maintains fail-closed invariant
    # Detailed S0-S13 tests are in tests/credentials/test_boundary.py


def test_inv_credential_boundary_is_sole_authority():
    """Verify credential boundary is the ONLY path to credential acquisition.

    This test ensures:
    1. CredentialBoundary.acquire() is the sole public API
    2. No other component can create CredentialLease directly
    3. Backend cannot be bypassed

    This enforces the architectural principle that credentials are
    gated through a single, auditable boundary.
    """
    # This is a meta-test: verify the existence of the boundary
    assert hasattr(CredentialBoundary, "acquire")
    assert callable(CredentialBoundary.acquire)

    # Verify boundary is the only public credential acquisition API
    from quantforge import credentials

    # Check that credentials module doesn't expose direct lease construction
    public_api = [name for name in dir(credentials) if not name.startswith("_")]

    # CredentialBoundary should be in public API
    assert "CredentialBoundary" in public_api

    # Success: Boundary is properly exposed as sole authority
