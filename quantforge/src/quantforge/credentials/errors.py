"""Fail-closed error taxonomy for the credential isolation boundary.

CREDENTIAL-001 section 23 freezes exactly this set of error types.  Every
boundary failure maps to one of them (or to ``TypeError`` for a wrong runtime
argument type); no failure path returns a lease, material or default value.
"""

from __future__ import annotations


class CredentialError(ValueError):
    """Base semantic error raised inside the credential boundary."""


class CredentialArgumentError(CredentialError):
    """Malformed caller input, backend conformance, reference, scope or
    instant value."""


class CredentialProviderError(CredentialError):
    """Sanitized backend-attributable failure under the bounded section 15
    exception guarantee."""


class CredentialProviderIdentityError(CredentialProviderError):
    """Provider identity is malformed or contradicts the requested scope."""


class CredentialUnavailableError(CredentialProviderError):
    """No credential exists for the reference or scope, including the
    shipped rejecting default."""


class CredentialRejectedError(CredentialProviderError):
    """The backend explicitly refused the acquisition."""


class CredentialScopeMismatchError(CredentialProviderError):
    """Served credential facts do not match the requested scope exactly."""


class CredentialLeaseError(CredentialError):
    """A secret-bearing object is unusable, discarded or out of order."""


class CredentialLeaseExpiredError(CredentialLeaseError):
    """The evaluation instant is at or after the lease expiry boundary."""
